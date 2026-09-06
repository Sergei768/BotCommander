import os
import sys
import subprocess
import time
import threading
import psutil
import platform
import socket
import logging
import json
import secrets
import re
import signal
import stat
from functools import wraps
from flask import Flask, render_template_string, redirect, url_for, jsonify, request, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('BOTCOMMANDER_HTTPS', '0') == '1',
)

BOTS_DIR = './bot'
LOGS_DIR = './logs'
STATUS = {}
PROCESSES = {}
DISABLED = set()
ERROR_HISTORY = {}
STATE_FILE = './bot_state.json'
PASSWORD_FILE = './.botcommander_auth'
SECRET_FILE = './.botcommander_secret'

STOP_EVENTS = {}
WORKERS = {}
_PROCESS_CACHE = {}
_state_lock = threading.Lock()

LOGIN_ATTEMPTS = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300

_VALID_BOT_NAME = re.compile(r'^[a-zA-Z0-9_-]+$')
psutil.cpu_percent(interval=None)


def validate_botname(name):
    return bool(name and _VALID_BOT_NAME.match(name))


def get_client_ip():
    return request.remote_addr or 'unknown'


def _cleanup_login_attempts():
    now = time.time()
    cutoff = LOGIN_LOCKOUT_SECONDS * 2
    for ip in list(LOGIN_ATTEMPTS.keys()):
        locked = LOGIN_ATTEMPTS[ip].get('locked_until')
        if locked and now > locked + cutoff:
            LOGIN_ATTEMPTS.pop(ip, None)


def is_login_locked(ip):
    _cleanup_login_attempts()
    entry = LOGIN_ATTEMPTS.get(ip)
    if entry and entry.get('locked_until') and time.time() < entry['locked_until']:
        return True
    return False


def login_lockout_remaining(ip):
    entry = LOGIN_ATTEMPTS.get(ip)
    if not entry or not entry.get('locked_until'):
        return 0
    return max(0, int(entry['locked_until'] - time.time()))


def register_failed_login(ip):
    _cleanup_login_attempts()
    entry = LOGIN_ATTEMPTS.setdefault(ip, {'count': 0, 'locked_until': None})
    entry['count'] += 1
    if entry['count'] >= MAX_LOGIN_ATTEMPTS:
        entry['locked_until'] = time.time() + LOGIN_LOCKOUT_SECONDS
        entry['count'] = 0


def register_successful_login(ip):
    LOGIN_ATTEMPTS.pop(ip, None)


def safe_write(path, data):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def get_or_create_secret_key():
    if os.path.exists(SECRET_FILE):
        try:
            with open(SECRET_FILE, 'r') as f:
                key = f.read().strip()
            if len(key) >= 16:
                return key
        except Exception:
            pass
    key = secrets.token_hex(32)
    safe_write(SECRET_FILE, key)
    return key


app.secret_key = get_or_create_secret_key()


def is_password_set():
    return os.path.exists(PASSWORD_FILE)


def set_password(password):
    hashed = generate_password_hash(password)
    safe_write(PASSWORD_FILE, hashed)


def verify_password(password):
    if not os.path.exists(PASSWORD_FILE):
        return False
    try:
        with open(PASSWORD_FILE, 'r') as f:
            stored = f.read().strip()
        return check_password_hash(stored, password)
    except Exception:
        return False


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_password_set():
            return redirect(url_for('setup'))
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def get_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['csrf_token'] = token
    return token


def require_csrf(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        submitted = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
        expected = session.get('csrf_token')
        if not expected or not submitted or not secrets.compare_digest(submitted, expected):
            return jsonify({'error': 'invalid or missing CSRF token'}), 403
        return f(*args, **kwargs)
    return decorated


def load_state():
    global DISABLED
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                with _state_lock:
                    DISABLED = set(data.get('disabled', []))
        except Exception:
            pass


def save_state():
    try:
        with _state_lock:
            data = {'disabled': list(DISABLED)}
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def add_error(botname, message):
    if botname not in ERROR_HISTORY:
        ERROR_HISTORY[botname] = []
    ERROR_HISTORY[botname].append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}")
    if len(ERROR_HISTORY[botname]) > 5:
        ERROR_HISTORY[botname] = ERROR_HISTORY[botname][-5:]


