from __future__ import annotations

from cardrag.domain import Issuer

from .base import IssuerAdapter
from .kb import KBAdapter
from .shinhan import ShinhanAdapter
from .woori import WooriAdapter


def adapter_for(issuer: Issuer, *, expected_minimum: int = 1) -> IssuerAdapter:
    adapters: dict[Issuer, IssuerAdapter] = {
        Issuer.WOORI: WooriAdapter(expected_minimum=expected_minimum),
        Issuer.KB: KBAdapter(expected_minimum=expected_minimum),
        Issuer.SHINHAN: ShinhanAdapter(expected_minimum=expected_minimum),
    }
    return adapters[issuer]
