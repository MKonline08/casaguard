"""Send completed Frigate person events to an opt-in generic HTTPS webhook.

The relay uses Frigate's local REST API; it does not require MQTT or cloud
credentials. Its on-disk state prevents duplicate notifications after restart.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
FRIGATE_PUBLIC_URL = os.environ.get("FRIGATE_PUBLIC_URL", FRIGATE_URL).rstrip("/")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "").strip()
POLL_SECONDS = max(5, int(os.environ.get("POLL_SECONDS", "10")))
STATE_PATH = Path("/state/last_event_id")


def load_last_id() -> str:
    try:
        return STATE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def save_last_id(event_id: str) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(event_id, encoding="utf-8")
    temporary.replace(STATE_PATH)


def get_events() -> list[dict]:
    query = urllib.parse.urlencode({"limit": 50, "has_snapshot": 1})
    request = urllib.request.Request(f"{FRIGATE_URL}/api/events?{query}")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError("Frigate events endpoint returned a non-list payload")
    return payload


def notify(event: dict) -> None:
    if not WEBHOOK_URL:
        return
    event_id = event["id"]
    body = {
        "source": "casaguard",
        "type": "person.detected",
        "event": event,
        "snapshot_url": f"{FRIGATE_PUBLIC_URL}/api/events/{event_id}/snapshot.jpg",
    }
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "CasaGuard/1.0"}
    if WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {WEBHOOK_TOKEN}"
    request = urllib.request.Request(WEBHOOK_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def run() -> None:
    print("CasaGuard webhook relay started", flush=True)
    last_id = load_last_id()
    while True:
        try:
            events = get_events()
            # API returns newest first. Process in chronological order.
            people = [e for e in events if e.get("label") == "person" and e.get("end_time")]
            people.sort(key=lambda event: event.get("end_time", 0))
            if last_id:
                try:
                    start = next(i for i, event in enumerate(people) if event.get("id") == last_id) + 1
                    pending = people[start:]
                except StopIteration:
                    # State may be older than Frigate's query window. Avoid a burst
                    # of old alerts; resume from the newest event instead.
                    pending = people[-1:]
            else:
                # First run establishes a baseline instead of alerting historical video.
                if people:
                    last_id = str(people[-1]["id"])
                    save_last_id(last_id)
                pending = []
            for event in pending:
                notify(event)
                last_id = str(event["id"])
                save_last_id(last_id)
                print(f"processed person event {last_id}", flush=True)
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"relay warning: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
