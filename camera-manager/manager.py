import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

STATE = Path(os.getenv("CAMERA_STATE", "/state/cameras.json"))
CONFIG = Path(os.getenv("FRIGATE_CONFIG", "/frigate/config.yml"))
BASE = Path(os.getenv("FRIGATE_BASE", "/frigate/base.yml"))
FRIGATE_URL = os.getenv("FRIGATE_URL", "http://frigate:5000").rstrip("/")
GO2RTC_URL = os.getenv("GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/")
SCAN_SECONDS = max(10, int(os.getenv("SCAN_SECONDS", "30")))
LIGHT_SCAN_SECONDS = max(5, int(os.getenv("LIGHT_SCAN_SECONDS", "10")))

LOCK = threading.RLock()
WORKER_LOCK = threading.RLock()
RUNTIME = {}
PUBLIC_WORKERS = {}

FORMATS = {"H264": "h264", "MJPG": "mjpeg", "JPEG": "mjpeg", "HEVC": "hevc", "YUYV": "yuyv422", "NV12": "nv12"}
PRIORITY = {"h264": 4, "mjpeg": 3, "hevc": 2, "nv12": 1, "yuyv422": 0}
NIGHT_FILTERS = {
    "gentle": "eq=brightness=0.03:contrast=1.05:gamma=1.25:saturation=0.95",
    "balanced": "eq=brightness=0.05:contrast=1.08:gamma=1.45:saturation=0.92",
    "aggressive": "eq=brightness=0.08:contrast=1.12:gamma=1.75:saturation=0.90",
}
DEFAULT_NIGHT = {
    "night_mode": "auto", "night_strength": "aggressive", "dark_threshold": 40,
    "bright_threshold": 90, "night_active": False, "last_transition": 0, "night_error": "",
}
PRESERVED_FIELDS = (
    "name", "enabled", "night_mode", "night_strength", "dark_threshold", "bright_threshold",
    "night_active", "last_transition", "night_error",
)


def run(command, timeout=8):
    return subprocess.check_output(command, stderr=subprocess.STDOUT, text=True, timeout=timeout)


def slug(value):
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(value).strip()).strip("_").lower()[:40] or "camera"


def base_label(value):
    return re.sub(r"\s*\(usb-[^)]+\)\s*$", "", str(value).rstrip(":"), flags=re.IGNORECASE).strip()


def best_mode(text):
    candidates = []
    current_format = "mjpeg"
    current_size = None
    for line in text.splitlines():
        match = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if match:
            current_format = FORMATS.get(match.group(1).upper(), match.group(1).lower())
            current_size = None
            continue
        match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if match:
            current_size = (int(match.group(1)), int(match.group(2)))
            continue
        match = re.search(r"Interval:\s+Discrete.*\((\d+(?:\.\d+)?)\s+fps\)", line)
        if current_size and match:
            candidates.append((*current_size, float(match.group(1)), current_format))
    if not candidates:
        return 640, 480, 5.0, "mjpeg"
    return max(candidates, key=lambda mode: (mode[0] * mode[1], PRIORITY.get(mode[3], 0), mode[2]))


def parse_v4l2_groups(text):
    groups = []
    current = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = {"label": line.rstrip(":"), "nodes": []}
            groups.append(current)
        elif current and "/dev/video" in line:
            current["nodes"].append(line.strip())
    return [group for group in groups if group["nodes"]]


def is_capture_node(path):
    try:
        info = run(["v4l2-ctl", "--device", path, "--get-fmt-video"], 5)
    except Exception:
        return False
    return "Width/Height" in info or "Pixel Format" in info


def read_usb_attributes(path):
    try:
        device = (Path("/sys/class/video4linux") / Path(path).name / "device").resolve()
    except Exception:
        return {}
    for parent in (device, *device.parents):
        vendor = parent / "idVendor"
        product = parent / "idProduct"
        if not vendor.exists() or not product.exists():
            continue
        values = {"vendor": vendor.read_text().strip().lower(), "product": product.read_text().strip().lower()}
        serial = parent / "serial"
        if serial.exists():
            values["serial"] = serial.read_text().strip()
        return values
    return {}


