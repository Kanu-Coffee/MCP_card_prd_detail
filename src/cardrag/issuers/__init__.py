"""Versioned issuer adapter contract and supported implementations."""

from .base import DiscoveryMode, IssuerAdapter, SourceRecord, SourceSnapshot
from .kb import KBAdapter
from .shinhan import ShinhanAdapter
from .woori import WooriAdapter

__all__ = [
    "DiscoveryMode",
    "IssuerAdapter",
    "KBAdapter",
    "ShinhanAdapter",
    "SourceRecord",
    "SourceSnapshot",
    "WooriAdapter",
]
