#!/usr/bin/env bash
# One-command CasaGuard installer for a Linux CasaOS host.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose)

fail() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[[ "$(uname -s)" == "Linux" ]] || fail "CasaGuard must run on a Linux CasaOS host."
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install CasaOS first."
docker info >/dev/null 2>&1 || fail "Docker is unavailable to this user. Try: sudo usermod -aG docker $USER, then sign out/in."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
[[ -e /dev/video0 ]] || fail "/dev/video0 was not found. Connect the Logitech webcam, then run scripts/camera-test.sh."

if ! grep -qi 'CasaOS' /etc/os-release 2>/dev/null && [[ ! -d /var/lib/casaos ]]; then
  echo "WARNING: CasaOS was not detected. Continuing on a supported Linux Docker host."
fi

cd "$ROOT_DIR"
[[ -f .env ]] || { cp .env.example .env; info "Created .env; set TZ before exposing the system."; }
mkdir -p frigate models
chmod 755 scripts/install.sh scripts/camera-test.sh

info "Checking the webcam..."
./scripts/camera-test.sh /dev/video0

info "Pulling CPU-only images (the CodeProject.AI image is large on first pull)..."
"${COMPOSE[@]}" pull

info "Building the local person-event webhook relay..."
"${COMPOSE[@]}" build webhook-relay

info "Starting CasaGuard..."
"${COMPOSE[@]}" up -d

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"
cat <<EOF

CasaGuard is starting. Give the AI server a few minutes on its first run.
Frigate:        http://${HOST_IP}:$(grep '^FRIGATE_PORT=' .env | cut -d= -f2)
CodeProject.AI: http://${HOST_IP}:$(grep '^CPAI_PORT=' .env | cut -d= -f2)

Set the Frigate administrator password on the first browser visit.
Check progress with: docker compose ps
EOF
