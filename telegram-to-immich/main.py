#!/usr/bin/env python3
"""
Telegram → Immich video sync
Watches a Telegram channel for new videos and uploads them to Immich.
"""

import os
import asyncio
import logging
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import requests
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo

# ── Config from env ──────────────────────────────────────────────────────────
TG_API_ID       = int(os.environ["TG_API_ID"])
TG_API_HASH     = os.environ["TG_API_HASH"]
TG_CHANNEL      = os.environ["TG_CHANNEL"]          # username or numeric ID
IMMICH_URL      = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY  = os.environ["IMMICH_API_KEY"]
DOWNLOAD_DIR    = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
SESSION_FILE    = os.environ.get("SESSION_FILE", "/session/tg_session")
BACKFILL        = os.environ.get("BACKFILL", "false").lower() == "true"
BACKFILL_LIMIT  = int(os.environ.get("BACKFILL_LIMIT", "100"))  # 0 = unlimited
LOG_LEVEL       = os.environ.get("LOG_LEVEL", "INFO").upper()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg-immich")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── Immich helpers ────────────────────────────────────────────────────────────
def immich_headers():
    return {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}


def immich_healthy() -> bool:
    try:
        r = requests.get(f"{IMMICH_URL}/api/server/ping", headers=immich_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Immich health check failed: {e}")
        return False


def upload_to_immich(file_path: Path, created_at: datetime) -> dict:
    """Upload a file to Immich. Returns the API response dict."""
    ts = created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    device_asset_id = file_path.name

    # Guess mime type
    suffix = file_path.suffix.lower()
    mime_map = {".mp4": "video/mp4", ".mkv": "video/x-matroska",
                ".webm": "video/webm", ".mov": "video/quicktime",
                ".avi": "video/x-msvideo", ".flv": "video/x-flv"}
    mime = mime_map.get(suffix, "video/mp4")

    with open(file_path, "rb") as f:
        response = requests.post(
            f"{IMMICH_URL}/api/assets",
            headers=immich_headers(),
            files={"assetData": (file_path.name, f, mime)},
            data={
                "deviceAssetId": device_asset_id,
                "deviceId": "tg-immich-sync",
                "fileCreatedAt": ts,
                "fileModifiedAt": ts,
            },
            timeout=300,
        )

    if response.status_code in (200, 201):
        data = response.json()
        status = data.get("status", "uploaded")
        log.info(f"Immich [{status}]: {file_path.name}")
        return data
    else:
        log.error(f"Immich upload failed {response.status_code}: {response.text[:200]}")
        return {}


# ── Telegram helpers ──────────────────────────────────────────────────────────
def is_video(message) -> bool:
    if not message.media:
        return False
    if isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return True
    return False


def guess_extension(message) -> str:
    try:
        mime = message.media.document.mime_type
        mime_ext = {
            "video/mp4": ".mp4", "video/x-matroska": ".mkv",
            "video/webm": ".webm", "video/quicktime": ".mov",
            "video/x-msvideo": ".avi", "video/x-flv": ".flv",
        }
        return mime_ext.get(mime, ".mp4")
    except Exception:
        return ".mp4"


async def download_and_upload(client, message):
    """Download a Telegram video message and upload it to Immich."""
    msg_id = message.id
    ext = guess_extension(message)
    created_at = message.date or datetime.now(timezone.utc)
    filename = f"tg_{msg_id}_{int(created_at.timestamp())}{ext}"
    dest = DOWNLOAD_DIR / filename

    if dest.exists():
        log.info(f"Already downloaded, skipping: {filename}")
        upload_to_immich(dest, created_at)
        return

    log.info(f"Downloading message {msg_id} → {filename}")
    try:
        await client.download_media(message, file=str(dest))
        log.info(f"Download complete: {filename} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
    except Exception as e:
        log.error(f"Download failed for message {msg_id}: {e}")
        return

    upload_to_immich(dest, created_at)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("Starting tg-immich-sync")

    if not immich_healthy():
        log.warning("Immich not reachable at startup — will retry on uploads")

    client = TelegramClient(SESSION_FILE, TG_API_ID, TG_API_HASH)
    await client.start()
    log.info("Telegram client authenticated")

    # Resolve channel entity once
    dialogs = await client.get_dialogs()
    entity = next((d.entity for d in dialogs if str(d.entity.id) == TG_CHANNEL or d.name == TG_CHANNEL), None)
    if entity is None:
        raise ValueError(f"Could not find group: {TG_CHANNEL}")
    log.info(f"Watching channel: {getattr(entity, 'title', TG_CHANNEL)}")

    # Optional backfill of existing messages
    if BACKFILL:
        limit = BACKFILL_LIMIT if BACKFILL_LIMIT > 0 else None
        log.info(f"Backfilling up to {limit or 'all'} messages…")
        async for message in client.iter_messages(entity, limit=limit):
            if is_video(message):
                await download_and_upload(client, message)
        log.info("Backfill complete")

    # Live listener for new messages
    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        message = event.message
        if is_video(message):
            log.info(f"New video detected (msg id {message.id})")
            await download_and_upload(client, message)

    log.info("Listening for new videos…")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
