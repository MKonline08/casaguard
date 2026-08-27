import hashlib, json, os, re, socket, subprocess, threading, time, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

STATE=Path(os.getenv('CAMERA_STATE','/state/cameras.json')); CONFIG=Path(os.getenv('FRIGATE_CONFIG','/frigate/config.yml')); BASE=Path(os.getenv('FRIGATE_BASE','/frigate/base.yml'))
FRIGATE_URL=os.getenv('FRIGATE_URL','http://frigate:5000'); SCAN_SECONDS=max(10,int(os.getenv('SCAN_SECONDS','30'))); LOCK=threading.RLock()
FORMATS={'H264':'h264','MJPG':'mjpeg','JPEG':'mjpeg','HEVC':'hevc','YUYV':'yuyv422','NV12':'nv12'}
PRIORITY={'h264':4,'mjpeg':3,'hevc':2,'nv12':1,'yuyv422':0}

def run(command,timeout=8): return subprocess.check_output(command,stderr=subprocess.STDOUT,text=True,timeout=timeout)
def slug(value): return (re.sub(r'[^a-zA-Z0-9_]+','_',str(value).strip()).strip('_').lower()[:40] or 'camera')

def best_mode(text):
    candidates=[]; current_format='mjpeg'; current_size=None
    for line in text.splitlines():
        match=re.search(r"\[\d+\]:\s+'([^']+)'",line)
        if match: current_format=FORMATS.get(match.group(1).upper(),match.group(1).lower()); current_size=None; continue
        match=re.search(r'Size:\s+Discrete\s+(\d+)x(\d+)',line)
        if match: current_size=(int(match.group(1)),int(match.group(2))); continue
        match=re.search(r'Interval:\s+Discrete.*\((\d+(?:\.\d+)?)\s+fps\)',line)
        if current_size and match: candidates.append((*current_size,float(match.group(1)),current_format))
    if not candidates: return 640,480,5.0,'mjpeg'
    return max(candidates,key=lambda mode:(mode[0]*mode[1],PRIORITY.get(mode[3],0),mode[2]))

def parse_v4l2_groups(text):
    groups=[]; current=None
    for line in text.splitlines():
        if line and not line[0].isspace(): current={'label':line.rstrip(':'),'nodes':[]}; groups.append(current)
        elif current and '/dev/video' in line: current['nodes'].append(line.strip())
    return [group for group in groups if group['nodes']]

def is_capture_node(path):
    try:
        info=run(['v4l2-ctl','--device',path,'--get-fmt-video'],5)
    except Exception: return False
    return 'Width/Height' in info or 'Pixel Format' in info

def discover_usb():
    try: groups=parse_v4l2_groups(run(['v4l2-ctl','--list-devices'],5))
    except Exception: groups=[{'label':path.name,'nodes':[str(path)]} for path in sorted(Path('/dev').glob('video*'))]
    cameras=[]; used_names=set()
    for group in groups:
        path=next((node for node in group['nodes'] if is_capture_node(node)),None)
        if not path: continue
        try: width,height,fps,input_format=best_mode(run(['v4l2-ctl','--device',path,'--list-formats-ext'],5))
        except Exception: width,height,fps,input_format=640,480,5.0,'mjpeg'
        digest=hashlib.sha256(group['label'].encode()).hexdigest()[:10]; camera_id=f'usb_{digest}'; name=slug(group['label'].split('(',1)[0])
        if name in used_names: name=f'{name}_{digest[:4]}'
        used_names.add(name)
        cameras.append({'id':camera_id,'kind':'usb','name':name,'label':group['label'],'path':path,'enabled':True,'status':'available','width':width,'height':height,'fps':fps,'input_format':input_format})
    return cameras

