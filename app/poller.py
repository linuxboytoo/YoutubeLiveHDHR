import json as _json
import logging
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import _resolve_handle_to_uc
from app import guide
from app.models import AppConfig, ChannelConfig
from app.resolver import is_channel_live

logger = logging.getLogger(__name__)

live_state: dict[int, Optional[int]] = {}

_scheduler: Optional[BackgroundScheduler] = None
_config: Optional[AppConfig] = None

JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")

_guide_task_id: Optional[str] = None


def _find_guide_task_id() -> Optional[str]:
    """Look up Jellyfin's RefreshGuide scheduled task ID (cached after first lookup)."""
    global _guide_task_id
    if _guide_task_id:
        return _guide_task_id
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return None
    try:
        req = urllib.request.Request(
            f"{JELLYFIN_URL}/ScheduledTasks",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            tasks = _json.loads(resp.read())
        for task in tasks:
            name = task.get("Name", "").lower()
            if "guide" in name or task.get("Key") == "RefreshGuide":
                _guide_task_id = task["Id"]
                logger.info("Found Jellyfin guide task: %s (%s)", task.get("Name"), _guide_task_id)
                return _guide_task_id
    except Exception as e:
        logger.debug("Could not find Jellyfin guide task: %s", e)
    return None


def _trigger_jellyfin_guide_refresh():
    """Tell Jellyfin to re-fetch guide data from our endpoints."""
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return
    task_id = _find_guide_task_id()
    if not task_id:
        return
    try:
        req = urllib.request.Request(
            f"{JELLYFIN_URL}/ScheduledTasks/Running/{task_id}",
            method="POST",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            data=b"",
        )
        urllib.request.urlopen(req, timeout=10).close()
        logger.info("Triggered Jellyfin guide refresh")
    except Exception as e:
        logger.warning("Jellyfin guide refresh trigger failed: %s", e)


def _check_channel(channel: ChannelConfig) -> tuple[ChannelConfig, bool]:
    uc_id = channel.youtube
    if uc_id.startswith("@"):
        resolved = _resolve_handle_to_uc(uc_id)
        if resolved:
            channel.youtube = resolved
            logger.info("Late-resolved %s -> %s", uc_id, resolved)
        else:
            logger.warning("Still cannot resolve %s, skipping poll", uc_id)
            return channel, False
    return channel, is_channel_live(channel.youtube)


def _poll():
    if _config is None:
        return

    any_changed = False

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_check_channel, ch): ch for ch in _config.channels}
        for future in as_completed(futures):
            try:
                channel, is_live = future.result()
            except Exception as e:
                logger.warning("Poll error: %s", e)
                continue
            was_live = live_state.get(channel.id, False)
            live_state[channel.id] = is_live
            if is_live != was_live:
                any_changed = True
                logger.info("Channel %s (%s): %s", channel.id, channel.name, "LIVE" if is_live else "offline")
                threading.Thread(target=guide.force_refresh_program,
                                 args=(channel.youtube,),
                                 kwargs={"is_live": is_live}, daemon=True).start()

    # Groups: pick highest-priority live member
    for group in _config.groups:
        prev_active = live_state.get(group.id)
        active_cid = next((cid for cid in group.channels if live_state.get(cid)), None)
        live_state[group.id] = active_cid
        if active_cid != prev_active:
            logger.info("Group %s now using channel %s", group.id, active_cid)

    if any_changed:
        threading.Thread(target=_trigger_jellyfin_guide_refresh, daemon=True).start()


def _fetch_logos(channels):
    for ch in channels:
        if not ch.youtube.startswith("@"):
            guide.fetch_channel_logo(ch.youtube)


def start(config: AppConfig, interval_seconds: int = 60):
    global _scheduler, _config
    _config = config

    threading.Thread(target=_fetch_logos, args=(config.channels,), daemon=True).start()
    threading.Thread(target=guide.refresh_all_programs, args=(config.channels, live_state), daemon=True).start()

    _poll()  # immediate first run

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_poll, "interval", seconds=interval_seconds, id="poll_live")
    _scheduler.add_job(lambda: guide.refresh_all_programs(_config.channels, live_state),
                       "interval", minutes=15, id="poll_guide")
    _scheduler.start()
    logger.info("Poller started, interval=%ss", interval_seconds)


def reload(config: AppConfig):
    global _config
    _config = config
    logger.info("Poller config reloaded")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
