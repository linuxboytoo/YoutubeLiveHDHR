import json
import logging
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

_logo_cache: dict[str, Optional[str]] = {}
program_info: dict[str, dict] = {}
_program_fetched_at: dict[str, float] = {}

PROGRAM_TTL = 300  # seconds between program info refreshes
LIVE_SLOT_HOURS = 0.5  # live slot window — short so stale entries expire quickly

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


def _fetch_program(uc_id: str, live: bool = False):
    """Fetch program info. Uses /live URL + is_live filter when live."""
    now = time.time()
    if now - _program_fetched_at.get(uc_id, 0) < PROGRAM_TTL:
        return

    if live:
        # --match-filter ensures we only accept genuinely live content;
        # yt-dlp sometimes returns a past video from the /live URL.
        cmd = ["yt-dlp", "--playlist-items", "1", "--dump-json", "--no-warnings",
               "--match-filter", "is_live",
               f"https://www.youtube.com/channel/{uc_id}/live"]
    else:
        cmd = ["yt-dlp", "--playlist-items", "1", "--dump-json", "--no-warnings",
               f"https://www.youtube.com/channel/{uc_id}/videos"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not line:
            logger.debug("No %s data for %s", "live stream" if live else "video", uc_id)
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
            "is_live": bool(info.get("is_live")),
        }
        _program_fetched_at[uc_id] = now
        _save_cache()
        logger.info("Program info updated for %s: %s (live=%s)",
                    uc_id, program_info[uc_id]["title"][:60], program_info[uc_id]["is_live"])
    except Exception as e:
        logger.warning("Error fetching program info for %s: %s", uc_id, e)


def force_refresh_program(uc_id: str, is_live: bool = False):
    """Bypass TTL and re-fetch program info. Pass is_live=True to fetch the current stream title."""
    _program_fetched_at.pop(uc_id, None)
    _fetch_program(uc_id, live=is_live)


def refresh_all_programs(channels, live_state: dict = None):
    """Refresh program info for all channels in parallel. Called on its own schedule."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as executor:
        for ch in channels:
            if not ch.youtube.startswith("@"):
                is_live = bool(live_state and live_state.get(ch.id))
                executor.submit(_fetch_program, ch.youtube, is_live)


def _program_window(prog: dict, now: int) -> tuple[int, int]:
    """Return (start, end) for a live programme slot.

    Start is the actual stream start time from YouTube metadata.
    End extends LIVE_SLOT_HOURS forward so the slot always covers now.
    """
    raw_start = prog.get("start_time", now) if prog else now
    start = min(raw_start, now)  # never in the future
    end = now + LIVE_SLOT_HOURS * 3600
    return start, end


def build_guide_json(config, live_state: dict) -> list:
    """Build HDHR-format guide.json consumed natively by Jellyfin/Plex."""
    now = int(time.time())
    entries = []

    for ch in config.channels:
        prog = program_info.get(ch.youtube, {})
        is_live = bool(live_state.get(ch.id))
        start, end = _program_window(prog, now)

        entry = {
            "GuideNumber": str(ch.id),
            "GuideName": ch.name,
        }
        logo = _logo_cache.get(ch.youtube)
        if logo:
            entry["ImageURL"] = logo

        if is_live:
            # If cached data was fetched while offline, kick off a live refresh in background
            if not prog.get("is_live"):
                threading.Thread(target=force_refresh_program,
                                 args=(ch.youtube,), kwargs={"is_live": True}, daemon=True).start()
            entry["Guide"] = [{
                "StartTime": start,
                "EndTime": end,
                "Title": f"{ch.name}: {prog.get('title', ch.name)}",
                "Synopsis": prog.get("description", ""),
                "ImageURL": prog.get("thumbnail", ""),
            }]
        entries.append(entry)

    for i, grp in enumerate(config.groups, 1):
        prog = {}
        logo = None
        active_member = None
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                active_member = member
                prog = program_info.get(member.youtube, {})
                logo = _logo_cache.get(member.youtube)
                break

        is_live = active_member is not None
        guide_name = f"{grp.name} • {active_member.name}" if active_member else grp.name

        entry = {
            "GuideNumber": str(i),
            "GuideName": guide_name,
        }
        if logo:
            entry["ImageURL"] = logo

        if is_live:
            start, end = _program_window(prog, now)
            prog_title = prog.get("title", active_member.name)
            entry["Guide"] = [{
                "StartTime": start,
                "EndTime": end,
                "Title": f"{grp.name} | {active_member.name}: {prog_title}",
                "Synopsis": prog.get("description", ""),
                "ImageURL": prog.get("thumbnail", ""),
            }]
        entries.append(entry)

    return entries


def build_xmltv(config, live_state: dict) -> str:
    root = ET.Element("tv", attrib={"generator-info-name": "YoutubeLiveHDHR"})

    # Groups first (channels 1, 2, 3…)
    for i, grp in enumerate(config.groups, 1):
        el = ET.SubElement(root, "channel", id=str(i))
        ET.SubElement(el, "display-name").text = grp.name
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                logo = _logo_cache.get(member.youtube)
                if logo:
                    ET.SubElement(el, "icon", src=logo)
                break

    for ch in config.channels:
        el = ET.SubElement(root, "channel", id=str(ch.id))
        ET.SubElement(el, "display-name").text = ch.name
        logo = _logo_cache.get(ch.youtube)
        if logo:
            ET.SubElement(el, "icon", src=logo)

    now = int(time.time())
    for ch in config.channels:
        prog = program_info.get(ch.youtube, {})
        is_live = bool(live_state.get(ch.id))
        if is_live:
            start, end = _program_window(prog, now)
            _add_programme(root, str(ch.id), f"{ch.name}: {prog.get('title', ch.name)}",
                           prog.get("description", ""), prog.get("thumbnail", ""), start, end)

    for i, grp in enumerate(config.groups, 1):
        prog = {}
        active_member = None
        for cid in grp.channels:
            member = next((c for c in config.channels if c.id == cid), None)
            if member and live_state.get(cid):
                active_member = member
                prog = program_info.get(member.youtube, {})
                break

        if active_member:
            start, end = _program_window(prog, now)
            title = f"{grp.name} | {active_member.name}: {prog.get('title', active_member.name)}"
            _add_programme(root, str(i), title,
                           prog.get("description", ""), prog.get("thumbnail", ""), start, end)

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _add_programme(root, channel_id: str, title: str, description: str, thumbnail: str,
                   start: int, stop: int):
    fmt = "%Y%m%d%H%M%S +0000"
    el = ET.SubElement(root, "programme",
                       start=time.strftime(fmt, time.gmtime(start)),
                       stop=time.strftime(fmt, time.gmtime(stop)),
                       channel=channel_id)
    ET.SubElement(el, "title").text = title
    if description:
        ET.SubElement(el, "desc").text = description
    if thumbnail:
        ET.SubElement(el, "icon", src=thumbnail)
    ET.SubElement(el, "category").text = "Live"
