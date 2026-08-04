#!/usr/bin/env bash
# Validate a USB webcam on the CasaOS host before starting CasaGuard.
set -Eeuo pipefail

CAMERA="${1:-/dev/video0}"

fail() { echo "ERROR: $*" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is required. Install it with: sudo apt install -y ffmpeg v4l-utils"
[[ -e "$CAMERA" ]] || fail "$CAMERA does not exist. Connect the webcam and check: v4l2-ctl --list-devices"
[[ -r "$CAMERA" ]] || fail "$CAMERA is not readable by $(id -un). Add the CasaOS user to the video group, then log in again."

echo "CasaGuard camera test: $CAMERA"
if command -v v4l2-ctl >/dev/null 2>&1; then
  echo
  echo "Supported formats:"
  v4l2-ctl --device="$CAMERA" --list-formats-ext || true
fi

echo
echo "Capturing one frame at 640x480 / 5fps..."
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
if ffmpeg -hide_banner -loglevel error -f v4l2 -video_size 640x480 -framerate 5 -i "$CAMERA" -frames:v 1 "$WORK_DIR/frame.jpg"; then
  SIZE="$(wc -c < "$WORK_DIR/frame.jpg")"
  echo "PASS: captured a ${SIZE}-byte test frame. CasaGuard can use $CAMERA."
else
  fail "capture failed. Copy the format from the list above into frigate/config.yml."
fi