def _sleep_chunked(stop_event, seconds=5, chunk=0.5):
    for _ in range(int(seconds / chunk)):
        if stop_event.is_set():
            break
        time.sleep(chunk)


def _wait_worker_dead(name, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = WORKERS.get(name)
        if not t or not t.is_alive():
            return True
        time.sleep(0.1)
    return False


def bot_worker(name, path):
    stop_event = STOP_EVENTS.setdefault(name, threading.Event())
    stop_event.clear()
    proc = None
    try:
        while not stop_event.is_set():
            with _state_lock:
                is_disabled = name in DISABLED

            if is_disabled:
                STATUS[name] = 'OFFLINE'
                _sleep_chunked(stop_event, seconds=5)
                continue

            try:
                abs_path = os.path.abspath(path)
                venv_python = os.path.join(abs_path, 'venv', 'bin', 'python3')
                main_py = os.path.join(abs_path, 'main.py')

                if not os.path.exists(venv_python):
                    add_error(name, f"Python not found: {venv_python}")
                    STATUS[name] = 'ERROR'
                    _sleep_chunked(stop_event, seconds=5)
                    continue

                if not os.path.exists(main_py):
                    add_error(name, f"main.py not found")
                    STATUS[name] = 'ERROR'
                    _sleep_chunked(stop_event, seconds=5)
                    continue

                os.makedirs(LOGS_DIR, exist_ok=True)
                log_path = os.path.join(LOGS_DIR, f"{name}.log")

                with open(log_path, 'a') as log_file:
                    kwargs = {
                        'stdout': log_file,
                        'stderr': subprocess.STDOUT,
                        'cwd': abs_path,
                        'env': os.environ.copy()
                    }
                    if platform.system() != 'Windows':
                        kwargs['preexec_fn'] = os.setsid

                    proc = subprocess.Popen([venv_python, main_py], **kwargs)
                    PROCESSES[name] = proc
                    STATUS[name] = 'ON'

                    while proc.poll() is None:
                        if stop_event.is_set():
                            break
                        time.sleep(0.5)

                    if stop_event.is_set():
                        try:
                            if platform.system() != 'Windows':
                                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                            else:
                                proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            try:
                                if platform.system() != 'Windows':
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                else:
                                    proc.kill()
                            except Exception:
                                pass
                        STATUS[name] = 'OFFLINE'
                        return

                if proc.returncode != 0:
                    add_error(name, f"Exit code {proc.returncode}")
                    STATUS[name] = 'DOWN'
                else:
                    STATUS[name] = 'DOWN'

            except Exception as e:
                add_error(name, str(e))
                STATUS[name] = 'ERROR'

            _sleep_chunked(stop_event, seconds=5)
    finally:
        with _state_lock:
            if WORKERS.get(name) is threading.current_thread():
                WORKERS.pop(name, None)
            if PROCESSES.get(name) is proc:
                PROCESSES.pop(name, None)
            _PROCESS_CACHE.pop(name, None)
        if stop_event.is_set():
            STATUS[name] = 'OFFLINE'


def start_worker(name, path):
    with _state_lock:
        if name in WORKERS and WORKERS[name].is_alive():
            return
    t = threading.Thread(target=bot_worker, args=(name, path), daemon=True)
    with _state_lock:
        WORKERS[name] = t
    t.start()


def start_all_bots():
    load_state()
    os.makedirs(BOTS_DIR, exist_ok=True)
    valid_bots = set()
    for name in os.listdir(BOTS_DIR):
        path = os.path.join(BOTS_DIR, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, 'main.py')):
            valid_bots.add(name)
            if name in DISABLED:
                STATUS[name] = 'OFFLINE'
            else:
                STATUS[name] = 'ON'
                start_worker(name, path)
    for name in list(STATUS.keys()):
        if name not in valid_bots:
            STATUS.pop(name, None)
            ERROR_HISTORY.pop(name, None)
            PROCESSES.pop(name, None)
            _PROCESS_CACHE.pop(name, None)
            STOP_EVENTS.pop(name, None)
            WORKERS.pop(name, None)


