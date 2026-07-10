"""Polymarket geographic eligibility preflight."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True, slots=True)
class GeoStatus:
    blocked: bool
    country: str
    region: str
    ip: str


def _fetch_geoblock() -> GeoStatus:
    request = urllib.request.Request(
        GEOBLOCK_URL,
        headers={"Accept": "application/json", "User-Agent": "polytracker/0.1"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    return GeoStatus(
        blocked=bool(payload["blocked"]),
        country=str(payload.get("country", "")),
        region=str(payload.get("region", "")),
        ip=str(payload.get("ip", "")),
    )


async def check_geoblock() -> GeoStatus:
    return await asyncio.to_thread(_fetch_geoblock)
