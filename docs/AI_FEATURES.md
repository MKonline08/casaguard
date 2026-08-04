# 🧠 AI features

CasaGuard treats Frigate as the always-on NVR and CodeProject.AI as an on-demand local enrichment server. That separation is intentional on an 8 GB CPU-only laptop: continuous NVR detection remains responsive while face, sound, and custom-model work is enabled only when useful.

> [!NOTE]
> The official CodeProject.AI Docker image includes object detection. Face Processing and Sound Classifier are optional modules installed from the local dashboard on first use; their files persist in the `casaguard_codeproject_modules` Docker volume. Do not install every available module on an 8 GB host.

## Add face recognition

1. Open `http://CASAOS-IP:32168`.
2. In the **Modules** dashboard, install **Face Processing** and wait for its status to become **Started**.
3. Enroll only people who have consented. Use several clear, well-lit photos for each person.
4. In the API explorer/dashboard, verify the face module’s `register` and `recognize` endpoints before connecting it to an alert workflow.

The server’s common face-registration request format is multipart form data:

```bash
curl -X POST "http://CASAOS-IP:32168/v1/vision/face/register" \
  -F 'userid="resident_1"' \
  -F "image=@/path/to/resident_1.jpg"
```

Use the live API explorer bundled with your installed module as the source of truth for the matching recognition endpoint and payload. Module APIs can change between CodeProject.AI releases; a quick test in the dashboard is safer than wiring an unverified automation to a security alert.

### Privacy and quality

- Face recognition is less reliable with faces smaller than roughly 100 pixels, backlighting, masks, or profiles.
- Match results should be advisory—not a decision to unlock doors, contact police, or take other irreversible action.
- Keep enrollment images and the Docker volume encrypted/backed up like other sensitive security data.

## Train and load a custom object model

CodeProject.AI accepts compatible YOLO models through its Object Detection module. Train with images that match your actual room: lighting, camera angle, distance, and the object’s common occlusions matter more than a large unrelated dataset.

1. Collect consented, non-sensitive images and label them in YOLO format.
2. Split them into training/validation sets without placing near-identical consecutive frames in both.
3. Train and export a model compatible with the installed Object Detection YOLO module.
4. Copy the model files into `models/` beside `docker-compose.yml`.
5. Restart CodeProject.AI, then choose/verify the model in its dashboard or API explorer.

```bash
docker compose restart codeproject-ai
docker compose logs -f codeproject-ai
```

`models/` is mounted read-only at `/app/modules/ObjectDetectionYolo/custom-models`, preventing a container process from changing your trained artifacts. Keep a model card with label names, source data restrictions, validation metrics, and training command outside the runtime folder.

## Enable audio classification

Audio classification needs an actual capture device and an audio-enabled stream. A typical USB webcam exposes video and audio as separate Linux devices; `/dev/video0` alone is not enough.

1. Find the microphone with `arecord -l` on the CasaOS host.
2. Confirm the microphone is lawful and appropriate to record in your room.
3. Install **Sound Classifier** from the CodeProject.AI dashboard.
4. Test a short WAV clip through the module’s dashboard/API explorer first.
5. Only then add a separate audio capture path or an alert bridge. Keep it disabled unless you specifically need it—continuous audio processing increases CPU, storage, and privacy impact.

CasaGuard does not silently enable microphone recording. This prevents an ambiguous USB camera setup from collecting audio unexpectedly.

## Connect Frigate events to AI and alerts

The included `webhook-relay` sends completed **person** events to your generic webhook endpoint with a snapshot URL. A lightweight local integration can then:

1. Fetch that snapshot from Frigate.
2. Submit it to the selected CodeProject.AI module.
3. Apply your own policy (for example, notify only for an unknown face).
4. Send a mobile push notification through ntfy, Home Assistant, or another local alert service.

Keep that policy external and explicit. It avoids a hidden network dependency and lets you decide exactly what counts as an alert. CasaGuard itself works without MQTT, a cloud account, or a subscription.
