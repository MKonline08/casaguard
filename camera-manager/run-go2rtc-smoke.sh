#!/bin/sh
set -eu

case "$(apk --print-arch)" in
  x86_64) asset=amd64 ;;
  aarch64) asset=arm64 ;;
  armv7|armhf) asset=arm ;;
  x86) asset=i386 ;;
  *) echo "Unsupported go2rtc smoke-test architecture" >&2; exit 1 ;;
esac

wget -q "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.9/go2rtc_linux_${asset}" -O /tmp/go2rtc
chmod +x /tmp/go2rtc
/tmp/go2rtc -config /app/go2rtc-smoke.yaml >/tmp/go2rtc.log 2>&1 &
go2rtc_pid=$!
cleanup() {
  kill "$go2rtc_pid" 2>/dev/null || true
  wait "$go2rtc_pid" 2>/dev/null || true
  rm -f /tmp/go2rtc /tmp/go2rtc.log /tmp/day.json /tmp/night.json
}
trap cleanup EXIT

attempt=0
until wget -qO- http://127.0.0.1:1984/api/streams >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 20 ]; then
    cat /tmp/go2rtc.log >&2
    exit 1
  fi
  sleep 1
done

ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
  -show_entries stream=codec_name,width,height -of json \
  rtsp://127.0.0.1:8554/test_day > /tmp/day.json
ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
  -show_entries stream=codec_name,width,height -of json \
  rtsp://127.0.0.1:8554/test_night > /tmp/night.json

grep -q '"codec_name": "h264"' /tmp/day.json
grep -q '"codec_name": "h264"' /tmp/night.json
grep -q '"width": 320' /tmp/day.json
grep -q '"height": 240' /tmp/night.json
