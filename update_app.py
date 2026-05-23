import os

content = """import os
import socket
import io
import base64
import uuid
import time
import subprocess
import shutil
import threading
import secrets
import string
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import qrcode
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_socketio import SocketIO, emit, join_room

# Load .env variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_interval=4,
    ping_timeout=10,
    max_http_buffer_size=10 * 1024 * 1024  # 10 MB max for high-res frames
)

PORT = int(os.environ.get('PORT', '5000'))
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
SNAPSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'snapshots')
MAX_RECORDINGS_GB = float(os.environ.get('MAX_RECORDINGS_GB', '5'))
MOTION_THRESHOLD = float(os.environ.get('MOTION_THRESHOLD', '5000'))
AUTO_RECORD_ON_MOTION = os.environ.get('AUTO_RECORD_ON_MOTION', 'False').lower() in ('true', '1', 't', 'y', 'yes')

server_start_time = time.time()
state_lock = threading.RLock()

def generate_pin():
    return "".join(secrets.choice(string.digits) for _ in range(6))

# Quality presets mapping
QUALITY_PRESETS = {
    '360p':  {'width': 480,  'height': 360},
    '480p':  {'width': 640,  'height': 480},
    '720p':  {'width': 1280, 'height': 720},
    '1080p': {'width': 1920, 'height': 1080},
}

app_state = {
    'mobile_connected': False,
    'dashboard_connected': False,
    'is_streaming': False,
    'quality': '480p',
    'fps': 15,
    'is_recording': False,
    'cameras': [],
    'active_camera': None,
    'recording_session_id': None,
    'pairing_pin': generate_pin(),
    'auto_record_on_motion': AUTO_RECORD_ON_MOTION,
    'last_motion_thumbnail': None
}

session_clients = {}
authenticated_mobiles = set()
last_frame_times = {}

recording_state = {
    'writer': None,
    'path': None,
    'start_time': None,
    'session_id': None,
    'frame_size': None,
    'frame_count': 0,
    'fps': 15,
    'last_frame': None
}

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def get_local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for test_ip in ['10.254.254.254', '192.168.254.254', '172.254.254.254']:
            try:
                s.connect((test_ip, 1))
                ip = s.getsockname()[0]
                if ip and not ip.startswith('127.'):
                    ips.add(ip)
            except Exception:
                continue
        s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if '.' in ip and not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass
    result = list(ips)
    if not result:
        result = ['127.0.0.1']
    return sorted(result)

def generate_self_signed_cert(cert_path="cert.pem", key_path="key.pem"):
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    if not shutil.which("openssl"):
        print("[WARNING] 'openssl' command line tool not found. Falling back to HTTP.")
        return None, None
    try:
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cert_path,
            "-days", "365", "-nodes",
            "-subj", "/C=US/ST=California/L=San Francisco/O=DroidCam MVP/OU=Development/CN=localhost"
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[SSL] Generated self-signed certificates: {cert_path}, {key_path}")
        return cert_path, key_path
    except Exception as e:
        print(f"[WARNING] Failed to generate certificates: {e}")
        if os.path.exists(cert_path):
            try: os.remove(cert_path)
            except: pass
        if os.path.exists(key_path):
            try: os.remove(key_path)
            except: pass
        return None, None

def generate_qr_code(url):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{img_str}"

# ---------------------------------------------------------------------------
# Storage limits
# ---------------------------------------------------------------------------
def _enforce_storage_limit():
    with state_lock:
        if not os.path.exists(RECORDINGS_DIR):
            return

        max_bytes = MAX_RECORDINGS_GB * 1024 * 1024 * 1024
        recordings = []
        total_size = 0
        for root, dirs, files in os.walk(RECORDINGS_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                if os.path.isfile(filepath):
                    try:
                        stat = os.stat(filepath)
                        recordings.append({
                            'path': filepath,
                            'size': stat.st_size,
                            'mtime': stat.st_mtime
                        })
                        total_size += stat.st_size
                    except Exception:
                        pass

        if total_size > max_bytes:
            recordings.sort(key=lambda x: x['mtime'])
            print(f"[Storage] Total size ({total_size / (1024**3):.2f} GB) exceeds limit ({MAX_RECORDINGS_GB} GB). Cleaning up...")
            for rec in recordings:
                if total_size <= max_bytes:
                    break
                try:
                    os.remove(rec['path'])
                    total_size -= rec['size']
                    print(f"[Storage] Deleted oldest recording: {rec['path']} ({rec['size'] / (1024**2):.1f} MB)")
                    parent_dir = os.path.dirname(rec['path'])
                    if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                except Exception as e:
                    print(f"[Storage] Error deleting {rec['path']}: {e}")

# ---------------------------------------------------------------------------
# Motion Detection & Pipeline
# ---------------------------------------------------------------------------
prev_motion_frame = None

def _detect_motion(frame):
    global prev_motion_frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)

    if prev_motion_frame is None:
        prev_motion_frame = gray
        return False, 0.0

    frame_diff = cv2.absdiff(prev_motion_frame, gray)
    thresh = cv2.threshold(frame_diff, 25, 255, cv2.THRESH_BINARY)[1]
    thresh = cv2.dilate(thresh, None, iterations=2)

    contours, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    contour_area_sum = sum(cv2.contourArea(c) for c in contours)
    prev_motion_frame = gray

    if contour_area_sum > MOTION_THRESHOLD:
        return True, contour_area_sum
    return False, contour_area_sum

def _create_thumbnail(frame, width=320):
    h, w = frame.shape[:2]
    new_h = int(width * (h / w))
    resized = cv2.resize(frame, (width, new_h))
    _, buffer = cv2.imencode('.jpg', resized)
    thumbnail_b64 = base64.b64encode(buffer.tobytes()).decode('utf-8')
    return f"data:image/jpeg;base64,{thumbnail_b64}"

# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------
def _start_recording():
    with state_lock:
        session_id = str(uuid.uuid4())[:8]
        date_str = datetime.now().strftime('%Y-%m-%d')
        folder = os.path.join(RECORDINGS_DIR, date_str)
        os.makedirs(folder, exist_ok=True)

        filename = f"session-{session_id}.mp4"
        filepath = os.path.join(folder, filename)

        recording_state['writer'] = None
        recording_state['path'] = filepath
        recording_state['start_time'] = None 
        recording_state['session_id'] = session_id
        recording_state['frame_size'] = None
        recording_state['frame_count'] = 0
        recording_state['fps'] = app_state['fps'] or 15
        recording_state['last_frame'] = None

        app_state['is_recording'] = True
        app_state['recording_session_id'] = session_id
        print(f"[Recording] Started session {session_id} -> {filepath}")

def _write_recording_frame(frame):
    with state_lock:
        if not app_state['is_recording']:
            return
        try:
            h, w = frame.shape[:2]
            if recording_state['writer'] is None:
                fps = recording_state['fps']
                recording_state['frame_size'] = (w, h)
                if recording_state['start_time'] is None:
                    recording_state['start_time'] = time.monotonic()
                try:
                    fourcc = cv2.VideoWriter_fourcc(*'avc1')
                    writer = cv2.VideoWriter(recording_state['path'], fourcc, fps, (w, h))
                    if not writer.isOpened():
                        raise Exception("avc1 writer failed to open")
                    recording_state['writer'] = writer
                    print(f"[Recording] Writer initialized with H264 (avc1): {w}x{h} @ {fps} FPS")
                except Exception as e:
                    print(f"[Recording] avc1 codec failed ({e}). Falling back to mp4v.")
                    try:
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        writer = cv2.VideoWriter(recording_state['path'], fourcc, fps, (w, h))
                        if not writer.isOpened():
                            raise Exception("mp4v writer failed to open")
                        recording_state['writer'] = writer
                        print(f"[Recording] Writer initialized with mp4v: {w}x{h} @ {fps} FPS")
                    except Exception as e2:
                        print(f"[Recording] Critical: Both codecs failed: {e2}")
                        return

            expected = recording_state['frame_size']
            if (w, h) != expected:
                frame = cv2.resize(frame, expected)

            now = time.monotonic()
            elapsed = now - recording_state['start_time']
            target_frame_index = int(elapsed * recording_state['fps'])

            frames_to_pad = min(target_frame_index - recording_state['frame_count'], 5)
            for _ in range(frames_to_pad):
                if recording_state['last_frame'] is not None:
                    recording_state['writer'].write(recording_state['last_frame'])
                    recording_state['frame_count'] += 1

            recording_state['writer'].write(frame)
            recording_state['last_frame'] = frame.copy()
            recording_state['frame_count'] += 1

        except Exception as e:
            print(f"[Recording] Frame write error: {e}")

def _stop_recording():
    with state_lock:
        if recording_state['writer'] is not None:
            recording_state['writer'].release()
            recording_state['writer'] = None
            duration = time.monotonic() - (recording_state['start_time'] or time.monotonic())
            print(f"[Recording] Stopped session {recording_state['session_id']} -- {recording_state['frame_count']} frames, {duration:.1f}s")
            _enforce_storage_limit()
        else:
            if recording_state['path'] and os.path.exists(recording_state['path']):
                try: os.remove(recording_state['path'])
                except: pass
            print(f"[Recording] Stopped session {recording_state['session_id']} (no frames captured)")

        recording_state['path'] = None
        recording_state['start_time'] = None
        recording_state['session_id'] = None
        recording_state['frame_size'] = None
        recording_state['frame_count'] = 0
        recording_state['last_frame'] = None

        app_state['is_recording'] = False
        app_state['recording_session_id'] = None

# ---------------------------------------------------------------------------
# API / Helpers
# ---------------------------------------------------------------------------
def _count_recordings_and_size():
    count, total_bytes = 0, 0
    if os.path.exists(RECORDINGS_DIR):
        for root, _, files in os.walk(RECORDINGS_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                if os.path.isfile(filepath):
                    count += 1
                    total_bytes += os.path.getsize(filepath)
    return count, round(total_bytes / (1024 * 1024), 2)

@app.route('/api/health', methods=['GET'])
def api_health():
    with state_lock:
        uptime = int(time.time() - server_start_time)
        count, size_mb = _count_recordings_and_size()
        health_data = {
            'status': 'healthy',
            'uptime_seconds': uptime,
            'is_streaming': app_state['is_streaming'],
            'is_recording': app_state['is_recording'],
            'mobile_connected': app_state['mobile_connected'],
            'dashboard_connected': app_state['dashboard_connected'],
            'recordings_count': count,
            'disk_used_mb': size_mb
        }
    return jsonify(health_data)

# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route('/')
def dashboard():
    local_ips = get_local_ips()
    connections = []
    for ip in local_ips:
        mobile_url = f"https://{ip}:{PORT}/mobile"
        qr_code_base64 = generate_qr_code(mobile_url)
        connections.append({
            'ip': ip,
            'url': mobile_url,
            'qr_code': qr_code_base64
        })
    with state_lock:
        pin = app_state['pairing_pin']
    return render_template('dashboard.html', connections=connections, port=PORT, pairing_pin=pin)

@app.route('/mobile')
def mobile():
    return render_template('mobile.html')

@app.route('/api/recordings', methods=['GET'])
def api_list_recordings():
    recordings = []
    if not os.path.exists(RECORDINGS_DIR):
        return jsonify(recordings)
    for date_folder in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
        date_path = os.path.join(RECORDINGS_DIR, date_folder)
        if not os.path.isdir(date_path): continue
        for filename in sorted(os.listdir(date_path), reverse=True):
            filepath = os.path.join(date_path, filename)
            if not os.path.isfile(filepath): continue
            stat = os.stat(filepath)
            duration = 0
            try:
                cap = cv2.VideoCapture(filepath)
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                    if fps > 0: duration = frame_count / fps
                cap.release()
            except: pass
            recordings.append({
                'date': date_folder, 'filename': filename,
                'size': stat.st_size, 'duration': round(duration, 1),
                'created': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return jsonify(recordings)

@app.route('/api/recordings/<date>/<filename>', methods=['GET'])
def api_get_recording(date, filename):
    safe_date, safe_filename = os.path.basename(date), os.path.basename(filename)
    filepath = os.path.join(RECORDINGS_DIR, safe_date, safe_filename)
    if not os.path.isfile(filepath): abort(404)
    return send_file(filepath, mimetype='video/mp4')

@app.route('/api/recordings/<date>/<filename>', methods=['DELETE'])
def api_delete_recording(date, filename):
    safe_date, safe_filename = os.path.basename(date), os.path.basename(filename)
    filepath = os.path.join(RECORDINGS_DIR, safe_date, safe_filename)
    if not os.path.isfile(filepath): abort(404)
    try:
        os.remove(filepath)
        date_dir = os.path.join(RECORDINGS_DIR, safe_date)
        if os.path.isdir(date_dir) and not os.listdir(date_dir): os.rmdir(date_dir)
        return jsonify({'status': 'deleted', 'filename': safe_filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _get_snapshots_list():
    snapshots = []
    if not os.path.exists(SNAPSHOTS_DIR): return snapshots
    for date_folder in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
        date_path = os.path.join(SNAPSHOTS_DIR, date_folder)
        if not os.path.isdir(date_path): continue
        for filename in sorted(os.listdir(date_path), reverse=True):
            filepath = os.path.join(date_path, filename)
            if not os.path.isfile(filepath): continue
            stat = os.stat(filepath)
            snapshots.append({
                'date': date_folder, 'filename': filename,
                'size': stat.st_size,
                'created': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return snapshots

@app.route('/api/snapshots', methods=['GET'])
def api_list_snapshots():
    return jsonify(_get_snapshots_list())

@app.route('/api/snapshots/<date>/<filename>', methods=['GET'])
def api_get_snapshot(date, filename):
    safe_date, safe_filename = os.path.basename(date), os.path.basename(filename)
    filepath = os.path.join(SNAPSHOTS_DIR, safe_date, safe_filename)
    if not os.path.isfile(filepath): abort(404)
    return send_file(filepath, mimetype='image/jpeg')

@app.route('/api/snapshots/<date>/<filename>', methods=['DELETE'])
def api_delete_snapshot(date, filename):
    safe_date, safe_filename = os.path.basename(date), os.path.basename(filename)
    filepath = os.path.join(SNAPSHOTS_DIR, safe_date, safe_filename)
    if not os.path.isfile(filepath): abort(404)
    try:
        os.remove(filepath)
        date_dir = os.path.join(SNAPSHOTS_DIR, safe_date)
        if os.path.isdir(date_dir) and not os.listdir(date_dir): os.rmdir(date_dir)
        return jsonify({'status': 'deleted', 'filename': safe_filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------------------------------------------------------------------------
# WebSocket Handlers
# ---------------------------------------------------------------------------
@socketio.on('join')
def on_join(data):
    client_type = data.get('client_type')
    sid = request.sid
    with state_lock:
        session_clients[sid] = client_type
        if client_type == 'dashboard':
            join_room('dashboard_room')
            app_state['dashboard_connected'] = True
            print(f"[SocketIO] Dashboard connected: sid={sid}")
            emit('status_update', app_state, broadcast=True)
        elif client_type == 'mobile':
            print(f"[SocketIO] Mobile client connected (awaiting PIN auth): sid={sid}")
            # Emitting an event to ensure the client shows the pin prompt
            emit('require_pin', {'message': 'Please authenticate'})

@socketio.on('pin_auth')
def handle_pin_auth(data):
    sid = request.sid
    pin = data.get('pin')
    with state_lock:
        if pin == app_state['pairing_pin']:
            authenticated_mobiles.add(sid)
            join_room('mobile_room')
            app_state['mobile_connected'] = True
            print(f"[Security] SID {sid} successfully authenticated with PIN")
            emit('pin_auth_status', {'success': True})
            emit('status_update', app_state, broadcast=True)
        else:
            print(f"[Security] SID {sid} failed PIN authentication: {pin}")
            emit('pin_auth_status', {'success': False, 'message': 'Incorrect PIN'})

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    with state_lock:
        client_type = session_clients.pop(sid, None)
        last_frame_times.pop(sid, None)
        is_auth = sid in authenticated_mobiles
        authenticated_mobiles.discard(sid)

        if client_type == 'dashboard':
            app_state['dashboard_connected'] = False
        elif client_type == 'mobile':
            if is_auth:
                app_state['mobile_connected'] = False
                app_state['is_streaming'] = False
                app_state['cameras'] = []
                app_state['active_camera'] = None
                if app_state['is_recording']:
                    _stop_recording()
                    emit('recording_status', {
                        'is_recording': False, 'session_id': None, 'path': None
                    }, to='dashboard_room', broadcast=True)

        print(f"[SocketIO] Client disconnected: {client_type} (sid={sid})")
        emit('status_update', app_state, broadcast=True)

@socketio.on('frame')
def handle_frame(data):
    sid = request.sid
    with state_lock:
        if sid not in authenticated_mobiles:
            return

        current_time = time.time()
        min_interval = 1.0 / max(app_state['fps'], 1)
        last_time = last_frame_times.get(sid, 0)
        if current_time - last_time < min_interval: return
        last_frame_times[sid] = current_time

        image_data = data.get('image')
        if not image_data: return

        nparr = np.frombuffer(image_data, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None: return

        motion_detected, area = _detect_motion(frame)
        if motion_detected:
            thumbnail = _create_thumbnail(frame)
            app_state['last_motion_thumbnail'] = thumbnail
            emit('motion_detected', {
                'timestamp': time.time(), 'area': area, 'thumbnail': thumbnail
            }, to='dashboard_room')

            if app_state['auto_record_on_motion'] and not app_state['is_recording']:
                _start_recording()
                emit('recording_status', {
                    'is_recording': True, 'session_id': recording_state['session_id'],
                    'path': recording_state['path']
                }, broadcast=True)
                emit('status_update', app_state, broadcast=True)

        if app_state['is_recording']:
            _write_recording_frame(frame)

        emit('video_frame', data, to='dashboard_room')

@socketio.on('stream_state_change')
def handle_stream_state_change(data):
    sid = request.sid
    with state_lock:
        if sid not in authenticated_mobiles: return
        app_state['is_streaming'] = data.get('is_streaming', False)
        print(f"[SocketIO] Streaming status updated: {app_state['is_streaming']}")
        if not app_state['is_streaming'] and app_state['is_recording']:
            _stop_recording()
            emit('recording_status', {
                'is_recording': False, 'session_id': None, 'path': None
            }, to='dashboard_room', broadcast=True)
        emit('status_update', app_state, broadcast=True)

@socketio.on('toggle_stream_request')
def handle_toggle_stream_request(data):
    emit('toggle_stream', data, to='mobile_room', broadcast=True)

@socketio.on('switch_camera_request')
def handle_switch_camera_request(data=None):
    emit('switch_camera', to='mobile_room', broadcast=True)

@socketio.on('select_camera_request')
def handle_select_camera_request(data):
    emit('select_camera', data, to='mobile_room', broadcast=True)

@socketio.on('cameras_updated')
def handle_cameras_updated(data):
    sid = request.sid
    with state_lock:
        if sid not in authenticated_mobiles: return
        app_state['cameras'] = data.get('cameras', [])
        app_state['active_camera'] = data.get('active_camera', None)
        print(f"[SocketIO] Cameras updated: {len(app_state['cameras'])} devices")
        emit('status_update', app_state, broadcast=True)

@socketio.on('set_quality')
def handle_set_quality(data):
    quality = data.get('quality', '480p')
    if quality not in QUALITY_PRESETS: return
    with state_lock:
        app_state['quality'] = quality
        preset = QUALITY_PRESETS[quality]
        print(f"[SocketIO] Quality changed to {quality}")
        emit('set_quality', {'quality': quality, 'width': preset['width'], 'height': preset['height']}, to='mobile_room', broadcast=True)
        emit('status_update', app_state, broadcast=True)

@socketio.on('set_fps')
def handle_set_fps(data):
    fps = data.get('fps', 15)
    if fps not in [10, 15, 30, 60]: return
    with state_lock:
        app_state['fps'] = fps
        print(f"[SocketIO] FPS changed to {fps}")
        emit('set_fps', {'fps': fps}, to='mobile_room', broadcast=True)
        emit('status_update', app_state, broadcast=True)

@socketio.on('set_auto_record')
def handle_set_auto_record(data):
    with state_lock:
        app_state['auto_record_on_motion'] = data.get('enabled', False)
        print(f"[SocketIO] Auto-record on motion changed to {app_state['auto_record_on_motion']}")
        emit('status_update', app_state, broadcast=True)

@socketio.on('start_recording')
def handle_start_recording(data=None):
    with state_lock:
        if app_state['is_recording']: return
        _start_recording()
        emit('recording_status', {'is_recording': True, 'session_id': recording_state['session_id'], 'path': recording_state['path']}, broadcast=True)
        emit('status_update', app_state, broadcast=True)

@socketio.on('stop_recording')
def handle_stop_recording(data=None):
    with state_lock:
        if not app_state['is_recording']: return
        _stop_recording()
        emit('recording_status', {'is_recording': False, 'session_id': None, 'path': None}, broadcast=True)
        emit('status_update', app_state, broadcast=True)

@socketio.on('save_snapshot')
def handle_save_snapshot(data):
    if not data or 'image' not in data: return
    try:
        image_bytes = data['image']
        snapshot_id = str(uuid.uuid4())[:8]
        date_str = datetime.now().strftime('%Y-%m-%d')
        folder = os.path.join(SNAPSHOTS_DIR, date_str)
        os.makedirs(folder, exist_ok=True)
        filename = f"snapshot-{snapshot_id}.jpg"
        filepath = os.path.join(folder, filename)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)
        print(f"[Snapshot] Saved snapshot {snapshot_id} -> {filepath}")
        emit('snapshot_saved', {'status': 'success', 'filename': filename, 'date': date_str}, to='dashboard_room', broadcast=True)
    except Exception as e:
        print(f"[Snapshot] Save error: {e}")

# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    cert, key = generate_self_signed_cert()
    local_ips = get_local_ips()
    primary_ip = local_ips[0] if local_ips else '127.0.0.1'
    print("\n" + "=" * 60)
    print(f" Camera Stream Server is starting!")
    print(f" PC Dashboard: https://localhost:{PORT}")
    print(f" Wireless LAN URL: https://{primary_ip}:{PORT}/mobile")
    if len(local_ips) > 1:
        for ip in local_ips[1:]: print(f"   - https://{ip}:{PORT}/mobile")
    print(f" Recordings directory: {RECORDINGS_DIR}")
    print("=" * 60 + "\n")
    if cert and key:
        socketio.run(app, host='0.0.0.0', port=PORT, certfile=cert, keyfile=key)
    else:
        print("[WARNING] Starting in HTTP mode.")
        socketio.run(app, host='0.0.0.0', port=PORT)
"""

with open("app.py", "w") as f:
    f.write(content)
