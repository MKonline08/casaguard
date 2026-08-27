# 🛠️ CasaGuard troubleshooting

## Camera not found

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
./scripts/camera-test.sh /dev/video0
```

If a USB camera is missing, unplug/reconnect it, run `v4l2-ctl --list-devices`, then trigger a scan at `http://YOUR-CASAOS-IP:8971`. Do not manually add duplicate `/dev/video*` nodes; the manager selects one capture node per physical camera.

If the device exists but the test fails, choose a listed format/resolution that the webcam actually supports. Lower the Frigate stream to `320x240` / 5 fps first, verify a picture, then increase it. Add the CasaOS user to the `video` group if host-level access is denied:

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
- For webcams that only output MJPEG, leave `#video=mjpeg` in the go2rtc line. For a camera whose test reports H.264, use `#video=h264` instead.

After changing configuration, validate by restarting Frigate:

```bash
docker compose up -d frigate
docker compose logs -f frigate
```

## High CPU usage

The supplied 640×480/5-fps profile is deliberately conservative, but CPU decoding and CPU detection are still real work.

1. Confirm no GPU preset was added: `hwaccel_args` should remain empty.
2. Lower `detect.fps` in `frigate/config.yml` from `5` to `3`.
3. Reduce camera resolution to `320x240` after verifying the webcam supports it.
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

The Compose limits are intentional: 1536 MB for Frigate and 1024 MB for CodeProject.AI. Inspect recent exits:

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