def usb_identity(attributes, label):
    vendor = attributes.get("vendor", "")
    product = attributes.get("product", "")
    serial = attributes.get("serial", "")
    if vendor and product and serial:
        raw, stable = f"{vendor}:{product}:{serial}", True
    elif vendor and product:
        # Many inexpensive UVC devices omit a serial. Vendor/product still survives a port move,
        # but is not marked confidently unique because two identical models can share it.
        raw, stable = f"{vendor}:{product}", False
    else:
        raw, stable = f"label:{label}", False
    return hashlib.sha256(raw.encode()).hexdigest()[:10], stable


def with_night_defaults(camera):
    for key, value in DEFAULT_NIGHT.items():
        camera.setdefault(key, value)
    return camera


def discover_usb():
    try:
        groups = parse_v4l2_groups(run(["v4l2-ctl", "--list-devices"], 5))
    except Exception:
        groups = [{"label": path.name, "nodes": [str(path)]} for path in sorted(Path("/dev").glob("video*"))]
    cameras, used_names, used_ids = [], set(), set()
    for group in groups:
        path = next((node for node in group["nodes"] if is_capture_node(node)), None)
        if not path:
            continue
        try:
            width, height, fps, input_format = best_mode(run(["v4l2-ctl", "--device", path, "--list-formats-ext"], 5))
        except Exception:
            width, height, fps, input_format = 640, 480, 5.0, "mjpeg"
        digest, stable = usb_identity(read_usb_attributes(path), base_label(group["label"]))
        camera_id = f"usb_{digest}"
        if camera_id in used_ids:
            # Identical cameras without serials cannot be distinguished across port moves. Keep
            # both visible and let migration avoid making an unsafe match.
            camera_id += "_" + hashlib.sha256(group["label"].encode()).hexdigest()[:4]
            stable = False
        used_ids.add(camera_id)
        name = slug(group["label"].split("(", 1)[0])
        if name in used_names:
            name = f"{name}_{digest[:4]}"
        used_names.add(name)
        cameras.append(with_night_defaults({
            "id": camera_id, "kind": "usb", "name": name, "label": group["label"],
            "base_label": base_label(group["label"]), "hardware_id": digest, "stable_hardware_id": stable,
            "path": path, "enabled": True, "status": "available", "width": width, "height": height,
            "fps": fps, "input_format": input_format,
        }))
    return cameras


