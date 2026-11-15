#!/usr/bin/env python3
"""
Telegram -> Immich media sync
Watches a Telegram chat for new photos/videos, uploads them to Immich,
and (optionally) adds them to an album.
"""

import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
from telethon import TelegramClient, events
from telethon.utils import get_peer_id
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    DocumentAttributeVideo,
)

# -- Config from env ----------------------------------------------------------
TG_API_ID       = int(os.environ["TG_API_ID"])
TG_API_HASH     = os.environ["TG_API_HASH"]
TG_CHANNEL      = os.environ["TG_CHANNEL"]            # numeric ID, username, or exact title
IMMICH_URL      = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY  = os.environ["IMMICH_API_KEY"]
IMMICH_ALBUM    = os.environ.get("IMMICH_ALBUM", "").strip()   # empty = no album
MEDIA_TYPES     = os.environ.get("MEDIA_TYPES", "both").lower()  # videos | photos | both
DOWNLOAD_DIR    = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
SESSION_FILE    = os.environ.get("SESSION_FILE", "/session/tg_session")
BACKFILL        = os.environ.get("BACKFILL", "false").lower() == "true"
BACKFILL_LIMIT  = int(os.environ.get("BACKFILL_LIMIT", "100"))  # 0 = unlimited
LOG_LEVEL       = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg-immich")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MIME_EXT = {
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/x-flv": ".flv",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/gif": ".gif",
}
EXT_MIME = {v: k for k, v in MIME_EXT.items()}


# -- Immich helpers -----------------------------------------------------------
def immich_headers():
    return {"x-api-key": IMMICH_API_KEY, "Accept": "application/json"}


def immich_healthy() -> bool:
    try:
        r = requests.get(f"{IMMICH_URL}/api/server/ping", headers=immich_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Immich health check failed: {e}")
        return False


_album_id = None


def get_or_create_album() -> str | None:
    """Find the album by name, creating it if missing. Cached after first success."""
    global _album_id
    if not IMMICH_ALBUM:
        return None
    if _album_id:
        return _album_id
    try:
        r = requests.get(f"{IMMICH_URL}/api/albums", headers=immich_headers(), timeout=30)
        r.raise_for_status()
        for album in r.json():
            if album.get("albumName") == IMMICH_ALBUM:
                _album_id = album["id"]
                log.info(f"Using existing album '{IMMICH_ALBUM}'")
                return _album_id

        r = requests.post(
            f"{IMMICH_URL}/api/albums",
            headers=immich_headers(),
            json={"albumName": IMMICH_ALBUM},
            timeout=30,
        )
        r.raise_for_status()
        _album_id = r.json()["id"]
        log.info(f"Created album '{IMMICH_ALBUM}'")
        return _album_id
    except Exception as e:
        log.error(f"Album lookup/creation failed (check API key permissions): {e}")
        return None


def add_to_album(asset_id: str):
    album_id = get_or_create_album()
    if not album_id:
        return
    try:
        r = requests.put(
            f"{IMMICH_URL}/api/albums/{album_id}/assets",
            headers=immich_headers(),
            json={"ids": [asset_id]},
            timeout=30,
        )
        r.raise_for_status()
        # Response is a per-asset result list, e.g. success / duplicate
        for item in r.json():
            log.info(f"Album '{IMMICH_ALBUM}': {item.get('success') and 'added' or item.get('error', 'failed')}")
    except Exception as e:
        log.error(f"Adding asset to album failed: {e}")


def upload_to_immich(file_path: Path, created_at: datetime):
    ts = created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    mime = EXT_MIME.get(file_path.suffix.lower(), "application/octet-stream")

    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{IMMICH_URL}/api/assets",
                headers=immich_headers(),
                files={"assetData": (file_path.name, f, mime)},
                data={
                    "deviceAssetId": file_path.name,
                    "deviceId": "tg-immich-sync",
                    "fileCreatedAt": ts,
                    "fileModifiedAt": ts,
                },
                timeout=300,
            )
    except Exception as e:
        log.error(f"Immich upload error for {file_path.name}: {e}")
        return

    if response.status_code in (200, 201):
        data = response.json()
        log.info(f"Immich [{data.get('status', 'uploaded')}]: {file_path.name}")
        # Duplicates also return the existing asset id, so they still get albumed
        if data.get("id"):
            add_to_album(data["id"])
    else:
        log.error(f"Immich upload failed {response.status_code}: {response.text[:200]}")


