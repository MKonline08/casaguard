# ⚡ CasaGuard setup

CasaGuard is a local multi-camera deployment for CasaOS. It discovers V4L2 USB cameras and ONVIF network cameras, and accepts validated RTSP/HTTP stream URLs.

## Before you start

- A 64-bit Linux machine running CasaOS and Docker Compose v2
- Any supported USB webcam, or an ONVIF/RTSP network camera
- Enough free storage for the configured 30-day alert and detection retention window
- A modern two-core-or-better CPU; the included profile targets an AMD Ryzen laptop with 8 GB RAM and no GPU

> [!IMPORTANT]
> CasaGuard groups the video nodes exposed by each physical webcam and tracks the camera independently of duplicate metadata nodes.

## Option A — CasaOS Custom App

1. Copy this repository onto the CasaOS host, then open its directory in a terminal.
2. Run `cp .env.example .env` and edit `TZ` to your IANA timezone, such as `America/Chicago`.
3. If a USB camera is connected, run `./scripts/camera-test.sh` to confirm the host can capture it. Zero-camera startup is also supported.
4. In CasaOS, select **App Store** → **Custom Install** → **Import** and choose **Docker Compose**.
5. Paste the complete contents of `docker-compose.yml` and select **Submit**.
6. Open `http://YOUR-CASAOS-IP:8971` to review discovered cameras, configure automatic day/night behavior, or add an authenticated RTSP URL.
7. Start the app. Go to `http://CASAOS-IP:5000` and set the Frigate account password when prompted.
8. Go to `http://CASAOS-IP:32168` and verify CodeProject.AI reports healthy.

The generated Frigate configuration is stored in the `casaguard_frigate_config` Docker volume. It is intentionally not tracked by Git, so automatic camera updates cannot block future `git pull` operations.

## Option B — recommended one-command install

```bash
git clone https://github.com/YOUR-ACCOUNT/casaguard.git
cd casaguard
cp .env.example .env
chmod +x scripts/*.sh
./scripts/install.sh
```

The script checks Linux/Docker, tests all connected USB cameras, pulls the published images, builds the local services, and starts the stack even when no camera is attached yet.

## Manual Docker Compose steps

```bash
cp .env.example .env
nano .env
./scripts/camera-test.sh /dev/video0
docker compose pull
docker compose build camera-manager webhook-relay
docker compose up -d
docker compose ps
```

Useful status commands:

```bash
docker compose logs -f frigate
docker compose logs -f camera-manager
docker compose logs -f codeproject-ai
docker compose logs -f webhook-relay
```

Stop without deleting recordings:

```bash
docker compose down
```

Do **not** run `docker compose down -v` unless you intentionally want to delete recordings, models, and the relay’s alert history.

## Ports

| Port | Container | Purpose | Exposure recommendation |
|---:|---|---|---|
| `5000` | Frigate | UI and REST API | LAN only |
| `8971` | Camera manager | USB/ONVIF discovery and RTSP setup | LAN only |
| `1984` | go2rtc local API | Camera ingest and light sampling | Loopback only; never expose |
| `8554` | Frigate/go2rtc | RTSP restream | LAN only; firewall if unused |
| `8555/tcp,udp` | Frigate/go2rtc | WebRTC | LAN only |
| `32168` | CodeProject.AI | Dashboard and REST API | LAN only |

Neither dashboard should be port-forwarded to the internet. Use a private VPN or a carefully configured reverse proxy with HTTPS and strong authentication for remote access.

## Automatic day/night mode

Every camera defaults to **Auto / Aggressive**. CasaGuard samples the hidden unfiltered stream every 10 seconds, enters Night after one minute below the dark threshold, and returns to Day after 30 seconds above the bright threshold. Very bright light restores Day within two samples. A five-minute dwell prevents rapid switching around either threshold.

Use the camera-manager page on port `8971` to force Day or Night, select Gentle/Balanced/Aggressive enhancement, or tune the per-camera thresholds. The filter never changes native resolution or live FPS. CasaGuard keeps the hidden native capture running and replaces only the affected camera's public output pipeline. If that replacement cannot be verified, it preserves the requested mode and asks Frigate for one controlled recovery restart.

Frigate's `GO2RTC_ALLOW_ARBITRARY_EXEC` switch is enabled so its startup security pass preserves CasaGuard's source-less local ingest destinations. CasaGuard does not expose go2rtc's management port beyond `127.0.0.1`, writes generated configuration with owner-only permissions, and does not accept arbitrary command or configuration input through the camera-manager API. Keep Frigate and the camera-manager restricted to trusted LAN users.

## Storage and backup

Docker named volumes hold durable data:

```bash
docker volume ls | grep casaguard
```

Frigate alert and detection recordings have 30 days of retention. For a backup, stop the stack and archive the named volumes using your normal CasaOS backup method. Back up any enrolled face images and custom models separately.

## Optional generic webhook

Frigate itself does not have an arbitrary outbound webhook setting. CasaGuard’s included `webhook-relay` safely polls its local event API and sends only completed `person` events.

1. Set `WEBHOOK_URL` in `.env` to an HTTPS endpoint you control. Set `FRIGATE_PUBLIC_URL` to the Frigate LAN URL if the receiver must open snapshot links.
2. Optionally set a long random `WEBHOOK_TOKEN`; the relay sends it as `Authorization: Bearer …`.
3. Apply it with `docker compose up -d webhook-relay`.

The JSON body includes `source`, `type`, the Frigate event, and a `snapshot_url`. Never put webhook secrets in Git.
