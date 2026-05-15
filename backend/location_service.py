"""Reverse geocoding via OpenStreetMap Nominatim (no API key)."""

from __future__ import annotations

from typing import Any

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "COOPilotAI/1.0 (hackathon; contact@coopilot.local)"


def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    r = requests.get(
        NOMINATIM_URL,
        params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    addr = data.get("address") or {}
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
        or ""
    )
    return {
        "display_name": data.get("display_name", ""),
        "city": city,
        "province": addr.get("state") or addr.get("region") or "",
        "road": addr.get("road") or "",
        "suburb": addr.get("suburb") or addr.get("neighbourhood") or "",
    }
