"""Send Telegram messages to other users (vendor invoice, notifications)."""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def is_configured() -> bool:
    return bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip())


def send_message(
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_preview: bool = True,
) -> dict[str, Any]:
    """Send via Bot API. Recipient must have started this bot at least once."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    payload: dict[str, Any] = {
        "chat_id": int(chat_id),
        "text": text[:4096],
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        desc = data.get("description", r.text[:300])
        raise RuntimeError(f"Telegram send failed: {desc}")
    return data.get("result") or {}


def format_invoice_message(
    *,
    business_name: str,
    vendor_name: str,
    products: str,
    invoice_id: str,
    amount: int,
    payment_url: str,
    vendor_doku_id: str = "",
) -> str:
    lines = [
        f"📄 Invoice dari {business_name}",
        f"Kepada: {vendor_name}",
        f"Item: {products}",
        f"Invoice: {invoice_id}",
        f"Nominal: Rp {amount:,}",
    ]
    if vendor_doku_id:
        lines.append(f"DOKU Vendor ID: {vendor_doku_id}")
    if payment_url:
        lines.append(f"Link pembayaran: {payment_url}")
    lines.append("\nMohon konfirmasi penerimaan. — COOPilot AI")
    return "\n".join(lines)
