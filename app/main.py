import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response

from app import guide, poller
from app.config import load_config
from app.resolver import get_live_stream_url, get_stream_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEVICE_ID = os.getenv("HDHR_DEVICE_ID", "12345678")
DEVICE_AUTH = os.getenv("HDHR_DEVICE_AUTH", "")
FRIENDLY_NAME = os.getenv("HDHR_FRIENDLY_NAME", "YouTubeHDHR")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "5004"))

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


@app.get("/discover.json")
def discover():
    ip = _get_local_ip()
    return {
        "FriendlyName": FRIENDLY_NAME,
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomerun4_atsc",
        "FirmwareVersion": "20190621",
        "DeviceID": DEVICE_ID,
        "DeviceAuth": DEVICE_AUTH,
        "BaseURL": f"http://{ip}:{PORT}",
        "LineupURL": f"http://{ip}:{PORT}/lineup.json",
        "TunerCount": 10,
    }


@app.get("/device.xml")
def device_xml():
    ip = _get_local_ip()
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>http://{ip}:{PORT}</URLBase>
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

    ip = _get_local_ip()
    entries = []

    for ch in _config.channels:
        gn = str(ch.id)
        entry = {
            "GuideNumber": gn,
            "GuideName": ch.name,
            "URL": f"http://{ip}:{PORT}/auto/v{ch.id}",
            "Guide_ID": gn,
            "Station": gn,
        }
        logo = guide._logo_cache.get(ch.youtube)
        if logo:
            entry["ImageURL"] = logo
        entries.append(entry)

    for grp in _config.groups:
        gn = str(grp.id)
        entry = {
            "GuideNumber": gn,
            "GuideName": grp.name,
            "URL": f"http://{ip}:{PORT}/auto/v{grp.id}",
            "Guide_ID": gn,
            "Station": gn,
        }
        entries.append(entry)

    return entries


@app.get("/auto/v{channel_id}")
def stream(channel_id: int):
    if _config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")

    channel = next((c for c in _config.channels if c.id == channel_id), None)
    group = next((g for g in _config.groups if g.id == channel_id), None)

    # Resolve which UC ID to stream from
    uc_id = None
    if channel and poller.live_state.get(channel_id):
        uc_id = channel.youtube
    elif group:
        active_cid = poller.live_state.get(channel_id)  # stores active member channel id
        if active_cid:
            member = next((c for c in _config.channels if c.id == active_cid), None)
            if member:
                uc_id = member.youtube

    if uc_id:
        live_url = get_live_stream_url(uc_id)
        if live_url:
            return RedirectResponse(url=live_url, status_code=302)

    # Find channel or group config for fallback

    target = channel or group
    if target is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    if target.fallback:
        fb_url = target.fallback.url
        if target.fallback.type == "playlist":
            resolved = get_stream_url(fb_url)
            if resolved:
                return RedirectResponse(url=resolved, status_code=302)
        elif target.fallback.type == "url":
            resolved = get_stream_url(fb_url)
            if resolved:
                return RedirectResponse(url=resolved, status_code=302)

    raise HTTPException(status_code=503, detail="No stream available")


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
