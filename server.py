#!/usr/bin/env python3
"""VidCrop: local web UI to arrange/crop videos on a canvas, exported with ffmpeg.
Run: python3 -I server.py [port]   (VIDCROP_VIDEOS=<dir> sets the folder auto-loaded at startup)"""
import hashlib, json, os, re, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.environ.get('VIDCROP_VIDEOS', '/Users/gupmadha/Desktop/whiteboardf/videos')
UPLOADS = os.path.join(APP, 'uploads')
FILES = {}                      # id -> {path,name,w,h,dur}
JOB = {'status': 'idle', 'progress': 0, 'error': '', 'out': ''}
PROC = None


def probe(path):
    o = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height:format=duration', '-of', 'json', path],
                       capture_output=True, text=True, check=True).stdout
    j = json.loads(o)
    return {'w': j['streams'][0]['width'], 'h': j['streams'][0]['height'], 'dur': float(j['format']['duration'])}


def add_file(path):
    path = os.path.abspath(path)
    fid = hashlib.sha1(path.encode()).hexdigest()[:10]
    if fid not in FILES:
        FILES[fid] = {'id': fid, 'path': path, 'name': os.path.basename(path), **probe(path)}
    return FILES[fid]


def osa(script):
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def pick_files():
    out = osa('set fs to choose file with prompt "Select videos" with multiple selections allowed\n'
              'set o to ""\nrepeat with f in fs\nset o to o & POSIX path of f & linefeed\nend repeat\nreturn o')
    return [p for p in out.splitlines() if p]


def pick_save():
    return osa(f'POSIX path of (choose file name with prompt "Export as" default name "export.mp4" '
               f'default location POSIX file "{VIDEOS}")')


def build_cmd(req):
    W, H, fps = int(req['canvas']['w']) // 2 * 2, int(req['canvas']['h']) // 2 * 2, int(req['canvas']['fps'])
    bg = '0x' + req['canvas']['bg'].lstrip('#')
    layers = [l for l in req['layers'] if l.get('vis', True)]
    if not layers:
        raise ValueError('no visible layers')
    def span(l):
        f = FILES[l['id']]
        tin = float(l.get('tin', 0)); tout = float(l.get('tout', f['dur']))
        return float(l.get('start', 0)), tin, max(0.1, tout - tin)
    D = max(span(l)[0] + span(l)[2] for l in layers)
    cmd, parts, cur = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats'], [], 'b0'
    parts.append(f'color=c={bg}:s={W}x{H}:r={fps}:d={D:.3f}[b0]')
    for i, l in enumerate(layers):
        f = FILES[l['id']]
        start, tin, ln = span(l)
        cmd += ['-ss', f'{tin:.3f}', '-t', f'{ln:.3f}', '-i', f['path']]
        cx, cy = round(f['w'] * l['l']), round(f['h'] * l['t'])
        cw = max(2, round(f['w'] * (1 - l['l'] - l['r']))); ch = max(2, round(f['h'] * (1 - l['t'] - l['b'])))
        cw, ch = min(cw, f['w'] - cx), min(ch, f['h'] - cy)
        dw = max(2, round(cw * l['s']) // 2 * 2); dh = max(2, round(ch * l['s']) // 2 * 2)
        parts.append(f'[{i}:v]fps={fps},setpts=PTS-STARTPTS+{start:.3f}/TB,crop={cw}:{ch}:{cx}:{cy},scale={dw}:{dh}:flags=lanczos,setsar=1[v{i}]')
        parts.append(f'[{cur}][v{i}]overlay={round(l["x"])}:{round(l["y"])}:eof_action=pass:enable=between(t\\,{start:.3f}\\,{start + ln:.3f})[b{i + 1}]')
        cur = f'b{i + 1}'
    parts.append(f'[{cur}]format=yuv420p[out]')
    enc = (['-c:v', 'h264_videotoolbox', '-b:v', '60M'] if req.get('quality') == 'fast'
           else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16'])
    cmd += ['-filter_complex', ';'.join(parts), '-map', '[out]', '-t', f'{D:.3f}', '-r', str(fps), *enc,
            '-movflags', '+faststart', req['out']]
    return cmd, D


def run_export(cmd, D, out):
    global PROC
    JOB.update(status='running', progress=0, error='', out=out)
    PROC = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    err = []
    threading.Thread(target=lambda: err.append(PROC.stderr.read()), daemon=True).start()
    for line in PROC.stdout:
        m = re.match(r'out_time_us=(\d+)', line)
        if m:
            JOB['progress'] = min(0.99, int(m.group(1)) / 1e6 / D)
    rc = PROC.wait()
    if JOB['status'] == 'cancelled':
        return
    if rc == 0:
        JOB.update(status='done', progress=1)
    else:
        JOB.update(status='error', error=(''.join(err))[-600:] or f'ffmpeg exit {rc}')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')

    def serve_video(self, f):
        size = os.path.getsize(f['path'])
        a, b = 0, size - 1
        m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
        if m:
            if m.group(1): a = int(m.group(1))
            if m.group(2): b = int(m.group(2))
            if not m.group(1): a, b = size - int(m.group(2)), size - 1
        b = min(b, size - 1)
        self.send_response(206 if m else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(b - a + 1))
        if m: self.send_header('Content-Range', f'bytes {a}-{b}/{size}')
        self.end_headers()
        try:
            with open(f['path'], 'rb') as fh:
                fh.seek(a)
                left = b - a + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk: break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            return self.send(200, open(os.path.join(APP, 'index.html'), 'rb').read(), 'text/html')
        if u.path.startswith('/video/') and u.path[7:] in FILES:
            return self.serve_video(FILES[u.path[7:]])
        if u.path == '/api/job':
            return self.send(200, JOB)
        if u.path == '/api/defaults':
            names = ['face.mp4', 'whiteboard.mp4', 'date.mp4']
            return self.send(200, [add_file(os.path.join(VIDEOS, n)) for n in names
                                   if os.path.exists(os.path.join(VIDEOS, n))])
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        global PROC
        u = urlparse(self.path)
        try:
            if u.path == '/api/upload':      # raw body, streamed to disk
                os.makedirs(UPLOADS, exist_ok=True)
                name = os.path.basename(parse_qs(u.query)['name'][0])
                dest = os.path.join(UPLOADS, name)
                left = int(self.headers['Content-Length'])
                with open(dest, 'wb') as fh:
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk: break
                        fh.write(chunk); left -= len(chunk)
                return self.send(200, add_file(dest))
            req = self.body()
            if u.path == '/api/pick':
                return self.send(200, [add_file(p) for p in pick_files()])
            if u.path == '/api/picksave':
                return self.send(200, {'path': pick_save()})
            if u.path == '/api/export':
                if JOB['status'] == 'running':
                    return self.send(409, {'error': 'export already running'})
                cmd, D = build_cmd(req)
                threading.Thread(target=run_export, args=(cmd, D, req['out']), daemon=True).start()
                return self.send(200, {'ok': True})
            if u.path == '/api/cancel':
                if PROC and JOB['status'] == 'running':
                    JOB['status'] = 'cancelled'; PROC.terminate()
                return self.send(200, {'ok': True})
        except Exception as e:
            return self.send(400, {'error': str(e)})
        self.send(404, {'error': 'not found'})


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
    print(f'VidCrop on http://localhost:{port}')
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
