import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.models import AppConfig
from app.resolver import get_live_stream_url

logger = logging.getLogger(__name__)

# channel_id -> live stream URL (or None if not live)
live_state: dict[int, Optional[str]] = {}

_scheduler: Optional[BackgroundScheduler] = None
_config: Optional[AppConfig] = None


def _poll():
    if _config is None:
        return

    for channel in _config.channels:
        url = get_live_stream_url(channel.youtube)
        prev = live_state.get(channel.id)
        live_state[channel.id] = url
        if url != prev:
            status = "LIVE" if url else "offline"
            logger.info("Channel %s (%s): %s", channel.id, channel.name, status)

    # Groups point to whichever member channel is live (priority order)
    for group in _config.groups:
        live_state[group.id] = None
        for cid in group.channels:
            if live_state.get(cid):
                live_state[group.id] = live_state[cid]
                logger.info("Group %s using channel %s", group.id, cid)
                break


def start(config: AppConfig, interval_seconds: int = 60):
    global _scheduler, _config
    _config = config

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
