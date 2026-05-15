"""
COOPilot Telegram — profil perusahaan spesifik, lokasi, rekomendasi supplier OSM,
vendor tetap + Telegram invoice ke vendor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from backend.business_profile import (
    build_business_context,
    cache_profile,
    format_profile_summary,
    has_profile,
    is_profile_complete,
    load_profile,
    save_location,
    save_profile,
)
from backend.intent_router import UserIntent, detect_intent
from backend.orchestrator import run_goal_flow
from backend import supplier_discovery
from backend.location_service import reverse_geocode
from backend.supplier_registry import (
    find_supplier,
    format_supplier_list,
    has_suppliers,
    list_suppliers,
    save_supplier,
    supplier_from_discovery,
)
from channels.formatters import escape_md, format_feed_line, format_workflow_summary

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("coopilot.telegram")

# Company onboarding (detailed)
(
    BIZ_LEGAL_TYPE,
    BIZ_LEGAL_NAME,
    BIZ_NAME,
    BIZ_TYPE,
    BIZ_ADDRESS,
    BIZ_CITY,
    BIZ_NPWP,
    OWNER_NAME,
    MODAL,
    TARGET,
    PREFS,
    BIZ_LOCATION,
) = range(12)

# Supplier onboarding
SUP_NAME, SUP_CONTACT, SUP_PHONE, SUP_ADDRESS, SUP_TELEGRAM, SUP_DOKU, SUP_PRODUCTS, SUP_AMOUNT = range(
    20, 28
)

FEED_DELAY_SEC = float(os.getenv("TELEGRAM_FEED_DELAY", "0.45"))
LOCATION_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Kirim lokasi kantor/usaha", request_location=True)], ["Lewati lokasi"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def _token() -> str:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in coopilot/.env")
    return token


def _get_profile(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return load_profile(chat_id) or context.user_data.get("profile")


def _ready(chat_id: int) -> tuple[bool, str]:
    if not has_profile(chat_id):
        return False, "Profil perusahaan belum lengkap. Ketik /setup"
    if not has_suppliers(chat_id):
        return False, "Belum ada supplier. /tambah_supplier atau /cari_supplier lalu /simpan_vendor"
    return True, ""


def _user_location(profile: dict | None) -> tuple[float, float] | None:
    if not profile:
        return None
    lat, lon = profile.get("latitude"), profile.get("longitude")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


async def _safe_reply(message, text: str, **kwargs) -> None:
    try:
        await message.reply_text(text, **kwargs)
    except Exception:
        await message.reply_text(re.sub(r"[*_`\[]", "", text))


# --- Company onboarding ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    context.user_data.pop("in_onboarding", None)

    profile = _get_profile(chat_id, context)
    if is_profile_complete(profile):
        name = profile.get("business_name", "bisnis Anda")
        msg = (
            f"Halo! COOPilot untuk *{escape_md(name)}*.\n\n"
            "/tambah_supplier — vendor tetap\n"
            "/cari_supplier — top 5 supplier terdekat (butuh lokasi)\n"
            "/daftar_supplier — lihat vendor\n"
            "/bayar <nama> — pembayaran DOKU\n"
            "/kirim_invoice <nama> — invoice ke Telegram vendor\n"
            "/lokasi — update GPS kantor\n"
            "/rencana | /profile | /setup | /help"
        )
        if not has_suppliers(chat_id):
            msg += "\n\n⚠️ Daftarkan vendor sebelum /bayar."
        await _safe_reply(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    context.user_data["in_onboarding"] = True
    context.user_data["onboarding"] = {}
    await _safe_reply(
        update.message,
        "Selamat datang di *COOPilot AI*.\n\n"
        "Setup perusahaan (1/12): *Jenis badan usaha*?\n"
        "(PT / CV / UD / Perorangan)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BIZ_LEGAL_TYPE


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["in_onboarding"] = True
    context.user_data["onboarding"] = {}
    await _safe_reply(
        update.message,
        "Setup ulang perusahaan.\n*Jenis badan usaha*? (PT/CV/UD/Perorangan)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BIZ_LEGAL_TYPE


async def onboard_legal_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["legal_entity_type"] = update.message.text.strip()
    await update.message.reply_text("Nama legal resmi perusahaan? (sesuai akta/NPWP)")
    return BIZ_LEGAL_NAME


async def onboard_legal_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["legal_name"] = update.message.text.strip()
    await update.message.reply_text("Nama brand / nama dagang yang dipakai sehari-hari?")
    return BIZ_NAME


async def onboard_biz_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["business_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Industri spesifik? (contoh: coffee shop online, distributor bahan kue)"
    )
    return BIZ_TYPE


async def onboard_biz_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["business_type"] = update.message.text.strip()
    await update.message.reply_text("Alamat lengkap kantor/usaha? (jalan, nomor, kelurahan)")
    return BIZ_ADDRESS


async def onboard_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["business_address"] = update.message.text.strip()
    await update.message.reply_text("Kota?")
    return BIZ_CITY


async def onboard_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["city"] = update.message.text.strip()
    await update.message.reply_text("NPWP perusahaan? (ketik - jika belum ada)")
    return BIZ_NPWP


async def onboard_npwp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["onboarding"]["npwp"] = "" if raw in ("-", "skip", "lewati") else raw
    await update.message.reply_text("Nama pemilik / penanggung jawab?")
    return OWNER_NAME


async def onboard_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["owner_name"] = update.message.text.strip()
    await update.message.reply_text("Modal operasional bulanan (angka, contoh: 2000000):")
    return MODAL


async def onboard_modal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = re.sub(r"[^\d]", "", update.message.text)
    if not raw:
        await update.message.reply_text("Masukkan angka modal, contoh: 2000000")
        return MODAL
    context.user_data["onboarding"]["budget"] = raw
    context.user_data["onboarding"]["modal"] = raw
    await update.message.reply_text("Target bisnis utama?")
    return TARGET


async def onboard_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["target_goal"] = update.message.text.strip()
    await update.message.reply_text("Preferensi operasional? (contoh: supplier terdekat & hemat)")
    return PREFS


async def onboard_prefs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboarding"]["operational_preference"] = update.message.text.strip()
    await _safe_reply(
        update.message,
        "Langkah terakhir: kirim *lokasi GPS* kantor/usaha untuk rekomendasi supplier terdekat.\n"
        "Tombol di bawah atau ketik *Lewati lokasi*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=LOCATION_KEYBOARD,
    )
    return BIZ_LOCATION


async def _finish_company_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = context.user_data.get("onboarding", {})
    chat_id = update.effective_chat.id
    try:
        save_profile(chat_id, profile)
        context.user_data["profile"] = profile
        mem_note = "Profil perusahaan tersimpan (Mem9)."
    except Exception as e:
        cache_profile(chat_id, profile)
        context.user_data["profile"] = profile
        mem_note = f"Profil cache lokal: {e}"

    context.user_data.pop("in_onboarding", None)
    await _safe_reply(
        update.message,
        f"✅ Profil perusahaan selesai!\n"
        f"*{escape_md(profile['legal_name'])}* / {escape_md(profile['business_name'])}\n"
        f"{escape_md(profile['business_address'])}, {escape_md(profile['city'])}\n"
        f"Modal: Rp {int(profile['budget']):,}\n{mem_note}\n\n"
        "Selanjutnya:\n"
        "• /tambah_supplier — vendor tetap (+ Telegram ID & DOKU vendor)\n"
        "• /cari_supplier — rekomendasi 5 supplier terdekat",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def onboard_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if loc:
        lat, lon = loc.latitude, loc.longitude
        try:
            geo = reverse_geocode(lat, lon)
            label = geo.get("display_name", "")[:200]
        except Exception:
            label = f"{lat:.5f}, {lon:.5f}"
        context.user_data["onboarding"]["latitude"] = lat
        context.user_data["onboarding"]["longitude"] = lon
        context.user_data["onboarding"]["location_label"] = label
    else:
        text = (update.message.text or "").strip().lower()
        if text not in ("lewati lokasi", "lewati", "skip", "-"):
            await update.message.reply_text(
                "Kirim lokasi dengan tombol 📍 atau ketik *Lewati lokasi*.",
                reply_markup=LOCATION_KEYBOARD,
            )
            return BIZ_LOCATION

    return await _finish_company_profile(update, context)


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("in_onboarding", None)
    context.user_data.pop("adding_supplier", None)
    await update.message.reply_text("Dibatalkan.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --- Supplier onboarding ---


async def cmd_tambah_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if not has_profile(chat_id):
        await update.message.reply_text("Setup perusahaan dulu: /setup")
        return ConversationHandler.END

    context.user_data["adding_supplier"] = True
    context.user_data["new_supplier"] = {"source": "registered"}
    await _safe_reply(
        update.message,
        "📦 *Vendor tetap* (1/8)\nNama supplier / perusahaan?",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SUP_NAME


async def sup_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_supplier"]["name"] = update.message.text.strip()
    await update.message.reply_text("Nama contact person di supplier?")
    return SUP_CONTACT


async def sup_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_supplier"]["contact_person"] = update.message.text.strip()
    await update.message.reply_text("Nomor WhatsApp supplier? (contoh: 62812...)")
    return SUP_PHONE


async def sup_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = re.sub(r"[^\d+]", "", update.message.text.strip())
    if len(phone) < 10:
        await update.message.reply_text("Nomor tidak valid. Contoh: 62812345678")
        return SUP_PHONE
    context.user_data["new_supplier"]["phone_wa"] = phone
    await update.message.reply_text("Alamat lengkap supplier?")
    return SUP_ADDRESS


async def sup_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_supplier"]["address"] = update.message.text.strip()
    await update.message.reply_text(
        "Telegram User ID vendor? (angka, dari @userinfobot — ketik - jika belum ada)\n"
        "Vendor harus pernah /start bot ini agar invoice terkirim."
    )
    return SUP_TELEGRAM


async def sup_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw not in ("-", "skip", "lewati"):
        tid = re.sub(r"[^\d]", "", raw)
        if tid:
            context.user_data["new_supplier"]["telegram_id"] = tid
    await update.message.reply_text(
        "Nomor/ID DOKU vendor (wallet/merchant ID)? Ketik - jika belum ada."
    )
    return SUP_DOKU


async def sup_doku(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw not in ("-", "skip", "lewati"):
        context.user_data["new_supplier"]["doku_id"] = raw
    await update.message.reply_text("Produk / bahan baku dari supplier ini?")
    return SUP_PRODUCTS


async def sup_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_supplier"]["products"] = update.message.text.strip()
    await update.message.reply_text("Nominal pembayaran rutin per bulan (angka, contoh: 500000):")
    return SUP_AMOUNT


async def sup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = re.sub(r"[^\d]", "", update.message.text)
    if not raw:
        await update.message.reply_text("Masukkan angka, contoh: 500000")
        return SUP_AMOUNT

    chat_id = update.effective_chat.id
    supplier = context.user_data.get("new_supplier", {})
    supplier["default_monthly_amount"] = int(raw)

    try:
        save_supplier(chat_id, supplier)
        note = "Supplier tersimpan ke Mem9."
    except Exception as e:
        note = f"Tersimpan lokal: {e}"

    context.user_data.pop("adding_supplier", None)
    tg = supplier.get("telegram_id", "-")
    doku = supplier.get("doku_id", "-")
    await _safe_reply(
        update.message,
        f"✅ Vendor terdaftar!\n"
        f"*{escape_md(supplier['name'])}*\n"
        f"📍 {escape_md(supplier.get('address', '-'))}\n"
        f"WA: `{supplier['phone_wa']}` | TG: `{tg}` | DOKU: `{doku}`\n"
        f"Nominal: Rp {supplier['default_monthly_amount']:,}\n{note}\n\n"
        f"/bayar {escape_md(supplier['name'])} | /kirim_invoice {escape_md(supplier['name'])}",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# --- Discovery & location commands ---


async def cmd_lokasi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not has_profile(update.effective_chat.id):
        await update.message.reply_text("Setup dulu: /setup")
        return
    context.user_data["awaiting_location_update"] = True
    await update.message.reply_text(
        "Kirim lokasi GPS baru kantor/usaha:",
        reply_markup=LOCATION_KEYBOARD,
    )


async def handle_location_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_location_update"):
        return
    loc = update.message.location
    if not loc:
        return
    chat_id = update.effective_chat.id
    try:
        geo = reverse_geocode(loc.latitude, loc.longitude)
        label = geo.get("display_name", "")[:200]
    except Exception:
        label = f"{loc.latitude:.5f}, {loc.longitude:.5f}"
    try:
        save_location(chat_id, loc.latitude, loc.longitude, label)
        note = "Lokasi tersimpan."
    except Exception as e:
        profile = _get_profile(chat_id, context) or {}
        profile.update(
            latitude=loc.latitude, longitude=loc.longitude, location_label=label
        )
        cache_profile(chat_id, profile)
        note = f"Cache: {e}"
    context.user_data.pop("awaiting_location_update", None)
    await update.message.reply_text(
        f"✅ Lokasi diperbarui.\n{label}\n{note}\nCoba /cari_supplier",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_cari_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return ConversationHandler.END when invoked from onboarding fallbacks."""
    chat_id = update.effective_chat.id
    context.user_data.pop("in_onboarding", None)
    context.user_data.pop("adding_supplier", None)

    if not has_profile(chat_id):
        await update.message.reply_text(
            "Profil perusahaan belum lengkap.\n"
            "Selesaikan /setup (atau /cancel lalu /setup ulang)."
        )
        return ConversationHandler.END

    profile = _get_profile(chat_id, context) or {}
    coords = _user_location(profile)
    if not coords:
        await update.message.reply_text(
            "📍 *Lokasi GPS belum ada* — /cari_supplier butuh titik kantor/usaha.\n\n"
            "Kirim lokasi: /lokasi\n"
            "(Jika tadi sempat ketuk pesan salah saat /setup, ketik /cancel lalu /setup)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    query = " ".join(context.args) if context.args else profile.get("business_type", "")
    await update.message.reply_text("🔍 Mencari supplier terdekat di peta OSM...")

    try:
        items = await asyncio.to_thread(
            supplier_discovery.find_nearby_suppliers,
            coords[0],
            coords[1],
            query=query,
            business_type=profile.get("business_type", ""),
            limit=5,
        )
        context.user_data["last_discovery"] = items
        text = supplier_discovery.format_recommendations(items)
        await _safe_reply(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("discovery failed")
        await update.message.reply_text(
            f"Gagal mencari supplier (OSM timeout/rate limit).\n"
            f"Coba lagi 1–2 menit atau /cari_supplier kopi\n\nDetail: {e}"
        )
    return ConversationHandler.END


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmd = (update.message.text or "").split()[0].lower()
    hints = {
        "/carisupplier": "/cari_supplier",
        "/carisup": "/cari_supplier",
        "/tambahsupplier": "/tambah_supplier",
        "/daftarsupplier": "/daftar_supplier",
        "/kiriminvoice": "/kirim_invoice",
    }
    fix = hints.get(cmd.split("@")[0])
    if fix:
        await update.message.reply_text(f"Maksud Anda {fix} ?\nCoba ketik perintah itu (pakai underscore _).")
    else:
        await update.message.reply_text(
            f"Perintah {cmd} tidak dikenal.\nKetik /help untuk daftar perintah."
        )


async def cmd_simpan_vendor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Gunakan: /simpan_vendor <nomor dari /cari_supplier>")
        return

    items = context.user_data.get("last_discovery") or []
    try:
        idx = int(context.args[0]) - 1
        picked = items[idx]
    except (ValueError, IndexError):
        await update.message.reply_text("Nomor tidak valid. Jalankan /cari_supplier dulu.")
        return

    draft = supplier_from_discovery(picked)
    await update.message.reply_text(
        f"Menyimpan *{escape_md(draft['name'])}*.\n"
        "Lengkapi nominal bulanan (angka):",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["pending_discovery_save"] = draft


async def handle_discovery_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("pending_discovery_save")
    if not draft:
        return
    raw = re.sub(r"[^\d]", "", update.message.text or "")
    if not raw:
        return
    draft["default_monthly_amount"] = int(raw)
    chat_id = update.effective_chat.id
    try:
        save_supplier(chat_id, draft)
        note = "Tersimpan. Tambahkan TG ID & DOKU via /tambah_supplier jika perlu."
    except Exception as e:
        note = str(e)
    context.user_data.pop("pending_discovery_save", None)
    await update.message.reply_text(
        f"✅ {draft['name']} masuk daftar vendor.\n{note}\n/bayar {draft['name']}"
    )


async def cmd_daftar_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not has_profile(chat_id):
        await update.message.reply_text("Setup perusahaan dulu: /setup")
        return
    text = format_supplier_list(chat_id)
    await _safe_reply(update.message, f"*Supplier terdaftar:*\n\n{text}", parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_reply(
        update.message,
        "*COOPilot — Autonomous Operations*\n\n"
        "*Perusahaan (spesifik):*\n"
        "/setup — badan usaha, nama legal, alamat, kota, modal, lokasi GPS\n\n"
        "*Vendor:*\n"
        "/tambah_supplier — vendor tetap + Telegram ID + DOKU vendor\n"
        "/cari_supplier [kategori] — top 5 terdekat (OSM)\n"
        "/simpan_vendor <no> — simpan dari rekomendasi\n\n"
        "*Pembayaran:*\n"
        "/bayar <nama> — workflow DOKU\n"
        "/kirim_invoice <nama> — invoice ke Telegram vendor\n\n"
        "/lokasi | /daftar_supplier | /profile | /cancel",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = _get_profile(update.effective_chat.id, context)
    if not is_profile_complete(profile):
        await update.message.reply_text("Profil belum lengkap. /setup")
        return
    await _safe_reply(
        update.message,
        f"*Profil perusahaan*\n{format_profile_summary(profile)}",
        parse_mode=ParseMode.MARKDOWN,
    )


def _resolve_supplier(chat_id: int, name_arg: str) -> dict | None:
    suppliers = list_suppliers(chat_id)
    if not suppliers:
        return None
    if name_arg:
        found = find_supplier(chat_id, name_arg)
        if found:
            return found
    if len(suppliers) == 1:
        return suppliers[0]
    return None


def _build_wf_context(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    goal: str,
    selected_supplier: dict | None,
) -> dict:
    profile = _get_profile(chat_id, context) or {}
    suppliers = list_suppliers(chat_id)
    wf: dict = {
        "chat_id": chat_id,
        "user_goal": goal,
        "profile": profile,
        "business_context": build_business_context(profile),
        "registered_suppliers": suppliers,
    }
    try:
        wf["budget_available"] = float(str(profile.get("budget", "0")).replace(".", ""))
    except (TypeError, ValueError):
        wf["budget_available"] = 0

    if selected_supplier:
        wf["selected_supplier"] = selected_supplier
        wf["vendor_name"] = selected_supplier.get("name")
        amt = int(selected_supplier.get("default_monthly_amount", 0))
        wf["payment_amount"] = amt if amt > 0 else wf.get("payment_amount", 0)
        wf["payment_allowed"] = False
    return wf


async def _run_workflow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    goal: str,
    intent: UserIntent,
    supplier_name: str = "",
) -> None:
    chat_id = update.effective_chat.id

    if not has_profile(chat_id):
        await update.message.reply_text("Profil belum lengkap. /setup")
        return

    if intent == UserIntent.VENDOR_PAYMENT:
        ok, msg = _ready(chat_id)
        if not ok:
            await update.message.reply_text(msg)
            return

        selected = _resolve_supplier(chat_id, supplier_name)
        if not selected:
            await _safe_reply(
                update.message,
                "Pilih supplier terdaftar:\n\n"
                f"{format_supplier_list(chat_id)}\n\n"
                "/bayar <nama> | /kirim_invoice <nama>",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        selected = None

    intent_label = {
        UserIntent.VENDOR_PAYMENT: "Vendor Payment",
        UserIntent.PLANNING: "Planning",
        UserIntent.MARKETING: "Marketing",
    }.get(intent, intent.value)

    status_msg = await update.message.reply_text(
        f"⏳ Workflow *{escape_md(intent_label)}* — "
        f"{escape_md((selected or {}).get('name', 'perusahaan'))}...",
        parse_mode=ParseMode.MARKDOWN,
    )

    wf_context = _build_wf_context(chat_id, context, goal, selected)
    try:
        run = await asyncio.to_thread(run_goal_flow, wf_context, intent)
    except Exception as e:
        logger.exception("workflow failed")
        await status_msg.edit_text(f"❌ Gagal: {escape_md(str(e))}")
        return

    await status_msg.edit_text(
        f"📋 *Operational feed* — {escape_md(intent_label)} ({run.status})",
        parse_mode=ParseMode.MARKDOWN,
    )

    for entry in run.feed:
        await _safe_reply(update.message, format_feed_line(entry), parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(FEED_DELAY_SEC)

    if run.status == "ok" and intent == UserIntent.VENDOR_PAYMENT:
        await _safe_reply(update.message, format_workflow_summary(run), parse_mode=ParseMode.MARKDOWN)
        tg_ok = run.context.get("telegram_invoice_sent")
        extra = "Invoice Telegram: terkirim ✅" if tg_ok else (
            "Invoice Telegram: belum (isi telegram_id vendor & vendor harus /start bot)"
        )
        await update.message.reply_text(
            f"✅ Selesai.\n{extra}\nWA: {run.context.get('supplier_phone', '-')}"
        )
    elif run.status == "ok":
        await _safe_reply(update.message, format_workflow_summary(run), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"Workflow berhenti: status={run.status}")


async def cmd_bayar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name_arg = " ".join(context.args) if context.args else ""
    goal = f"Bayar supplier {name_arg}".strip() if name_arg else "Bayar supplier bulan ini"
    await _run_workflow(update, context, goal, UserIntent.VENDOR_PAYMENT, supplier_name=name_arg)


async def cmd_kirim_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name_arg = " ".join(context.args) if context.args else ""
    if not name_arg:
        await update.message.reply_text("Gunakan: /kirim_invoice <nama_supplier>")
        return
    goal = f"Kirim invoice ke supplier {name_arg}"
    await _run_workflow(update, context, goal, UserIntent.VENDOR_PAYMENT, supplier_name=name_arg)


async def cmd_rencana(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    goal = " ".join(context.args) if context.args else "Rencana operasional bisnis"
    await _run_workflow(update, context, goal, UserIntent.PLANNING)


async def handle_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("in_onboarding") or context.user_data.get("adding_supplier"):
        return
    if context.user_data.get("pending_discovery_save"):
        await handle_discovery_amount(update, context)
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        low = text.split()[0].lower().split("@")[0]
        if low in ("/carisupplier", "/carisup"):
            await cmd_cari_supplier(update, context)
        elif low not in (
            "/start",
            "/setup",
            "/help",
            "/cancel",
            "/profile",
            "/lokasi",
            "/bayar",
            "/rencana",
            "/cari_supplier",
            "/tambah_supplier",
            "/daftar_supplier",
            "/simpan_vendor",
            "/kirim_invoice",
        ):
            await cmd_unknown(update, context)
        return

    intent = detect_intent(text)
    if intent == UserIntent.VENDOR_PAYMENT:
        if not has_suppliers(update.effective_chat.id):
            await update.message.reply_text("Daftarkan vendor: /tambah_supplier atau /cari_supplier")
            return
        await update.message.reply_text(
            "Pembayaran:\n/daftar_supplier\n/bayar <nama>\n/kirim_invoice <nama>"
        )
        return

    if re.search(r"\b(cari|rekomendasi).*(supplier|vendor)\b", text, re.I):
        await cmd_cari_supplier(update, context)
        return

    await _run_workflow(update, context, text, intent)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


def _global_command_handlers() -> list[CommandHandler]:
    """Commands that must work even during /setup or /tambah_supplier."""
    names = [
        "help",
        "profile",
        "lokasi",
        "cari_supplier",
        "carisupplier",
        "carisup",
        "daftar_supplier",
        "bayar",
        "rencana",
        "cancel",
    ]
    mapping = {
        "help": cmd_help,
        "profile": cmd_profile,
        "lokasi": cmd_lokasi,
        "cari_supplier": cmd_cari_supplier,
        "carisupplier": cmd_cari_supplier,
        "carisup": cmd_cari_supplier,
        "daftar_supplier": cmd_daftar_supplier,
        "bayar": cmd_bayar,
        "rencana": cmd_rencana,
        "cancel": onboard_cancel,
    }
    return [CommandHandler(n, mapping[n]) for n in names]


def build_application() -> Application:
    app = Application.builder().token(_token()).build()
    global_fallbacks = _global_command_handlers()

    company_onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CommandHandler("setup", cmd_setup)],
        states={
            BIZ_LEGAL_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_legal_type)],
            BIZ_LEGAL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_legal_name)],
            BIZ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_biz_name)],
            BIZ_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_biz_type)],
            BIZ_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_address)],
            BIZ_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_city)],
            BIZ_NPWP: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_npwp)],
            OWNER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_owner)],
            MODAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_modal)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_target)],
            PREFS: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_prefs)],
            BIZ_LOCATION: [
                MessageHandler(filters.LOCATION, onboard_location),
                MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_location),
            ],
        },
        fallbacks=global_fallbacks,
        name="company_onboarding",
    )

    supplier_onboarding = ConversationHandler(
        entry_points=[CommandHandler("tambah_supplier", cmd_tambah_supplier)],
        states={
            SUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_name)],
            SUP_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_contact)],
            SUP_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_phone)],
            SUP_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_address)],
            SUP_TELEGRAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_telegram)],
            SUP_DOKU: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_doku)],
            SUP_PRODUCTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_products)],
            SUP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sup_amount)],
        },
        fallbacks=global_fallbacks + [CommandHandler("tambah_supplier", cmd_tambah_supplier)],
        name="supplier_onboarding",
    )

    # Priority group: works before conversation swallows updates
    for h in global_fallbacks:
        app.add_handler(h, group=-1)

    app.add_handler(company_onboarding)
    app.add_handler(supplier_onboarding)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("daftar_supplier", cmd_daftar_supplier))
    app.add_handler(CommandHandler("cari_supplier", cmd_cari_supplier))
    app.add_handler(CommandHandler("carisupplier", cmd_cari_supplier))
    app.add_handler(CommandHandler("carisup", cmd_cari_supplier))
    app.add_handler(CommandHandler("simpan_vendor", cmd_simpan_vendor))
    app.add_handler(CommandHandler("lokasi", cmd_lokasi))
    app.add_handler(CommandHandler("bayar", cmd_bayar))
    app.add_handler(CommandHandler("kirim_invoice", cmd_kirim_invoice))
    app.add_handler(CommandHandler("rencana", cmd_rencana))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_update))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_goal))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logger.info("COOPilot Telegram bot starting...")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
