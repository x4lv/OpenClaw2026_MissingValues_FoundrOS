"""Registered suppliers per Telegram company — only these may be paid."""

from __future__ import annotations

import json
import uuid
from typing import Any

from backend import mem9_client
from backend.business_profile import _user_tag

SUPPLIER_TAG = "supplier_registry"
_supplier_cache: dict[str, list[dict[str, Any]]] = {}


def _cache_key(chat_id: int | str) -> str:
    return str(chat_id)


def cache_suppliers(chat_id: int | str, suppliers: list[dict[str, Any]]) -> None:
    _supplier_cache[_cache_key(chat_id)] = suppliers


def supplier_from_discovery(item: dict[str, Any]) -> dict[str, Any]:
    """Map OSM discovery row to registrable supplier draft."""
    return {
        "name": item.get("name", ""),
        "contact_person": item.get("name", ""),
        "phone_wa": item.get("phone") or "",
        "address": item.get("address", ""),
        "products": item.get("shop_type", "bahan/supply"),
        "default_monthly_amount": 0,
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        "source": "recommended",
        "osm_id": item.get("osm_id"),
    }


def save_supplier(chat_id: int | str, supplier: dict[str, Any]) -> dict[str, Any]:
    supplier = {
        **supplier,
        "supplier_id": supplier.get("supplier_id") or f"SUP-{uuid.uuid4().hex[:8].upper()}",
        "chat_id": str(chat_id),
        "source": supplier.get("source") or "registered",
    }
    content = (
        f"COOPilot registered supplier (telegram {chat_id}): "
        f"Name={supplier.get('name')}; Contact={supplier.get('contact_person')}; "
        f"WA={supplier.get('phone_wa')}; Telegram={supplier.get('telegram_id', '-')}; "
        f"DOKU={supplier.get('doku_id', '-')}; Address={supplier.get('address', '-')}; "
        f"Products={supplier.get('products')}; "
        f"DefaultAmount=Rp {supplier.get('default_monthly_amount')}."
    )
    mem9_client.add_memory(
        content,
        tags=["coopilot", SUPPLIER_TAG, _user_tag(chat_id), f"sup_{supplier['supplier_id']}"],
        metadata={"supplier_json": json.dumps(supplier, ensure_ascii=False), "chat_id": str(chat_id)},
        sync=True,
    )
    existing = list_suppliers(chat_id, use_cache=False)
    existing = [s for s in existing if s.get("supplier_id") != supplier["supplier_id"]]
    existing.append(supplier)
    cache_suppliers(chat_id, existing)
    return supplier


def _parse_supplier(meta: dict, content: str) -> dict[str, Any] | None:
    raw = meta.get("supplier_json") if meta else None
    if raw:
        return json.loads(raw)
    return None


def list_suppliers(chat_id: int | str, *, use_cache: bool = True) -> list[dict[str, Any]]:
    key = _cache_key(chat_id)
    if use_cache and key in _supplier_cache:
        return list(_supplier_cache[key])

    if not mem9_client.is_configured():
        return _supplier_cache.get(key, [])

    suppliers: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        hits = mem9_client.search_memory("", tags=[_user_tag(chat_id), SUPPLIER_TAG], limit=50)
        for m in hits.get("memories") or []:
            meta = m.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            sup = _parse_supplier(meta, m.get("content") or "")
            if sup and sup.get("name") and sup.get("supplier_id") not in seen:
                seen.add(sup["supplier_id"])
                suppliers.append(sup)
    except Exception:
        pass

    cache_suppliers(chat_id, suppliers)
    return suppliers


def has_suppliers(chat_id: int | str) -> bool:
    return len(list_suppliers(chat_id)) > 0


def find_supplier(chat_id: int | str, name_query: str) -> dict[str, Any] | None:
    q = (name_query or "").strip().lower()
    if not q:
        return None
    for s in list_suppliers(chat_id):
        if q in (s.get("name") or "").lower():
            return s
    return None


def format_supplier_list(chat_id: int | str) -> str:
    suppliers = list_suppliers(chat_id)
    if not suppliers:
        return "Belum ada supplier terdaftar."
    lines = []
    for i, s in enumerate(suppliers, 1):
        src = s.get("source", "registered")
        extra = []
        if s.get("address"):
            extra.append(f"📍 {s['address'][:60]}")
        if s.get("telegram_id"):
            extra.append(f"TG: `{s['telegram_id']}`")
        if s.get("doku_id"):
            extra.append(f"DOKU: `{s['doku_id']}`")
        detail = "\n   ".join(extra)
        lines.append(
            f"{i}. *{s.get('name')}* [{src}] — {s.get('products', '-')}\n"
            f"   WA: {s.get('phone_wa') or '-'} | Rp {int(s.get('default_monthly_amount', 0)):,}/bulan"
            + (f"\n   {detail}" if detail else "")
        )
    return "\n".join(lines)
