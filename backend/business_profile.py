"""Business profile — detailed company onboarding stored in Mem9 per Telegram user."""

from __future__ import annotations

import json
import re
from typing import Any

from backend import mem9_client

PROFILE_TAG = "business_profile"
_profile_cache: dict[str, dict[str, Any]] = {}

REQUIRED_FIELDS = (
    "legal_entity_type",
    "legal_name",
    "business_name",
    "business_type",
    "business_address",
    "city",
    "budget",
)


def _user_tag(chat_id: int | str) -> str:
    return f"telegram_{chat_id}"


def cache_profile(chat_id: int | str, profile: dict[str, Any]) -> None:
    _profile_cache[str(chat_id)] = profile


def is_profile_complete(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    for key in REQUIRED_FIELDS:
        if not str(profile.get(key) or "").strip():
            return False
    return True


def save_profile(chat_id: int | str, profile: dict[str, Any]) -> dict[str, Any]:
    payload = {**profile, "chat_id": str(chat_id)}
    cache_profile(chat_id, payload)
    loc = ""
    if profile.get("latitude") is not None:
        loc = f" Location: {profile.get('latitude')},{profile.get('longitude')} ({profile.get('location_label', '')})."
    content = (
        f"COOPilot business profile (telegram {chat_id}): "
        f"Entity {profile.get('legal_entity_type')} '{profile.get('legal_name')}'. "
        f"Brand '{profile.get('business_name')}' ({profile.get('business_type')}). "
        f"Address: {profile.get('business_address')}, {profile.get('city')}. "
        f"NPWP: {profile.get('npwp', '-')}. Owner: {profile.get('owner_name', '-')}. "
        f"Budget Rp {profile.get('budget')}. Target: {profile.get('target_goal')}. "
        f"Preferences: {profile.get('operational_preference')}.{loc}"
    )
    return mem9_client.add_memory(
        content,
        tags=["coopilot", PROFILE_TAG, _user_tag(chat_id)],
        metadata={"profile_json": json.dumps(payload, ensure_ascii=False), "chat_id": str(chat_id)},
        sync=True,
    )


def save_location(chat_id: int | str, lat: float, lon: float, label: str = "") -> dict[str, Any]:
    profile = load_profile(chat_id) or {"chat_id": str(chat_id)}
    profile["latitude"] = lat
    profile["longitude"] = lon
    profile["location_label"] = label
    return save_profile(chat_id, profile)


def _parse_profile_from_content(content: str) -> dict[str, Any] | None:
    if "Brand '" not in content and "Business '" not in content:
        return None
    name_m = re.search(r"Brand '([^']+)'", content) or re.search(r"Business '([^']+)'", content)
    if not name_m:
        return None
    return {"business_name": name_m.group(1).strip()}


def load_profile(chat_id: int | str) -> dict[str, Any] | None:
    cached = _profile_cache.get(str(chat_id))
    if cached and cached.get("business_name"):
        return cached

    if not mem9_client.is_configured():
        return None

    try:
        hits = mem9_client.search_memory("", tags=[_user_tag(chat_id), PROFILE_TAG], limit=10)
        memories = hits.get("memories") or []
        if not memories:
            hits = mem9_client.search_memory(f"business profile telegram {chat_id}", limit=10)
            memories = hits.get("memories") or []

        for m in memories:
            meta = m.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            raw = meta.get("profile_json")
            if raw:
                profile = json.loads(raw)
                cache_profile(chat_id, profile)
                return profile
            parsed = _parse_profile_from_content(m.get("content") or "")
            if parsed and parsed.get("business_name"):
                cache_profile(chat_id, parsed)
                return parsed
        return None
    except Exception:
        return _profile_cache.get(str(chat_id))


def has_profile(chat_id: int | str) -> bool:
    return is_profile_complete(load_profile(chat_id))


def build_business_context(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "Perusahaan belum terdaftar"
    if profile.get("raw_memory"):
        return profile["raw_memory"]
    loc = ""
    if profile.get("latitude") is not None:
        loc = f" Lokasi: {profile.get('location_label') or profile.get('latitude')},{profile.get('longitude')}."
    return (
        f"{profile.get('legal_entity_type')} {profile.get('legal_name')} — "
        f"brand {profile.get('business_name')} ({profile.get('business_type')}). "
        f"Alamat: {profile.get('business_address')}, {profile.get('city')}."
        f"{loc} Budget Rp {profile.get('budget')}. Target: {profile.get('target_goal')}. "
        f"Prefs: {profile.get('operational_preference')}."
    )


def format_profile_summary(profile: dict[str, Any]) -> str:
    lines = [
        f"*Badan usaha:* {profile.get('legal_entity_type', '-')}",
        f"*Nama legal:* {profile.get('legal_name', '-')}",
        f"*Brand:* {profile.get('business_name', '-')}",
        f"*Industri:* {profile.get('business_type', '-')}",
        f"*Alamat:* {profile.get('business_address', '-')}, {profile.get('city', '-')}",
        f"*NPWP:* {profile.get('npwp') or '-'}",
        f"*Pemilik:* {profile.get('owner_name') or '-'}",
        f"*Modal:* Rp {int(profile.get('budget', 0) or 0):,}",
        f"*Target:* {profile.get('target_goal', '-')}",
    ]
    if profile.get("latitude") is not None:
        lines.append(f"*Lokasi GPS:* {profile.get('location_label') or 'tercatat'}")
    return "\n".join(lines)
