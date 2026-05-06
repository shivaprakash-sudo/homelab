# tg-immich-sync

Auto-downloads videos from a Telegram channel and uploads them to Immich.

## Setup

### 1. Get Telegram API credentials
Go to https://my.telegram.org → "API development tools" → create an app.
Copy the **App api_id** and **App api_hash**.

### 2. Get your Immich API key
Immich web UI → top-right avatar → Account Settings → API Keys → New API Key.

### 3. Configure docker-compose.yml
Fill in all the values in the `environment:` section.

For `TG_CHANNEL`:
- Public channel: use the username, e.g. `some_channel`
- Private channel: use the numeric ID, e.g. `-1001234567890`
  (forward a message to @userinfobot to get the channel ID)

### 4. First-time authentication (required once)

Telethon needs an interactive login with your phone number + SMS code.
Run this once before starting the service:

```bash
docker compose run --rm tg-immich-sync python -c "
from telethon.sync import TelegramClient
import os
c = TelegramClient('/session/tg_session', int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
c.start()
print('Session saved.')
c.disconnect()
"
```

This saves the session to `./session/tg_session.session` — it persists across restarts.

### 5. Start the service

```bash
docker compose up -d
```

### 6. View logs

```bash
docker compose logs -f
```

## Backfill existing channel history

Set `BACKFILL: "true"` in docker-compose.yml before first start, then set it back
to `"false"` after. Use `BACKFILL_LIMIT: "0"` for unlimited (be careful on large channels).

## Notes

- **Duplicate handling:** Immich deduplicates by content hash — safe to re-run.
- **Downloaded files** are kept in `./downloads/` with filenames like `tg_<msgid>_<timestamp>.mp4`.
- **`cryptg`** is included for faster Telegram downloads (C-based crypto).
- The container runs as root by default; add `user:` directive if needed.
