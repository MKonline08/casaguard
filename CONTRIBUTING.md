# Contributing to CasaGuard

Thanks for improving local-first room security.

## Before opening a pull request

1. Keep changes focused and explain the actual hardware you tested.
2. Never commit camera recordings, face-enrollment images, `.env`, webhook tokens, or custom models without clear licensing.
3. Validate Compose syntax on a Linux Docker host and run `scripts/camera-test.sh` against the affected webcam format.
4. Update the relevant document when a port, volume, image tag, or retention behavior changes.

## Security reports

Do not open a public issue for a vulnerability that could expose a camera, recording, or credential. Contact the repository maintainers privately with a reproduction and affected version instead.

## Style

Keep the system CPU-friendly by default, use pin-able image tags, and document any resource cost. CasaGuard must remain usable without MQTT, a cloud account, or a paid subscription.
