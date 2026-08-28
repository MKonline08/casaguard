# 🛠️ CasaGuard troubleshooting

## Camera not found

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
./scripts/camera-test.sh /dev/video0
```

If a USB camera is missing, unplug/reconnect it, run `v4l2-ctl --list-devices`, then trigger a scan at `http://YOUR-CASAOS-IP:8971`. Do not manually add duplicate `/dev/video*` nodes; the manager selects one capture node per physical camera.

If the device exists but the test fails, verify that its native mode appears in `v4l2-ctl --list-formats-ext`. Add the CasaOS user to the `video` group if host-level access is denied:

```bash
sudo usermod -aG video "$USER"
```

Sign out and back in afterward.

## Frigate starts but has a blank camera

Read the camera-specific logs:

```bash
docker compose logs --tail=200 frigate
```

Common fixes:

- Run `scripts/camera-test.sh` again. Frigate cannot fix a host capture failure.
- Stop every other program using the webcam (browser, Zoom, Cheese, etc.).
- Confirm `casaguard-camera-manager` is healthy and privileged device access is allowed by CasaOS.
- Open port `8971` and confirm the camera pipeline says **running**. CasaGuard selects MJPEG/H.264 handling automatically; do not hand-edit the generated YAML.

After changing configuration, validate by restarting Frigate:

```bash
docker compose up -d frigate
docker compose logs -f frigate
```

## Night mode does not switch

Open `http://YOUR-CASAOS-IP:8971` and check each camera's Light, Active profile, and Monitor fields. Auto enters Night after six readings at or below the dark threshold and returns to Day after three readings at or above the bright threshold. Test immediately by selecting forced Night and then forced Day.

If the monitor reports `sample_error`, `fallback_restart`, or a reconnecting pipeline, inspect both services:

```bash
docker compose logs --since=5m camera-manager frigate
curl -s http://127.0.0.1:8971/api/cameras
curl -s http://127.0.0.1:1984/api/streams
```

Port `1984` must remain bound to `127.0.0.1`. Software enhancement cannot reveal detail in complete darkness without visible or infrared illumination. Aggressive mode also increases encoding work; use Balanced or Gentle if the host cannot maintain the camera's native FPS.

## High CPU usage

Live video keeps each camera's selected native resolution and FPS, while object detection is capped at 5 FPS. Native software encoding and CPU detection are still real work.

1. Confirm no GPU preset was added: `hwaccel_args` should remain empty.
2. Use a hardware H.264 camera when possible; Day mode can copy H.264 without re-encoding.
3. Use Gentle or Balanced night enhancement if aggressive filtering overloads the host.
4. Track fewer labels; `person` alone is the best low-power baseline.
5. Do not run face recognition, sound classification, custom objects, and Frigate review at the same time on an 8 GB system.

Apply one change at a time, then run `docker stats` for at least several minutes before deciding whether it helped.

## Too many false positives or missed people

The included `person.min_area: 8000` and `threshold: 0.7` prioritize fewer false alerts in a room. Tune from actual Frigate review items:

| Symptom | First adjustment |
|---|---|
| Small / distant people are missed | Reduce `min_area` by 1,000–2,000. |
| Shadows are detected as people | Raise `threshold` to `0.75` or add a motion mask. |
| Pets prompt too many reviews | Remove `cat` and `dog` from `track`. |
| CPU grows after tuning | Keep detection at 5 fps or lower. |

Save a clean snapshot before tuning. Threshold changes cannot correct a camera pointed at a window, mirror, or moving TV; repositioning is usually the better fix.

## RAM limit or container restarts

The Compose limits are intentional: 3072 MB for Frigate and 1024 MB for CodeProject.AI. Inspect recent exits:

```bash
docker compose ps
docker inspect casaguard-codeproject-ai --format '{{.State.OOMKilled}}'
docker compose logs --tail=150 codeproject-ai
```

If CodeProject.AI is OOM-killed, stop it while confirming Frigate is stable, then install and run just one extra module. If your host has no headroom beyond 8 GB, keep CodeProject.AI stopped except when registering faces or testing models:

```bash
docker compose stop codeproject-ai
docker compose start codeproject-ai
```

Do not lift memory limits until you have measured CasaOS host memory and swap pressure.

## Webhook sends nothing

1. Confirm `WEBHOOK_URL` is non-empty in `.env`.
2. Restart the relay: `docker compose up -d webhook-relay`.
3. Watch its logs: `docker compose logs -f webhook-relay`.
4. Trigger a new completed person event. On first start, the relay stores a baseline and deliberately does not send historical events.

The webhook endpoint must be reachable from the Docker network and return a 2xx response. A 401 or 403 normally means its bearer token is wrong.