def discover_onvif(timeout=2.0):
    message_id=uuid.uuid4(); found={}
    probe=f'''<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{message_id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''.encode()
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP); sock.settimeout(0.25)
    try:
        sock.sendto(probe,('239.255.255.250',3702)); deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                payload,address=sock.recvfrom(65535); root=ElementTree.fromstring(payload)
                xaddrs=[node.text for node in root.iter() if node.tag.endswith('XAddrs') and node.text]
                endpoint=next((node.text for node in root.iter() if node.tag.endswith('Address') and node.text),address[0]); key=hashlib.sha256(endpoint.encode()).hexdigest()[:10]
                found[key]={'id':f'onvif_{key}','kind':'network','name':f'onvif_{slug(address[0])}','label':f'ONVIF camera at {address[0]}','host':address[0],'onvif_urls':xaddrs,'path':'','enabled':False,'status':'needs_stream_url'}
            except socket.timeout: continue
            except Exception: continue
    finally: sock.close()
    return list(found.values())

def probe_stream(path):
    try:
        data=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=codec_name,width,height,avg_frame_rate','-of','json',path],12)); stream=data['streams'][0]
        numerator,denominator=(stream.get('avg_frame_rate') or '0/1').split('/',1); fps=float(numerator)/max(float(denominator),1)
        return int(stream['width']),int(stream['height']),fps or 5.0,stream.get('codec_name','unknown')
    except Exception: return None

def load_state():
    try:
        data=json.loads(STATE.read_text()); return data if isinstance(data,dict) else {}
    except Exception: return {}

def save_state(cameras):
    STATE.parent.mkdir(parents=True,exist_ok=True); temporary=STATE.with_suffix('.tmp'); temporary.write_text(json.dumps(cameras,indent=2,sort_keys=True)); temporary.chmod(0o600); temporary.replace(STATE)

def public_state(cameras):
    safe=json.loads(json.dumps(cameras))
    for camera in safe.values():
        path=camera.get('path','')
        camera['path']=re.sub(r'(?<=://)[^/@]+@','***:***@',path)
    return safe

def render(cameras):
    enabled=[camera for camera in cameras.values() if camera.get('enabled') and camera.get('path') and camera.get('status')!='offline']
    lines=['# Generated by CasaGuard. Manage cameras at port 8971.','go2rtc:','  streams:' if enabled else '  streams: {}']
    for camera in enabled:
        name=slug(camera['name'])
        if camera['kind']=='usb':
            input_format=camera.get('input_format','mjpeg'); output='copy' if input_format=='h264' else 'h264'
            source=f'ffmpeg:device?video={camera["path"]}&input_format={input_format}&video_size={camera["width"]}x{camera["height"]}&framerate={camera["fps"]:g}#video={output}'
        else:
            output='copy' if camera.get('input_format')=='h264' else 'h264'; source=f'ffmpeg:{camera["path"]}#video={output}'
        lines += [f'    {name}:',f'      - {json.dumps(source)}']
    if not enabled: lines.append('cameras: {}'); return '\n'.join(lines)+'\n'
    lines.append('cameras:')
    for camera in enabled:
        name=slug(camera['name']); detect_fps=min(float(camera.get('fps',5)),5)
        lines += [f'  {name}:','    enabled: true','    ffmpeg:','      inputs:',f'        - path: rtsp://127.0.0.1:8554/{name}','          input_args: preset-rtsp-restream','          roles:','            - detect','            - record','    detect:',f'      width: {int(camera.get("width",640))}',f'      height: {int(camera.get("height",480))}',f'      fps: {detect_fps:g}','    live:','      streams:',f'        Native: {name}','    record:','      enabled: true','    snapshots:','      enabled: true']
    return '\n'.join(lines)+'\n'

def write_config(cameras):
    base=BASE.read_text().rstrip() if BASE.exists() else 'mqtt:\n  enabled: false'; generated=base+'\n\n'+render(cameras)
    if CONFIG.exists() and CONFIG.read_text()==generated: return False
    CONFIG.parent.mkdir(parents=True,exist_ok=True); temporary=CONFIG.with_suffix('.tmp'); temporary.write_text(generated); temporary.replace(CONFIG); return True

def restart_frigate():
    try:
        request=urllib.request.Request(FRIGATE_URL+'/api/restart',method='POST',data=b''); urllib.request.urlopen(request,timeout=5).read(); return True
    except Exception: return False

def scan(restart=True):
    with LOCK:
        cameras=load_state(); usb=discover_usb(); usb_ids={camera['id'] for camera in usb}
        for camera_id,camera in list(cameras.items()):
            if camera.get('kind')=='usb' and camera_id not in usb_ids: camera['status']='offline'
        for discovered in usb:
            previous=cameras.get(discovered['id'],{}); discovered['name']=previous.get('name',discovered['name']); discovered['enabled']=previous.get('enabled',True); cameras[discovered['id']]=discovered
        for discovered in discover_onvif(): cameras.setdefault(discovered['id'],discovered)
        save_state(cameras); changed=write_config(cameras)
    if changed and restart: restart_frigate()
    return cameras

INDEX='''<!doctype html><meta charset="utf-8"><title>CasaGuard Cameras</title><style>body{font:16px system-ui;max-width:900px;margin:40px auto;background:#111;color:#eee}button,input{padding:9px;margin:4px}article{background:#222;padding:14px;margin:10px 0;border-radius:10px}.ok{color:#5f5}.bad{color:#f88}</style><h1>CasaGuard cameras</h1><button onclick="rescan()">Rescan USB and ONVIF</button><div id="list"></div><h2>Add RTSP camera</h2><input id="name" placeholder="Camera name"><input id="url" size="55" placeholder="rtsp://user:password@camera/stream"><button onclick="add()">Add</button><script>async function load(){let c=await fetch('/api/cameras').then(r=>r.json());list.innerHTML=Object.values(c).map(x=>`<article><b>${x.name}</b> <span class="${x.status==='available'||x.status==='configured'?'ok':'bad'}">${x.status}</span><br>${x.kind} · ${x.width||'?'}×${x.height||'?'} @ ${x.fps||'?'} fps · ${x.input_format||''}</article>`).join('')}async function rescan(){await fetch('/api/rescan',{method:'POST'});load()}async function add(){let r=await fetch('/api/cameras',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.value,path:url.value})});if(!r.ok)alert((await r.json()).error);load()}load();</script>'''.encode()

class Handler(BaseHTTPRequestHandler):
    def reply(self,status,payload,content_type='application/json'):
        body=payload if isinstance(payload,bytes) else json.dumps(payload).encode(); self.send_response(status); self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path=='/': self.reply(200,INDEX,'text/html; charset=utf-8')
        elif self.path=='/api/cameras': self.reply(200,public_state(load_state()))
        else: self.send_error(404)
    def do_POST(self):
        if self.path=='/api/rescan': self.reply(200,scan()); return
        if self.path!='/api/cameras': self.send_error(404); return
        try:
            length=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(length) or b'{}'); path=str(data.get('path','')).strip()
            if not path.startswith(('rtsp://','rtsps://','http://','https://')): self.reply(400,{'error':'A valid RTSP/HTTP camera URL is required'}); return
            details=probe_stream(path)
            if not details: self.reply(400,{'error':'The camera stream could not be opened'}); return
            width,height,fps,codec=details; camera_id='network_'+hashlib.sha256(path.encode()).hexdigest()[:10]
            with LOCK:
                cameras=load_state(); cameras[camera_id]={'id':camera_id,'kind':'network','name':slug(data.get('name',camera_id)),'label':data.get('name',camera_id),'path':path,'enabled':True,'status':'configured','width':width,'height':height,'fps':fps,'input_format':codec}; save_state(cameras); changed=write_config(cameras)
            if changed: restart_frigate()
            self.reply(201,public_state({camera_id:cameras[camera_id]})[camera_id])
        except Exception as error: self.reply(400,{'error':str(error)})
    def log_message(self,*_): pass

def loop():
    while True:
        time.sleep(SCAN_SECONDS)
        try: scan()
        except Exception as error: print(f'camera scan failed: {error}',flush=True)

def main():
    scan(restart=False); threading.Thread(target=loop,daemon=True).start(); ThreadingHTTPServer(('0.0.0.0',8971),Handler).serve_forever()

if __name__=='__main__': main()
