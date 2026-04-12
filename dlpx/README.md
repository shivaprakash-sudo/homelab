# DLPX — Self-Hosted yt-dlp Web Downloader

## Stack
- **Backend**: FastAPI + yt-dlp + ffmpeg (Python 3.12)
- **Frontend**: Static HTML/JS served by Nginx
- **Reverse proxy**: Caddy (auto HTTPS via Let's Encrypt)
- **Orchestration**: Docker Compose

---

## Prerequisites

- Ubuntu server with Docker + Docker Compose installed
- DuckDNS account with a subdomain pointing to your public IP
- Router with ports 80 and 443 forwarded to this server

---

## Setup

### 1. Clone / copy this directory to your server

```bash
scp -r ./ytdlp-web user@yourserver:~/ytdlp-web
# or git clone if you push it to a repo
```

### 2. Edit `.env`

```
DOMAIN=yourname.duckdns.org
```

### 3. Edit `caddy/Caddyfile` for HTTPS

Uncomment the email line and add your email for Let's Encrypt:

```
{
    email your@email.com
}

yourname.duckdns.org {
    reverse_proxy frontend:80
}
```

### 4. Set up DuckDNS auto-updater (on the host)

```bash
chmod +x duckdns-update.sh
# Edit SUBDOMAIN and TOKEN in the script
crontab -e
# Add: */5 * * * * /home/youruser/ytdlp-web/duckdns-update.sh >> /var/log/duckdns.log 2>&1
```

### 5. Build and start

```bash
cd ~/ytdlp-web
docker compose up -d --build
```

### 6. Check logs

```bash
docker compose logs -f backend
docker compose logs -f caddy
```

---

## Access

- Local: `http://localhost`
- Remote: `https://yourname.duckdns.org`

---

## Features

| Feature | Details |
|---|---|
| Video download | MP4 / MKV / WebM, quality 240p–1080p or best |
| Audio download | MP3 / M4A / Opus / FLAC / WAV, best quality |
| Playlist support | Video or audio, with start/end range |
| Metadata | Embed title, artist, album art thumbnail |
| Subtitles | Download + embed by language code (en, ja, etc.) |
| URL inspect | Fetch title/thumbnail/duration before downloading |
| Progress | Live progress bar with speed and ETA |
| Log viewer | Expandable raw yt-dlp output per job |
| File download | Direct download link for each output file |
| Job cleanup | Delete jobs and their files manually |

---

## Security Note

This service is open to anyone with the URL. To restrict access, add Basic Auth in Caddy:

```
yourname.duckdns.org {
    basicauth {
        admin $2a$14$...  # generate with: caddy hash-password
    }
    reverse_proxy frontend:80
}
```

Generate a hash: `docker run --rm caddy:2-alpine caddy hash-password --plaintext yourpassword`

---

## Disk Cleanup

Downloaded files live in a Docker volume (`downloads`). Jobs older than 2h are auto-cleaned on restart. For continuous cleanup, add a cron inside the container or add a scheduled task service.

```bash
# Manual: remove all download files
docker compose exec backend find /downloads -mindepth 1 -maxdepth 1 -type d -mmin +120 -exec rm -rf {} +
```

---

## Updating yt-dlp

yt-dlp gets the latest binary at build time. To update:

```bash
docker compose build backend --no-cache
docker compose up -d backend
```
