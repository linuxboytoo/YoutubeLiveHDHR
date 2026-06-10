import json
import logging
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

_logo_cache: dict[str, Optional[str]] = {}
program_info: dict[str, dict] = {}
_program_fetched_at: dict[str, float] = {}

PROGRAM_TTL = 300  # seconds between program info refreshes

_CACHE_PATH: Optional[str] = None


def init_cache(config_path: str):
    """Call once at startup with the config file path to enable disk persistence."""
    global _CACHE_PATH
    _CACHE_PATH = os.path.join(os.path.dirname(config_path), "guide_cache.json")
    _load_cache()


def _load_cache():
    if not _CACHE_PATH:
        return
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        _logo_cache.update(data.get("logos", {}))
        program_info.update(data.get("programs", {}))
        _program_fetched_at.update({k: data.get("fetched_at", {}).get(k, 0)
                                     for k in program_info})
        logger.info("Loaded guide cache: %d logos, %d programs", len(_logo_cache), len(program_info))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not load guide cache: %s", e)


def _save_cache():
    if not _CACHE_PATH:
        return
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump({
                "logos": _logo_cache,
                "programs": program_info,
                "fetched_at": _program_fetched_at,
            }, f, indent=2)
    except Exception as e:
        logger.warning("Could not save guide cache: %s", e)


def fetch_channel_logo(uc_id: str) -> Optional[str]:
    if uc_id in _logo_cache:
        return _logo_cache[uc_id]
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-single-json",
             f"https://www.youtube.com/channel/{uc_id}"],
            capture_output=True, text=True, timeout=60
        )
        if result.stdout.strip():
            info = json.loads(result.stdout.strip())
            thumbs = [t for t in info.get("thumbnails", []) if t.get("url")]
            if thumbs:
                # Prefer square-ish thumbnails (avatars) over wide banners
                def squareness(t):
                    w, h = t.get("width") or 1, t.get("height") or 1
                    return -abs(w - h)

                best = max(thumbs, key=squareness)
                url = best["url"]
                _logo_cache[uc_id] = url
                _save_cache()
                logger.info("Logo cached for %s", uc_id)
                return url
    except Exception as e:
        logger.warning("Error fetching logo for %s: %s", uc_id, e)
    _logo_cache[uc_id] = None
    return None


def maybe_refresh_program(uc_id: str):
    """Fetch current live stream title/description/thumbnail, rate-limited by TTL."""
    now = time.time()
    if now - _program_fetched_at.get(uc_id, 0) < PROGRAM_TTL:
        return
    try:
        result = subprocess.run(
            ["yt-dlp", "--playlist-items", "1", "--dump-json", "--no-warnings",
             f"https://www.youtube.com/channel/{uc_id}/live"],
            capture_output=True, text=True, timeout=30
        )
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not line:
            return
        info = json.loads(line)
        thumbs = [t for t in info.get("thumbnails", []) if t.get("url")]
        thumbnail = (
            max(thumbs, key=lambda t: (t.get("width") or 0))["url"]
            if thumbs else info.get("thumbnail")
        )
        program_info[uc_id] = {
            "title": info.get("title", "Live"),
            "description": info.get("description", ""),
            "thumbnail": thumbnail,
            "start_time": info.get("timestamp") or info.get("release_timestamp") or int(now),
        }
        _program_fetched_at[uc_id] = now
        _save_cache()
        logger.info("Program info updated for %s: %s", uc_id, program_info[uc_id]["title"])
    except Exception as e:
        logger.warning("Error fetching program info for %s: %s", uc_id, e)


def build_guide_json(config, live_state: dict) -> list:
    """Build HDHR-format guide.json consumed natively by Jellyfin/Plex."""
    now = int(time.time())
    entries = []

    for ch in config.channels:
        prog = program_info.get(ch.youtube, {})
        entry = {
            "GuideNumber": str(ch.id),
            "GuideName": ch.name,
        }
        logo = _logo_cache.get(ch.youtube)
        if logo:
            entry["ImageURL"] = logo
        if prog:
            start = prog.get("start_time", now)
            entry["Guide"] = [{
                "StartTime": start,
                "EndTime": start + 3600,
                "Title": prog.get("title", ch.name),
                "Synopsis": prog.get("description", ""),
                "ImageURL": prog.get("thumbnail", ""),
            }]
        entries.append(entry)

    for grp in config.groups:
        prog = {}
        logo = None
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                prog = program_info.get(member.youtube, {})
                logo = _logo_cache.get(member.youtube)
                break
        entry = {
            "GuideNumber": str(grp.id),
            "GuideName": grp.name,
        }
        if logo:
            entry["ImageURL"] = logo
        if prog:
            start = prog.get("start_time", now)
            entry["Guide"] = [{
                "StartTime": start,
                "EndTime": start + 3600,
                "Title": prog.get("title", grp.name),
                "Synopsis": prog.get("description", ""),
                "ImageURL": prog.get("thumbnail", ""),
            }]
        entries.append(entry)

    return entries


def build_xmltv(config, live_state: dict) -> str:
    root = ET.Element("tv", attrib={"generator-info-name": "YoutubeLiveHDHR"})

    for ch in config.channels:
        el = ET.SubElement(root, "channel", id=str(ch.id))
        ET.SubElement(el, "display-name").text = ch.name
        logo = _logo_cache.get(ch.youtube)
        if logo:
            ET.SubElement(el, "icon", src=logo)

    for grp in config.groups:
        el = ET.SubElement(root, "channel", id=str(grp.id))
        ET.SubElement(el, "display-name").text = grp.name
        # Use logo of whichever member is currently live
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                logo = _logo_cache.get(member.youtube)
                if logo:
                    ET.SubElement(el, "icon", src=logo)
                break

    now = int(time.time())

    for ch in config.channels:
        prog = program_info.get(ch.youtube, {})
        start = prog.get("start_time", now)
        _add_programme(root, str(ch.id), prog, ch.name, start, start + 3600)

    for grp in config.groups:
        prog = {}
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                prog = program_info.get(member.youtube, {})
                break
        start = prog.get("start_time", now)
        _add_programme(root, str(grp.id), prog, grp.name, start, start + 3600)

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _add_programme(root, channel_id: str, prog: dict, fallback_name: str, start: int, stop: int):
    fmt = "%Y%m%d%H%M%S +0000"
    el = ET.SubElement(root, "programme",
                       start=time.strftime(fmt, time.gmtime(start)),
                       stop=time.strftime(fmt, time.gmtime(stop)),
                       channel=channel_id)
    ET.SubElement(el, "title").text = prog.get("title", fallback_name)
    if prog.get("description"):
        ET.SubElement(el, "desc").text = prog["description"]
    if prog.get("thumbnail"):
        ET.SubElement(el, "icon", src=prog["thumbnail"])
    ET.SubElement(el, "category").text = "Live"
