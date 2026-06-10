from typing import Optional
from pydantic import BaseModel


class FallbackConfig(BaseModel):
    type: str  # "playlist" or "url"
    url: str


class ChannelConfig(BaseModel):
    id: int
    name: str
    youtube: str
    fallback: Optional[FallbackConfig] = None


class GroupConfig(BaseModel):
    id: int
    name: str
    channels: list[int]
    fallback: Optional[FallbackConfig] = None


class AppConfig(BaseModel):
    channels: list[ChannelConfig] = []
    groups: list[GroupConfig] = []