def get_bot_cpu_usage():
    usage = {}
    for name, proc in list(PROCESSES.items()):
        try:
            if proc.poll() is not None:
                if name in _PROCESS_CACHE:
                    del _PROCESS_CACHE[name]
                usage[name] = {'cpu': 0.0, 'mem': 0.0, 'uptime': 0}
                continue

            p = _PROCESS_CACHE.get(name)
            if not p or p.pid != proc.pid:
                p = psutil.Process(proc.pid)
                _PROCESS_CACHE[name] = p

            cpu_percent = p.cpu_percent(interval=None)
            mem_info = p.memory_info()
            mem_percent = p.memory_percent(memtype='rss')
            create_time = p.create_time()
            uptime_sec = time.time() - create_time

            usage[name] = {
                'cpu': cpu_percent,
                'mem': mem_percent,
                'uptime': uptime_sec,
            }
        except Exception:
            if name in _PROCESS_CACHE:
                del _PROCESS_CACHE[name]
            usage[name] = {'cpu': 0.0, 'mem': 0.0, 'uptime': 0}
    return usage


def format_uptime(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d > 0:
        parts.append(f"{d}d")
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return ' '.join(parts)


def get_system_info():
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    hostname = socket.gethostname()
    os_info = platform.system() + " " + platform.release()
    uptime_seconds = time.time() - psutil.boot_time()
    uptime_str = format_uptime(uptime_seconds)
    python_version = platform.python_version()
    cpu_arch = platform.machine()
    return {
        'cpu': cpu,
        'ram': ram.percent,
        'ram_used_gb': ram.used / 1024**3,
        'ram_total_gb': ram.total / 1024**3,
        'hostname': hostname,
        'os_info': os_info,
        'uptime': uptime_str,
        'python_version': python_version,
        'cpu_arch': cpu_arch,
    }


AUTH_STYLES = '''
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background-color: #1e1e1e;
    color: #d4d4d4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .auth-box {
    background: #252526;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 40px;
    width: 320px;
    text-align: center;
  }
  .auth-icon {
    font-size: 32px;
    margin-bottom: 12px;
    display: inline-block;
    animation: logoPulse 3s ease-in-out infinite;
    transition: transform 0.3s ease, color 0.3s ease;
  }
  .auth-icon:hover {
    animation-play-state: paused;
    transform: rotate(90deg) scale(1.15);
    color: #4a9eff;
  }
  @keyframes logoPulse {
    0%, 100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.12);
      opacity: 0.75;
    }
  }
  .auth-box h1 {
    font-size: 18px;
    font-weight: 300;
    color: #ffffff;
    margin-bottom: 8px;
    letter-spacing: 1px;
  }
  .auth-box p {
    font-size: 12px;
    color: #808080;
    margin-bottom: 24px;
  }
  .form-group {
    margin-bottom: 16px;
    text-align: left;
  }
  .form-group label {
    display: block;
    font-size: 11px;
    color: #808080;
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .form-group input {
    width: 100%;
    padding: 10px 12px;
    background: #1e1e1e;
    border: 1px solid #404040;
    border-radius: 3px;
    color: #d4d4d4;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s;
  }
  .form-group input:focus {
    border-color: #4a9eff;
  }
  .btn-submit {
    width: 100%;
    padding: 10px;
    background: #1e3a5f;
    border: 1px solid #4a9eff;
    border-radius: 3px;
    color: #4a9eff;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    margin-top: 8px;
  }
  .btn-submit:hover {
    background: #4a9eff;
    color: #fff;
  }
  .flash {
    background: #3f1e1e;
    border: 1px solid #f48771;
    color: #f48771;
    padding: 10px 12px;
    border-radius: 3px;
    font-size: 12px;
    margin-bottom: 16px;
  }
</style>
'''


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_password_set():
        return redirect(url_for('login'))
    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm', '').strip()
        if not password:
            flash('Password is required')
        elif password != confirm:
            flash('Passwords do not match')
        elif len(password) < 4:
            flash('Password must be at least 4 characters')
        else:
            set_password(password)
            session['authenticated'] = True
            return redirect(url_for('index'))
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Setup — BotCommander</title>''' + AUTH_STYLES + '''</head>
<body>
  <div class="auth-box">
    <div class="auth-icon">⌘</div>
    <h1>BotCommander</h1>
    <p>Create your admin password</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="post">
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" placeholder="Enter password" autofocus>
      </div>
      <div class="form-group">
        <label>Confirm</label>
        <input type="password" name="confirm" placeholder="Repeat password">
      </div>
      <button type="submit" class="btn-submit">Set Password</button>
    </form>
  </div>
</body>
</html>
''')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_password_set():
        return redirect(url_for('setup'))
    ip = get_client_ip()
    if request.method == 'POST':
        if is_login_locked(ip):
            remaining = login_lockout_remaining(ip)
            flash(f'Too many failed attempts. Try again in {remaining}s.')
        else:
            password = request.form.get('password', '').strip()
            if verify_password(password):
                register_successful_login(ip)
                session['authenticated'] = True
                return redirect(url_for('index'))
            else:
                register_failed_login(ip)
                flash('Invalid password')
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Login — BotCommander</title>''' + AUTH_STYLES + '''</head>
<body>
  <div class="auth-box">
    <div class="auth-icon">⌘</div>
    <h1>BotCommander</h1>
    <p>Enter your password to continue</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    <form method="post">
      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" placeholder="••••••••" autofocus>
      </div>
      <button type="submit" class="btn-submit">Sign In</button>
    </form>
  </div>
</body>
</html>
''')


