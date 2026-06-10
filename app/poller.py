import logging
import threading
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import _resolve_handle_to_uc
from app import guide
from app.models import AppConfig
from app.resolver import is_channel_live

logger = logging.getLogger(__name__)

# channel_id -> live stream URL (or None if not live)
live_state: dict[int, Optional[str]] = {}

_scheduler: Optional[BackgroundScheduler] = None
_config: Optional[AppConfig] = None


def _poll():
    if _config is None:
        return

    for channel in _config.channels:
        uc_id = channel.youtube
        if uc_id.startswith("@"):
            resolved = _resolve_handle_to_uc(uc_id)
            if resolved:
                channel.youtube = resolved
                logger.info("Late-resolved %s -> %s", uc_id, resolved)
            else:
                logger.warning("Still cannot resolve %s, skipping poll", uc_id)
                continue
        is_live = is_channel_live(channel.youtube)
        was_live = live_state.get(channel.id, False)
        live_state[channel.id] = is_live
        if is_live != was_live:
            logger.info("Channel %s (%s): %s", channel.id, channel.name, "LIVE" if is_live else "offline")
        if is_live:
            guide.maybe_refresh_program(channel.youtube)

    # Groups point to whichever member channel is live (priority order)
    for group in _config.groups:
        prev_active = live_state.get(group.id)
        active_cid = next((cid for cid in group.channels if live_state.get(cid)), None)
        live_state[group.id] = active_cid
        if active_cid != prev_active:
            logger.info("Group %s now using channel %s", group.id, active_cid)


def _fetch_logos(channels):
    for ch in channels:
        if not ch.youtube.startswith("@"):
            guide.fetch_channel_logo(ch.youtube)


def start(config: AppConfig, interval_seconds: int = 60):
    global _scheduler, _config
    _config = config

    threading.Thread(target=_fetch_logos, args=(config.channels,), daemon=True).start()

    _poll()  # immediate first run

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_poll, "interval", seconds=interval_seconds, id="poll_live")
    _scheduler.start()
    logger.info("Poller started, interval=%ss", interval_seconds)


def reload(config: AppConfig):
    global _config
    _config = config
    logger.info("Poller config reloaded")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