def discover_onvif(timeout=2.0):
    message_id, found = uuid.uuid4(), {}
    probe = f'''<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{message_id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''.encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.25)
    try:
        sock.sendto(probe, ("239.255.255.250", 3702))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload, address = sock.recvfrom(65535)
                root = ElementTree.fromstring(payload)
                xaddrs = [node.text for node in root.iter() if node.tag.endswith("XAddrs") and node.text]
                endpoint = next((node.text for node in root.iter() if node.tag.endswith("Address") and node.text), address[0])
                key = hashlib.sha256(endpoint.encode()).hexdigest()[:10]
                found[key] = with_night_defaults({
                    "id": f"onvif_{key}", "kind": "network", "name": f"onvif_{slug(address[0])}",
                    "label": f"ONVIF camera at {address[0]}", "host": address[0], "onvif_urls": xaddrs,
                    "path": "", "enabled": False, "status": "needs_stream_url",
                })
            except socket.timeout:
                continue
            except Exception:
                continue
    finally:
        sock.close()
    return list(found.values())


def probe_stream(path):
    try:
        data = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=codec_name,width,height,avg_frame_rate", "-of", "json", path], 12))
        stream = data["streams"][0]
        numerator, denominator = (stream.get("avg_frame_rate") or "0/1").split("/", 1)
        fps = float(numerator) / max(float(denominator), 1)
        return int(stream["width"]), int(stream["height"]), fps or 5.0, stream.get("codec_name", "unknown")
    except Exception:
        return None


def load_state():
    try:
        data = json.loads(STATE.read_text())
        return {camera_id: with_night_defaults(camera) for camera_id, camera in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(cameras):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(cameras, indent=2, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(STATE)


def public_state(cameras):
    safe = json.loads(json.dumps(cameras))
    for camera_id, camera in safe.items():
        camera["path"] = re.sub(r"(?<=://)[^/@]+@", "***:***@", camera.get("path", ""))
        camera.pop("stable_hardware_id", None)
        runtime = RUNTIME.get(camera_id, {})
        camera["luminance"] = runtime.get("luminance")
        camera["night_status"] = runtime.get("status", "starting")
        camera["last_sample"] = runtime.get("last_sample", 0)
        camera["monitor_error"] = runtime.get("error", "")
        camera["pipeline_error"] = runtime.get("worker_error", "")
        worker = PUBLIC_WORKERS.get(camera_id)
        camera["pipeline_running"] = bool(worker and worker["process"].poll() is None)
    return safe


def source_stream_name(camera):
    return f"{slug(camera['name'])}__source"


def raw_stream_source(camera):
    if camera["kind"] == "usb":
        input_format = camera.get("input_format", "mjpeg")
        output = "copy" if input_format in ("h264", "mjpeg", "hevc") else "h264"
        return (f'ffmpeg:device?video={camera["path"]}&input_format={camera.get("input_format", "mjpeg")}'
                f'&video_size={camera.get("width", 640)}x{camera.get("height", 480)}'
                f'&framerate={float(camera.get("fps", 5)):g}#video={output}')
    return camera["path"]


def placeholder_stream_source(camera):
    # Frigate removes source-less go2rtc streams during its security pass. A loopback RTSP
    # placeholder is safe (no shell/exec capability) and keeps the destination registered until
    # the manager attaches the real external producer through go2rtc's local ingest API.
    return f"rtsp://127.0.0.1:8554/__casaguard_wait_{slug(camera['name'])}"


def render(cameras):
    enabled = [c for c in cameras.values() if c.get("enabled") and c.get("path") and c.get("status") != "offline"]
    lines = ["# Generated by CasaGuard. Manage cameras at port 8971.", "go2rtc:",
             "  streams:" if enabled else "  streams: {}"]
    for camera in enabled:
        name = slug(camera["name"])
        lines += [f"    {source_stream_name(camera)}:", f"      - {json.dumps(raw_stream_source(camera))}",
                  f"    {name}: # CasaGuard profile: {'night' if camera.get('night_active') else 'day'}",
                  f"      - {json.dumps(placeholder_stream_source(camera))}"]
    if not enabled:
        lines.append("cameras: {}")
        return "\n".join(lines) + "\n"
    lines.append("cameras:")
    for camera in enabled:
        name = slug(camera["name"])
        detect_fps = min(float(camera.get("fps", 5)), 5)
        lines += [f"  {name}:", "    enabled: true", "    ffmpeg:", "      inputs:",
                  f"        - path: rtsp://127.0.0.1:8554/{name}", "          input_args: preset-rtsp-restream",
                  "          roles:", "            - detect", "            - record", "    detect:",
                  "      enabled: true", f"      width: {int(camera.get('width', 640))}",
                  f"      height: {int(camera.get('height', 480))}", f"      fps: {detect_fps:g}",
                  "    live:", "      streams:", f"        Native: {name}", "    record:",
                  "      enabled: true", "    snapshots:", "      enabled: true"]
    return "\n".join(lines) + "\n"


def write_config(cameras):
    base = BASE.read_text().rstrip() if BASE.exists() else "mqtt:\n  enabled: false"
    generated = base + "\n\n" + render(cameras)
    if CONFIG.exists() and CONFIG.read_text() == generated:
        return False
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(generated)
    temporary.chmod(0o600)
    temporary.replace(CONFIG)
    return True


def restart_frigate():
    try:
        request = urllib.request.Request(FRIGATE_URL + "/api/restart", method="POST", data=b"")
        urllib.request.urlopen(request, timeout=8).read()
        return True
    except Exception:
        return False


def fetch_frame(stream_name, timeout=10):
    query = urllib.parse.urlencode({"src": stream_name, "width": 32, "height": 18})
    with urllib.request.urlopen(f"{GO2RTC_URL}/api/frame.jpeg?{query}", timeout=timeout) as response:
        payload = response.read(2_000_000)
    if not payload.startswith(b"\xff\xd8"):
        raise ValueError("go2rtc did not return a JPEG frame")
    return payload


def jpeg_luminance(payload):
    process = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf", "scale=1:1:flags=area,format=gray",
         "-frames:v", "1", "-f", "rawvideo", "pipe:1"], input=payload, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=8, check=True,
    )
    if len(process.stdout) != 1:
        raise ValueError("unable to calculate frame luminance")
    return process.stdout[0]


def sample_luminance(camera, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return jpeg_luminance(fetch_frame(source_stream_name(camera)))
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.25)
    raise last_error


def evaluate_light(camera, luminance, runtime, now=None):
    now = time.time() if now is None else now
    mode, active = camera.get("night_mode", "auto"), bool(camera.get("night_active"))
    if mode == "night":
        return True if not active else None
    if mode == "day":
        return False if active else None
    dark_threshold, bright_threshold = int(camera.get("dark_threshold", 40)), int(camera.get("bright_threshold", 90))
    dwell_ready = now - float(camera.get("last_transition", 0)) >= 300
    for key in ("dark_samples", "bright_samples", "extreme_samples"):
        runtime.setdefault(key, 0)
    if active:
        runtime["dark_samples"] = 0
        runtime["bright_samples"] = runtime["bright_samples"] + 1 if luminance >= bright_threshold else 0
        runtime["extreme_samples"] = runtime["extreme_samples"] + 1 if luminance >= 160 else 0
        if runtime["extreme_samples"] >= 2 or (dwell_ready and runtime["bright_samples"] >= 3):
            return False
    else:
        runtime["bright_samples"] = runtime["extreme_samples"] = 0
        runtime["dark_samples"] = runtime["dark_samples"] + 1 if luminance <= dark_threshold else 0
        if dwell_ready and runtime["dark_samples"] >= 6:
            return True
    return None


def encoder_args(camera, night_active=None):
    night_active = bool(camera.get("night_active")) if night_active is None else bool(night_active)
    if not night_active and camera.get("input_format") == "h264":
        return ["-c:v", "copy"]
    args = []
    if night_active:
        strength = camera.get("night_strength", "aggressive")
        args += ["-vf", NIGHT_FILTERS.get(strength, NIGHT_FILTERS["aggressive"])]
    return args + ["-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
                   "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
                   "-g", str(max(1, round(float(camera.get("fps", 5)) * 2)))]


def public_worker_command(camera):
    source = urllib.parse.quote(source_stream_name(camera), safe="")
    destination = urllib.parse.quote(slug(camera["name"]), safe="")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
            "-i", f"rtsp://127.0.0.1:8554/{source}", "-map", "0:v:0", "-an",
            *encoder_args(camera), "-f", "mpegts", "-method", "POST",
            f"{GO2RTC_URL}/api/stream.ts?dst={destination}"]


def worker_signature(camera):
    return (source_stream_name(camera), slug(camera["name"]), camera.get("input_format"),
            float(camera.get("fps", 5)), bool(camera.get("night_active")), camera.get("night_strength"))


def stop_public_worker(camera_id):
    entry = PUBLIC_WORKERS.pop(camera_id, None)
    if not entry:
        return
    process = entry["process"]
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def start_public_worker(camera_id, camera):
    process = subprocess.Popen(public_worker_command(camera), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True)
    PUBLIC_WORKERS[camera_id] = {"process": process, "signature": worker_signature(camera), "started": time.monotonic()}
    RUNTIME.setdefault(camera_id, {})["pipeline_pid"] = process.pid
    return process


def replace_public_worker(camera_id, camera, verify=True):
    with WORKER_LOCK:
        stop_public_worker(camera_id)
        process = start_public_worker(camera_id, camera)
    if not verify:
        return process
    deadline, last_error = time.monotonic() + 20, None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"camera pipeline exited with status {process.returncode}")
        try:
            fetch_frame(slug(camera["name"]), timeout=4)
            return process
        except Exception as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"replacement stream did not produce a frame: {last_error}")


def supervise_workers_once():
    cameras = load_state()
    active = {camera_id: camera for camera_id, camera in cameras.items()
              if camera.get("enabled") and camera.get("path") and camera.get("status") != "offline"}
    with WORKER_LOCK:
        for camera_id in list(PUBLIC_WORKERS):
            if camera_id not in active:
                stop_public_worker(camera_id)
        for camera_id, camera in active.items():
            entry = PUBLIC_WORKERS.get(camera_id)
            if entry and entry["process"].poll() is None and entry["signature"] == worker_signature(camera):
                continue
            if entry:
                stop_public_worker(camera_id)
            runtime = RUNTIME.setdefault(camera_id, {})
            if time.monotonic() < runtime.get("worker_retry_at", 0):
                continue
            try:
                start_public_worker(camera_id, camera)
                runtime.update({"worker_retry_at": time.monotonic() + 5, "worker_error": ""})
            except Exception as error:
                runtime.update({"worker_error": str(error), "worker_retry_at": time.monotonic() + 10})


def apply_stream_state(cameras, camera_id, target, reason, force=False):
    camera = cameras[camera_id]
    if not force and bool(camera.get("night_active")) == bool(target):
        return True
    camera["night_active"] = bool(target)
    camera["last_transition"] = int(time.time())
    camera["night_error"] = ""
    save_state(cameras)
    write_config(cameras)
    runtime = RUNTIME.setdefault(camera_id, {})
    runtime.update({"status": "switching", "reason": reason, "dark_samples": 0, "bright_samples": 0, "extreme_samples": 0})
    try:
        replace_public_worker(camera_id, camera)
        runtime["status"] = "night" if target else "day"
        return True
    except Exception as error:
        camera["night_error"] = f"live switch failed: {error}; Frigate restart requested"
        save_state(cameras)
        runtime["status"] = "fallback_restart"
        if restart_frigate():
            return True
        camera["night_error"] += " but restart failed"
        save_state(cameras)
        runtime["status"] = "error"
        return False


def merge_usb_state(cameras, discovered):
    counts = Counter(c.get("base_label") or base_label(c.get("label", "")) for c in discovered)
    claimed, result = set(), dict(cameras)
    for camera in discovered:
        previous = result.get(camera["id"])
        label_key = camera.get("base_label")
        if previous is None and counts[label_key] == 1:
            candidates = [(cid, item) for cid, item in result.items() if cid not in claimed and item.get("kind") == "usb"
                          and (item.get("base_label") or base_label(item.get("label", ""))) == label_key]
            if candidates:
                candidates.sort(key=lambda pair: pair[1].get("status") == "available", reverse=True)
                old_id, previous = candidates[0]
                claimed.add(old_id)
        if previous:
            for field in PRESERVED_FIELDS:
                if field in previous:
                    camera[field] = previous[field]
        result[camera["id"]] = with_night_defaults(camera)
        if camera.get("stable_hardware_id") and counts[label_key] == 1:
            for old_id, old in list(result.items()):
                if old_id == camera["id"] or old.get("kind") != "usb":
                    continue
                old_label = old.get("base_label") or base_label(old.get("label", ""))
                if old_label == label_key and not old.get("stable_hardware_id"):
                    result.pop(old_id, None)
                    RUNTIME.pop(old_id, None)
    return result


def scan(restart=True):
    with LOCK:
        discovered_usb = discover_usb()
        cameras = merge_usb_state(load_state(), discovered_usb)
        usb_ids = {camera["id"] for camera in discovered_usb}
        for camera_id, camera in cameras.items():
            if camera.get("kind") == "usb" and camera_id not in usb_ids:
                camera["status"] = "offline"
        for discovered in discover_onvif():
            cameras.setdefault(discovered["id"], discovered)
        save_state(cameras)
        changed = write_config(cameras)
    if changed and restart:
        restart_frigate()
    return cameras


def monitor_once():
    with LOCK:
        cameras = load_state()
        camera_ids = [cid for cid, camera in cameras.items() if camera.get("enabled") and camera.get("path") and camera.get("status") != "offline"]
    for camera_id in camera_ids:
        with LOCK:
            cameras = load_state()
            camera = cameras.get(camera_id)
            if not camera:
                continue
        runtime = RUNTIME.setdefault(camera_id, {})
        try:
            luminance = sample_luminance(camera)
            runtime.update({"luminance": luminance, "last_sample": int(time.time()), "status": "monitoring", "error": ""})
            target = evaluate_light(camera, luminance, runtime)
            if target is not None:
                with LOCK:
                    cameras = load_state()
                    if camera_id in cameras:
                        apply_stream_state(cameras, camera_id, target, "automatic light transition")
            elif runtime.get("status") == "monitoring":
                runtime["status"] = "night" if camera.get("night_active") else "day"
        except Exception as error:
            runtime.update({"status": "sample_error", "last_sample": int(time.time()), "error": str(error)})


INDEX = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CasaGuard Cameras</title><style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:0 16px;background:#111;color:#eee}button,input,select{padding:9px;margin:4px;background:#171717;color:#eee;border:1px solid #555;border-radius:5px}article{background:#222;padding:14px;margin:10px 0;border-radius:10px}.ok{color:#5f5}.bad{color:#f88}.meta{color:#bbb;line-height:1.7}.controls{display:flex;flex-wrap:wrap;align-items:center;margin-top:8px}label{font-size:13px;color:#ccc}</style><h1>CasaGuard cameras</h1><button onclick="rescan()">Rescan USB and ONVIF</button><div id="list"></div><h2>Add RTSP camera</h2><input id="name" placeholder="Camera name"><input id="url" size="55" placeholder="rtsp://user:password@camera/stream"><button onclick="add()">Add</button><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function patchCamera(id,body){let r=await fetch('/api/cameras/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)alert((await r.json()).error);await load()}
async function load(){let c=await fetch('/api/cameras').then(r=>r.json());list.innerHTML=Object.values(c).map(x=>`<article><b>${esc(x.name)}</b> <span class="${x.status==='available'||x.status==='configured'?'ok':'bad'}">${esc(x.status)}</span><div class="meta">${esc(x.kind)} · ${esc(x.width||'?')}×${esc(x.height||'?')} @ ${esc(x.fps||'?')} fps · ${esc(x.input_format||'')}<br>Light: ${x.luminance??'waiting'} / 255 · Active profile: <b>${x.night_active?'Night':'Day'}</b> · Monitor: ${esc(x.night_status)} · Pipeline: <span class="${x.pipeline_running?'ok':'bad'}">${x.pipeline_running?'running':'reconnecting'}</span>${x.last_transition?'<br>Last switch: '+new Date(x.last_transition*1000).toLocaleString():''}${x.night_error?'<br><span class="bad">'+esc(x.night_error)+'</span>':''}${x.monitor_error?'<br><span class="bad">Monitor: '+esc(x.monitor_error)+'</span>':''}${x.pipeline_error?'<br><span class="bad">Pipeline: '+esc(x.pipeline_error)+'</span>':''}</div><div class="controls"><label>Mode <select onchange="patchCamera('${esc(x.id)}',{night_mode:this.value})"><option value="auto" ${x.night_mode==='auto'?'selected':''}>Auto</option><option value="day" ${x.night_mode==='day'?'selected':''}>Day</option><option value="night" ${x.night_mode==='night'?'selected':''}>Night</option></select></label><label>Strength <select onchange="patchCamera('${esc(x.id)}',{night_strength:this.value})"><option value="gentle" ${x.night_strength==='gentle'?'selected':''}>Gentle</option><option value="balanced" ${x.night_strength==='balanced'?'selected':''}>Balanced</option><option value="aggressive" ${x.night_strength==='aggressive'?'selected':''}>Aggressive</option></select></label><label>Dark ≤ <input type="number" min="0" max="255" value="${esc(x.dark_threshold)}" onchange="patchCamera('${esc(x.id)}',{dark_threshold:Number(this.value)})" size="4"></label><label>Bright ≥ <input type="number" min="0" max="255" value="${esc(x.bright_threshold)}" onchange="patchCamera('${esc(x.id)}',{bright_threshold:Number(this.value)})" size="4"></label></div></article>`).join('')}
async function rescan(){await fetch('/api/rescan',{method:'POST'});load()}async function add(){let r=await fetch('/api/cameras',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.value,path:url.value})});if(!r.ok)alert((await r.json()).error);load()}load();setInterval(load,10000);</script>'''.encode()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/":
            self.reply(200, INDEX, "text/html; charset=utf-8")
        elif self.path == "/api/cameras":
            self.reply(200, public_state(load_state()))
        else:
            self.send_error(404)

    def do_PATCH(self):
        match = re.fullmatch(r"/api/cameras/([^/]+)", self.path)
        if not match:
            self.send_error(404)
            return
        camera_id = urllib.parse.unquote(match.group(1))
        try:
            data = self.read_json()
            allowed = {"night_mode", "night_strength", "dark_threshold", "bright_threshold"}
            if not data or not set(data).issubset(allowed):
                raise ValueError("Only night_mode, night_strength, dark_threshold, and bright_threshold may be changed")
            with LOCK:
                cameras = load_state()
                if camera_id not in cameras:
                    self.reply(404, {"error": "Camera not found"})
                    return
                camera = cameras[camera_id]
                old_strength = camera.get("night_strength")
                if "night_mode" in data:
                    if data["night_mode"] not in ("auto", "day", "night"):
                        raise ValueError("night_mode must be auto, day, or night")
                    camera["night_mode"] = data["night_mode"]
                if "night_strength" in data:
                    if data["night_strength"] not in NIGHT_FILTERS:
                        raise ValueError("night_strength must be gentle, balanced, or aggressive")
                    camera["night_strength"] = data["night_strength"]
                for field in ("dark_threshold", "bright_threshold"):
                    if field in data:
                        value = int(data[field])
                        if not 0 <= value <= 255:
                            raise ValueError(f"{field} must be between 0 and 255")
                        camera[field] = value
                if int(camera["dark_threshold"]) >= int(camera["bright_threshold"]):
                    raise ValueError("dark_threshold must be lower than bright_threshold")
                save_state(cameras)
                target = False if camera["night_mode"] == "day" else True if camera["night_mode"] == "night" else None
                force = bool(camera.get("night_active")) and old_strength != camera.get("night_strength")
                if target is not None:
                    apply_stream_state(cameras, camera_id, target, "manual mode", force=force)
                elif force:
                    apply_stream_state(cameras, camera_id, bool(camera.get("night_active")), "night strength changed", force=True)
                else:
                    write_config(cameras)
                self.reply(200, public_state({camera_id: cameras[camera_id]})[camera_id])
        except Exception as error:
            self.reply(400, {"error": str(error)})

    def do_POST(self):
        if self.path == "/api/rescan":
            self.reply(200, public_state(scan()))
            return
        if self.path != "/api/cameras":
            self.send_error(404)
            return
        try:
            data = self.read_json()
            path = str(data.get("path", "")).strip()
            if not path.startswith(("rtsp://", "rtsps://", "http://", "https://")):
                self.reply(400, {"error": "A valid RTSP/HTTP camera URL is required"})
                return
            details = probe_stream(path)
            if not details:
                self.reply(400, {"error": "The camera stream could not be opened"})
                return
            width, height, fps, codec = details
            camera_id = "network_" + hashlib.sha256(path.encode()).hexdigest()[:10]
            with LOCK:
                cameras = load_state()
                cameras[camera_id] = with_night_defaults({"id": camera_id, "kind": "network",
                    "name": slug(data.get("name", camera_id)), "label": data.get("name", camera_id), "path": path,
                    "enabled": True, "status": "configured", "width": width, "height": height, "fps": fps,
                    "input_format": codec})
                save_state(cameras)
                changed = write_config(cameras)
            if changed:
                restart_frigate()
            self.reply(201, public_state({camera_id: cameras[camera_id]})[camera_id])
        except Exception as error:
            self.reply(400, {"error": str(error)})

    def log_message(self, *_):
        pass


def scan_loop():
    while True:
        time.sleep(SCAN_SECONDS)
        try:
            scan()
        except Exception as error:
            print(f"camera scan failed: {error}", flush=True)


def monitor_loop():
    while True:
        time.sleep(LIGHT_SCAN_SECONDS)
        try:
            monitor_once()
        except Exception as error:
            print(f"night monitor failed: {error}", flush=True)


def worker_loop():
    while True:
        try:
            supervise_workers_once()
        except Exception as error:
            print(f"camera pipeline supervisor failed: {error}", flush=True)
        time.sleep(2)


def main():
    scan(restart=True)
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8971), Handler).serve_forever()


if __name__ == "__main__":
    main()