@app.route('/logout', methods=['POST'])
@require_csrf
def logout():
    session.pop('authenticated', None)
    session.pop('csrf_token', None)
    return redirect(url_for('login'))


@app.route('/')
@require_auth
def index():
    csrf_token = get_csrf_token()
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="csrf-token" content="{{ csrf_token }}">
<title>BotCommander</title>
<style>
  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }
  body {
    background-color: #1e1e1e;
    color: #d4d4d4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    margin: 0;
    padding: 40px;
    min-height: 100vh;
    position: relative;
  }
  .header {
    text-align: center;
    margin-bottom: 30px;
  }
  .header-icon {
    font-size: 32px;
    margin-bottom: 10px;
    display: inline-block;
    position: relative;
    cursor: pointer;
    text-decoration: none;
    color: inherit;
    animation: logoPulse 3s ease-in-out infinite;
    transition: transform 0.3s ease, color 0.3s ease;
  }
  .header-icon:hover {
    animation-play-state: paused;
    transform: rotate(90deg) scale(1.15);
    color: #f48771;
  }
  .header-icon::after {
    content: 'Logout';
    position: absolute;
    top: calc(100% - 4px);
    left: 50%;
    transform: translateX(-50%) scale(0.9);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.5px;
    color: #f48771;
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease, transform 0.2s ease;
  }
  .header-icon:hover::after {
    opacity: 1;
    transform: translateX(-50%) scale(1);
  }
  @keyframes logoPulse {
    0%, 100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.12);
      opacity: 0.75;
    }
  }
  .header h1 {
    font-size: 24px;
    font-weight: 300;
    margin: 0;
    color: #ffffff;
    letter-spacing: 2px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
  }
  .top-bar {
    position: absolute;
    top: 40px;
    right: 40px;
  }
  .top-bar a {
    font-size: 11px;
    color: #808080;
    text-decoration: none;
    transition: color 0.2s;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .top-bar a:hover {
    color: #f48771;
  }
  .system-info {
    margin-bottom: 20px;
    font-size: 12px;
    color: #808080;
    font-family: monospace;
    background: #252526;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 16px 20px;
    line-height: 1.6;
    text-align: center;
  }
  .bots-container {
    background: #252526;
    border: 1px solid #333;
    border-radius: 4px;
    overflow: hidden;
  }
  .bots-header {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    background: #2d2d30;
    border-bottom: 1px solid #333;
    gap: 16px;
  }
  .col-header {
    font-size: 11px;
    color: #808080;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 500;
    cursor: pointer;
    user-select: none;
    transition: color 0.2s;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .col-header:hover {
    color: #d4d4d4;
  }
  .col-header.sort-active {
    color: #4a9eff;
  }
  .sort-arrow {
    font-size: 10px;
    opacity: 0.5;
  }
  .col-header.sort-active .sort-arrow {
    opacity: 1;
  }
  .h-name { width: 140px; }
  .h-status { width: 100px; }
  .h-cpu { width: 80px; text-align: center; }
  .h-mem { width: 80px; text-align: center; }
  .h-uptime { width: 80px; text-align: center; }
  .h-actions { margin-left: auto; }
  .bot-row {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid #2a2a2a;
    gap: 16px;
  }
  .bot-row:last-child {
    border-bottom: none;
  }
  .bot-row:hover {
    background: #2a2d2e;
  }
  .bot-row.offline {
    opacity: 0.6;
  }
  .col-name {
    width: 140px;
    font-weight: 500;
    color: #ffffff;
    flex-shrink: 0;
  }
  .col-status {
    width: 100px;
    flex-shrink: 0;
  }
  .col-metric {
    width: 80px;
    text-align: center;
    font-family: monospace;
    flex-shrink: 0;
  }
  .col-actions {
    margin-left: auto;
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
  .btn {
    display: inline-block;
    padding: 6px 14px;
    background: #1e1e1e;
    border: 1px solid #404040;
    border-radius: 3px;
    color: #d4d4d4;
    text-decoration: none;
    font-size: 11px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    transition: all 0.15s;
    min-width: 60px;
    text-align: center;
  }
  .btn:hover {
    background: #2a2d2e;
    border-color: #4a9eff;
    color: #4a9eff;
  }
  .btn-primary {
    background: #1e3a5f;
    border-color: #4a9eff;
    color: #4a9eff;
  }
  .btn-primary:hover {
    background: #4a9eff;
    color: #fff;
  }
  .btn-danger {
    background: #3f1e1e;
    border-color: #f48771;
    color: #f48771;
  }
  .btn-danger:hover {
    background: #f48771;
    color: #fff;
  }
  .status-ON {
    color: #4ec9b0;
    font-weight: 500;
  }
  .status-DOWN, .status-ERROR {
    color: #f48771;
    font-weight: 500;
  }
  .status-OFFLINE {
    color: #6e6e6e;
  }
  .error-badge {
    display: inline-block;
    width: 6px;
    height: 6px;
    background: #f48771;
    border-radius: 50%;
    margin-left: 6px;
    vertical-align: middle;
  }
</style>
<script>
let sortColumn = 'cpu';
let sortDirection = 'desc';

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').getAttribute('content');
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}

function botAction(action, name) {
  fetch(`/${action}/${encodeURIComponent(name)}`, {
    method: 'POST',
    headers: { 'X-CSRFToken': csrfToken() },
  })
    .then(response => {
      if (!response.ok) throw new Error('action failed');
      return fetchStatus();
    })
    .catch(() => fetchStatus());
}

function doLogout() {
  fetch('/logout', {
    method: 'POST',
    headers: { 'X-CSRFToken': csrfToken() },
  }).then(() => { window.location.href = '/login'; });
}

function fetchStatus() {
  fetch('/status').then(response => response.json()).then(data => {
    document.getElementById('system-info').textContent = data.system_info;
    renderBots(data.bots);
  });
}

function renderBots(botsData) {
  const container = document.getElementById('bots-rows');
  container.innerHTML = '';

  let botsArray = Object.entries(botsData).map(([name, info]) => ({
    name, ...info,
    cpuNum: parseFloat(info.cpu) || 0,
    memNum: parseFloat(info.mem) || 0
  }));

  botsArray.sort((a, b) => {
    let valA, valB;
    if (sortColumn === 'name') { valA = a.name.toLowerCase(); valB = b.name.toLowerCase(); }
    else if (sortColumn === 'status') { valA = a.status; valB = b.status; }
    else if (sortColumn === 'cpu') { valA = a.cpuNum; valB = b.cpuNum; }
    else if (sortColumn === 'mem') { valA = a.memNum; valB = b.memNum; }
    else if (sortColumn === 'uptime') { valA = a.uptime; valB = b.uptime; }

    if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
    if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
    return 0;
  });

  for (const bot of botsArray) {
    const row = document.createElement('div');
    row.className = 'bot-row';
    if (bot.status === 'OFFLINE') row.classList.add('offline');

    const statusClass = 'status-' + bot.status.split(' ')[0];
    const hasErrors = bot.errors && bot.errors.length > 0;
    const safeName = escapeHtml(bot.name);
    const safeErrors = hasErrors ? escapeHtml(bot.errors.join('\\n')) : '';
    const nameArg = JSON.stringify(bot.name).replace(/"/g, '&quot;');

    let actionsHtml = '';
    if (bot.status === 'OFFLINE') {
      actionsHtml = `<button type="button" class="btn btn-primary" onclick="botAction('enable', ${nameArg})">Start</button>`;
    } else {
      actionsHtml = `
        <button type="button" class="btn" onclick="botAction('restart', ${nameArg})">Restart</button>
        <button type="button" class="btn btn-danger" onclick="botAction('stop', ${nameArg})">Stop</button>
        <button type="button" class="btn" onclick="botAction('disable', ${nameArg})">Disable</button>
      `;
    }

    row.innerHTML = `
      <div class="col-name">${safeName}${hasErrors ? '<span class="error-badge" title="' + safeErrors + '"></span>' : ''}</div>
      <div class="col-status ${statusClass}">${bot.status}</div>
      <div class="col-metric">${bot.cpu.toFixed(1)}%</div>
      <div class="col-metric">${bot.mem.toFixed(1)}%</div>
      <div class="col-metric">${bot.uptime || '-'}</div>
      <div class="col-actions">${actionsHtml}</div>
    `;
    container.appendChild(row);
  }

  updateHeaderArrows();
}

function setSort(column) {
  if (sortColumn === column) {
    sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    sortColumn = column;
    sortDirection = 'desc';
  }
  fetchStatus();
}

function updateHeaderArrows() {
  document.querySelectorAll('.col-header').forEach(el => {
    el.classList.remove('sort-active');
    const arrow = el.querySelector('.sort-arrow');
    if (arrow) arrow.textContent = '⇅';
  });

  const activeHeader = document.querySelector(`.col-header[data-sort="${sortColumn}"]`);
  if (activeHeader) {
    activeHeader.classList.add('sort-active');
    const arrow = activeHeader.querySelector('.sort-arrow');
    if (arrow) arrow.textContent = sortDirection === 'asc' ? '▲' : '▼';
  }
}

setInterval(fetchStatus, 1000);
window.onload = fetchStatus;
</script>
</head>
<body>
  <div class="header">
    <span onclick="doLogout()" class="header-icon" title="Logout">⌘</span>
    <h1>BotCommander</h1>
  </div>
  <div id="system-info" class="system-info">Loading...</div>
  <div class="bots-container">
    <div class="bots-header">
      <div class="col-header h-name" data-sort="name" onclick="setSort('name')">Name <span class="sort-arrow">⇅</span></div>
      <div class="col-header h-status" data-sort="status" onclick="setSort('status')">Status <span class="sort-arrow">⇅</span></div>
      <div class="col-header h-cpu sort-active" data-sort="cpu" onclick="setSort('cpu')">CPU <span class="sort-arrow">▼</span></div>
      <div class="col-header h-mem" data-sort="mem" onclick="setSort('mem')">RAM <span class="sort-arrow">⇅</span></div>
      <div class="col-header h-uptime" data-sort="uptime" onclick="setSort('uptime')">Uptime <span class="sort-arrow">⇅</span></div>
      <div class="col-header h-actions">Actions</div>
    </div>
    <div id="bots-rows"></div>
  </div>
</body>
</html>
''', csrf_token=csrf_token)


@app.route('/status')
@require_auth
def status():
    system = get_system_info()
    bot_stats = get_bot_cpu_usage()
    bots_data = {}
    for name, st in list(STATUS.items()):
        errors = ERROR_HISTORY.get(name, [])
        usage = bot_stats.get(name, {'cpu': 0.0, 'mem': 0.0, 'uptime': 0})
        bots_data[name] = {
            'status': st,
            'cpu': usage['cpu'],
            'mem': usage['mem'],
            'uptime': format_uptime(usage['uptime']) if usage['uptime'] > 0 else '-',
            'errors': errors,
        }
    system_info_text = (
        f"{system['hostname']} | {system['os_info']} | "
        f"CPU: {system['cpu']:.1f}% | RAM: {system['ram']:.1f}% ({system['ram_used_gb']:.1f}/{system['ram_total_gb']:.1f} GB) | "
        f"Uptime: {system['uptime']} | Python: {system['python_version']} | {system['cpu_arch']}"
    )
    return jsonify({
        'system_info': system_info_text,
        'bots': bots_data,
    })


@app.route('/restart/<botname>', methods=['POST'])
@require_auth
@require_csrf
def restart_bot(botname):
    if not validate_botname(botname):
        return jsonify({'error': 'Invalid bot name'}), 400

    ev = STOP_EVENTS.get(botname)
    if ev:
        ev.set()
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        try:
            if platform.system() != 'Windows':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass

    if not _wait_worker_dead(botname, timeout=3.0):
        t = WORKERS.get(botname)
        if t and t.is_alive():
            proc = PROCESSES.get(botname)
            if proc and proc.poll() is None:
                try:
                    if platform.system() != 'Windows':
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
            t.join(timeout=2)

    with _state_lock:
        WORKERS.pop(botname, None)
        PROCESSES.pop(botname, None)
        _PROCESS_CACHE.pop(botname, None)

    if botname in STOP_EVENTS:
        STOP_EVENTS[botname].clear()
    else:
        STOP_EVENTS[botname] = threading.Event()

    STATUS[botname] = 'ON'
    path = os.path.join(BOTS_DIR, botname)
    start_worker(botname, path)
    return jsonify({'ok': True})


@app.route('/stop/<botname>', methods=['POST'])
@require_auth
@require_csrf
def stop_bot(botname):
    if not validate_botname(botname):
        return jsonify({'error': 'Invalid bot name'}), 400

    ev = STOP_EVENTS.get(botname)
    if ev:
        ev.set()
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        try:
            if platform.system() != 'Windows':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass

    _wait_worker_dead(botname, timeout=3.0)
    STATUS[botname] = 'OFFLINE'
    return jsonify({'ok': True})


@app.route('/disable/<botname>', methods=['POST'])
@require_auth
@require_csrf
def disable_bot(botname):
    if not validate_botname(botname):
        return jsonify({'error': 'Invalid bot name'}), 400

    ev = STOP_EVENTS.get(botname)
    if ev:
        ev.set()
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        try:
            if platform.system() != 'Windows':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass

    _wait_worker_dead(botname, timeout=3.0)

    with _state_lock:
        changed = botname not in DISABLED
        DISABLED.add(botname)
    if changed:
        save_state()
    STATUS[botname] = 'OFFLINE'
    return jsonify({'ok': True})


@app.route('/enable/<botname>', methods=['POST'])
@require_auth
@require_csrf
def enable_bot(botname):
    if not validate_botname(botname):
        return jsonify({'error': 'Invalid bot name'}), 400

    with _state_lock:
        changed = botname in DISABLED
        if changed:
            DISABLED.remove(botname)
    if changed:
        save_state()

    ev = STOP_EVENTS.get(botname)
    if ev:
        ev.set()
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        try:
            if platform.system() != 'Windows':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass

    if not _wait_worker_dead(botname, timeout=3.0):
        t = WORKERS.get(botname)
        if t and t.is_alive():
            proc = PROCESSES.get(botname)
            if proc and proc.poll() is None:
                try:
                    if platform.system() != 'Windows':
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
            t.join(timeout=2)

    with _state_lock:
        WORKERS.pop(botname, None)
        PROCESSES.pop(botname, None)
        _PROCESS_CACHE.pop(botname, None)

    if botname in STOP_EVENTS:
        STOP_EVENTS[botname].clear()
    else:
        STOP_EVENTS[botname] = threading.Event()

    STATUS[botname] = 'ON'
    path = os.path.join(BOTS_DIR, botname)
    start_worker(botname, path)
    return jsonify({'ok': True})


def graceful_shutdown(signum, frame):
    print(f"\n[shutdown] Signal {signum} received, stopping all bots...")
    for name in list(PROCESSES.keys()):
        ev = STOP_EVENTS.get(name)
        if ev:
            ev.set()
        proc = PROCESSES.get(name)
        if proc and proc.poll() is None:
            try:
                if platform.system() != 'Windows':
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                pass
    for name, t in list(WORKERS.items()):
        if t.is_alive():
            t.join(timeout=2)
    sys.exit(0)


try:
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
except Exception:
    pass


if __name__ == '__main__':
    start_all_bots()
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = '127.0.0.1'
    print(f" ⌘ BotCommander http://{local_ip}:9999")
    try:
        app.run(host='0.0.0.0', port=9999)
    except OSError as e:
        if e.errno == 98 or "Address already in use" in str(e):
            print(f"[err] Port 9999 in use")
        else:
            print(f"[err] {e}")
        sys.exit(1)
