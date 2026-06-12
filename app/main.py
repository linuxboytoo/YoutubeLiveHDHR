import asyncio
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager

import yaml as _yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app import guide, poller
from app.config import CONFIG_PATH, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
http_logger = logging.getLogger("http.access")

DEVICE_ID = os.getenv("HDHR_DEVICE_ID", "12345678")
DEVICE_AUTH = os.getenv("HDHR_DEVICE_AUTH", "")
FRIENDLY_NAME = os.getenv("HDHR_FRIENDLY_NAME", "YouTubeHDHR")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "5004"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

_config = None


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _base_url() -> str:
    if BASE_URL:
        return BASE_URL
    return f"http://{_get_local_ip()}:{PORT}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config
    logger.info("Loading config...")
    guide.init_cache(os.getenv("CONFIG_PATH", "/config/channels.yaml"))
    _config = load_config()
    poller.start(_config, POLL_INTERVAL)
    yield
    poller.stop()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = (time.time() - start) * 1000
    http_logger.info(
        "%s %s%s [%d] %.0fms | UA: %s",
        request.method,
        request.url.path,
        f"?{request.url.query}" if request.url.query else "",
        response.status_code,
        ms,
        request.headers.get("user-agent", "-"),
    )
    return response


@app.get("/discover.json")
def discover():
    base = _base_url()
    return {
        "FriendlyName": FRIENDLY_NAME,
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomerun4_atsc",
        "FirmwareVersion": "20190621",
        "DeviceID": DEVICE_ID,
        "DeviceAuth": DEVICE_AUTH,
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": 10,
    }


@app.get("/device.xml")
def device_xml():
    base = _base_url()
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{base}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{FRIENDLY_NAME}</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>HDTC-2US</modelName>
    <modelNumber>HDTC-2US</modelNumber>
    <serialNumber>{DEVICE_ID}</serialNumber>
    <UDN>uuid:d865bba8-e8e8-4c6e-b4c7-{DEVICE_ID}</UDN>
  </device>