# -- Telegram helpers ---------------------------------------------------------
def media_kind(message) -> str | None:
    """Return 'photo', 'video', or None, filtered by MEDIA_TYPES."""
    kind = None
    if isinstance(message.media, MessageMediaPhoto):
        kind = "photo"
    elif isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes):
            kind = "video"
        elif (doc.mime_type or "").startswith("image/") and doc.mime_type != "image/webp":
            kind = "photo"   # images sent as files; webp excluded (mostly stickers)

    if kind == "photo" and MEDIA_TYPES in ("photos", "both"):
        return kind
    if kind == "video" and MEDIA_TYPES in ("videos", "both"):
        return kind
    return None


def guess_extension(message, kind: str) -> str:
    if isinstance(message.media, MessageMediaPhoto):
        return ".jpg"
    try:
        return MIME_EXT.get(message.media.document.mime_type, ".mp4" if kind == "video" else ".jpg")
    except Exception:
        return ".mp4" if kind == "video" else ".jpg"


async def download_and_upload(client, message, kind: str):
    ext = guess_extension(message, kind)
    created_at = message.date or datetime.now(timezone.utc)
    dest = DOWNLOAD_DIR / f"tg_{message.id}_{int(created_at.timestamp())}{ext}"

    if dest.exists():
        log.info(f"Already downloaded, re-uploading: {dest.name}")
    else:
        log.info(f"Downloading {kind} (msg {message.id}) -> {dest.name}")
        try:
            await client.download_media(message, file=str(dest))
            log.info(f"Downloaded {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        except Exception as e:
            log.error(f"Download failed for message {message.id}: {e}")
            return

    # requests is blocking; run it off the event loop so Telegram stays responsive
    await asyncio.to_thread(upload_to_immich, dest, created_at)


# -- Main ---------------------------------------------------------------------
async def main():
    log.info(f"Starting tg-immich-sync (media: {MEDIA_TYPES}, album: {IMMICH_ALBUM or 'none'})")

    if not immich_healthy():
        log.warning("Immich not reachable at startup - will retry on uploads")

    client = TelegramClient(SESSION_FILE, TG_API_ID, TG_API_HASH)
    await client.start()
    log.info("Telegram client authenticated")

    dialogs = await client.get_dialogs()
    want = TG_CHANNEL.strip().lstrip("@")
    entity = None
    for d in dialogs:
        e = d.entity
        candidates = {
            str(e.id),                      # raw id, e.g. 1234567890
            str(get_peer_id(e)),            # marked id, e.g. -1001234567890 / -4280512476
            (d.name or "").lower(),
            (getattr(e, "username", None) or "").lower(),
        }
        if want in candidates or want.lower() in candidates:
            entity = e
            break
    if entity is None:
        log.error("Could not find that chat. Chats this account can see:")
        for d in dialogs:
            log.error(f"  id={d.entity.id}  marked={get_peer_id(d.entity)}  name={d.name!r}")
        raise ValueError(f"Could not find chat: {TG_CHANNEL}")
    log.info(f"Watching: {getattr(entity, 'title', TG_CHANNEL)}")

    if BACKFILL:
        limit = BACKFILL_LIMIT if BACKFILL_LIMIT > 0 else None
        log.info(f"Backfilling up to {limit or 'all'} messages...")
        async for message in client.iter_messages(entity, limit=limit):
            kind = media_kind(message)
            if kind:
                await download_and_upload(client, message, kind)
        log.info("Backfill complete")

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        kind = media_kind(event.message)
        if kind:
            log.info(f"New {kind} detected (msg {event.message.id})")
            await download_and_upload(client, event.message, kind)

    log.info("Listening for new media...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())