</root>"""
    return Response(content=xml, media_type="application/xml")


@app.get("/lineup_status.json")
def lineup_status():
    return {
        "ScanInProgress": 0,
        "ScanPossible": 0,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


@app.get("/lineup.json")
def lineup():
    if _config is None:
        return []

    base = _base_url()
    entries = []

    # Groups first so they get low channel numbers (1, 2, 3…)
    for i, grp in enumerate(_config.groups, 1):
        gn = str(i)
        active_cid = poller.live_state.get(grp.id)
        active_member = next((c for c in _config.channels if c.id == active_cid), None) if active_cid else None
        guide_name = f"{grp.name} • {active_member.name}" if active_member else grp.name
        entry = {
            "GuideNumber": gn,
            "GuideName": guide_name,
            "URL": f"{base}/auto/v{grp.id}",
            "Guide_ID": gn,
            "Station": gn,
        }
        entries.append(entry)

    for ch in _config.channels:
        gn = str(ch.id)
        entry = {
            "GuideNumber": gn,
            "GuideName": ch.name,
            "URL": f"{base}/auto/v{ch.id}",
            "Guide_ID": gn,
            "Station": gn,
        }
        logo = guide._logo_cache.get(ch.youtube)
        if logo:
            entry["ImageURL"] = logo
        entries.append(entry)

    return entries


async def _proxy_stream(youtube_url: str):
    """Pipe a YouTube stream through yt-dlp → ffmpeg → MPEG-TS to the client.

    Jellyfin's transcoder cannot access YouTube CDN URLs directly (auth headers
    required), so we proxy the stream server-side instead of redirecting.
    """
    logger.info("Stream proxy start: %s", youtube_url)

    ytdlp = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--format", "best[protocol^=m3u8]/best",
        "--no-part",
        "--no-warnings",
        "-o", "-",
        youtube_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-c", "copy",
        "-f", "mpegts",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def _pipe_to_ffmpeg():
        """Forward yt-dlp stdout → ffmpeg stdin."""
        try:
            while True:
                chunk = await ytdlp.stdout.read(65536)
                if not chunk:
                    break
                ffmpeg.stdin.write(chunk)
                await ffmpeg.stdin.drain()
        except Exception as e:
            logger.debug("yt-dlp pipe ended: %s", e)
        finally:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass

    pipe_task = asyncio.create_task(_pipe_to_ffmpeg())

    try:
        while True:
            chunk = await ffmpeg.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    except Exception as e:
        logger.warning("Stream proxy error: %s", e)
    finally:
        logger.info("Stream proxy end: %s", youtube_url)
        pipe_task.cancel()
        for proc in (ffmpeg, ytdlp):
            try:
                proc.kill()
            except Exception:
                pass
        await asyncio.gather(pipe_task, return_exceptions=True)


@app.get("/auto/v{channel_id}")
async def stream(channel_id: int):
    if _config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")

    channel = next((c for c in _config.channels if c.id == channel_id), None)
    group = next((g for g in _config.groups if g.id == channel_id), None)

    youtube_url = None

    if channel and poller.live_state.get(channel_id):
        youtube_url = f"https://www.youtube.com/channel/{channel.youtube}/live"
    elif group:
        active_cid = poller.live_state.get(channel_id)
        if active_cid:
            member = next((c for c in _config.channels if c.id == active_cid), None)
            if member:
                youtube_url = f"https://www.youtube.com/channel/{member.youtube}/live"

    if not youtube_url:
        target = channel or group
        if target is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        if target.fallback:
            youtube_url = target.fallback.url

    if not youtube_url:
        raise HTTPException(status_code=503, detail="No stream available")

    return StreamingResponse(
        _proxy_stream(youtube_url),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/guide.json")
def guide_json():
    if _config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")
    return guide.build_guide_json(_config, poller.live_state)


@app.get("/epg.xml")
def epg_xml():
    if _config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")
    xml = guide.build_xmltv(_config, poller.live_state)
    return Response(content=xml, media_type="application/xml")


@app.post("/reload")
def reload_config():
    global _config
    _config = load_config()
    poller.reload(_config)
    return {"status": "reloaded"}


# ── Config UI helpers ─────────────────────────────────────────────────────────

class _ChannelBody(BaseModel):
    name: str
    youtube: str

class _GroupBody(BaseModel):
    name: str
    channels: list[int] = []

def _read_yaml() -> dict:
    with open(CONFIG_PATH) as f:
        return _yaml.safe_load(f) or {"channels": [], "groups": []}

def _write_yaml(data: dict):
    with open(CONFIG_PATH, "w") as f:
        _yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

def _do_reload():
    def _task():
        global _config
        _config = load_config()
        poller.reload(_config)
    threading.Thread(target=_task, daemon=True).start()


# ── Config API ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    data = _read_yaml()
    ls = poller.live_state
    for ch in data.get("channels", []):
        ch["live"] = bool(ls.get(ch["id"]))
    for grp in data.get("groups", []):
        grp["live"] = ls.get(grp["id"]) is not None
    return data

@app.post("/api/channels")
def api_add_channel(body: _ChannelBody):
    data = _read_yaml()
    channels = data.setdefault("channels", [])
    new_id = max((c["id"] for c in channels), default=1000) + 1
    entry = {"id": new_id, "name": body.name, "youtube": body.youtube}
    channels.append(entry)
    _write_yaml(data)
    _do_reload()
    return entry

@app.put("/api/channels/{channel_id}")
def api_update_channel(channel_id: int, body: _ChannelBody):
    data = _read_yaml()
    channels = data.get("channels", [])
    idx = next((i for i, c in enumerate(channels) if c["id"] == channel_id), None)
    if idx is None:
        raise HTTPException(status_code=404)
    channels[idx]["name"] = body.name
    channels[idx]["youtube"] = body.youtube
    _write_yaml(data)
    _do_reload()
    return channels[idx]

@app.delete("/api/channels/{channel_id}")
def api_delete_channel(channel_id: int):
    data = _read_yaml()
    data["channels"] = [c for c in data.get("channels", []) if c["id"] != channel_id]
    for grp in data.get("groups", []):
        grp["channels"] = [cid for cid in grp.get("channels", []) if cid != channel_id]
    _write_yaml(data)
    _do_reload()
    return {"ok": True}

@app.post("/api/groups")
def api_add_group(body: _GroupBody):
    data = _read_yaml()
    groups = data.setdefault("groups", [])
    new_id = max((g["id"] for g in groups), default=2000) + 1
    entry = {"id": new_id, "name": body.name, "channels": body.channels}
    groups.append(entry)
    _write_yaml(data)
    _do_reload()
    return entry

@app.put("/api/groups/{group_id}")
def api_update_group(group_id: int, body: _GroupBody):
    data = _read_yaml()
    groups = data.get("groups", [])
    idx = next((i for i, g in enumerate(groups) if g["id"] == group_id), None)
    if idx is None:
        raise HTTPException(status_code=404)
    groups[idx]["name"] = body.name
    groups[idx]["channels"] = body.channels
    _write_yaml(data)
    _do_reload()
    return groups[idx]

@app.delete("/api/groups/{group_id}")
def api_delete_group(group_id: int):
    data = _read_yaml()
    data["groups"] = [g for g in data.get("groups", []) if g["id"] != group_id]
    _write_yaml(data)
    _do_reload()
    return {"ok": True}


# ── Config UI ─────────────────────────────────────────────────────────────────

@app.get("/ui")
@app.get("/ui/")
def ui_page():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ui.html")
    with open(html_path) as f:
        return Response(content=f.read(), media_type="text/html")
