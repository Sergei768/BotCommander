import os
import sys
import subprocess
import time
import threading
import psutil
import platform
import socket
from flask import Flask, render_template_string, redirect, url_for, jsonify

app = Flask(__name__)

BOTS_DIR = './bot'
STATUS = {}
PROCESSES = {}
DISABLED = set()
ERROR_HISTORY = {}

def add_error(botname, message):
    if botname not in ERROR_HISTORY:
        ERROR_HISTORY[botname] = []
    ERROR_HISTORY[botname].append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}")
    if len(ERROR_HISTORY[botname]) > 5:
        ERROR_HISTORY[botname] = ERROR_HISTORY[botname][-5:]

def bot_worker(name, path):
    while True:
        if name in DISABLED:
            STATUS[name] = 'OFFLINE'
            time.sleep(5)
            continue

        try:
            STATUS[name] = 'STARTING'
            PROCESSES[name] = subprocess.Popen(
                ['bash', '-c', f'source {path}/venv/bin/activate && python3 {path}/main.py'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            STATUS[name] = 'ON'
            stdout, stderr = PROCESSES[name].communicate()
            if PROCESSES[name].returncode != 0:
                err_msg = stderr.decode(errors='ignore').strip() or f"Exit code {PROCESSES[name].returncode}"
                add_error(name, err_msg)
                STATUS[name] = f'DOWN'
        except Exception as e:
            add_error(name, str(e))
            STATUS[name] = f'ERROR'
        time.sleep(5)

def start_all_bots():
    for name in os.listdir(BOTS_DIR):
        path = os.path.join(BOTS_DIR, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, 'main.py')):
            if name not in DISABLED:
                STATUS[name] = 'STARTING'
                t = threading.Thread(target=bot_worker, args=(name, path), daemon=True)
                t.start()
            else:
                STATUS[name] = 'OFFLINE'

def get_bot_cpu_usage():
    usage = {}
    for name, proc in PROCESSES.items():
        if proc.poll() is None:
            try:
                p = psutil.Process(proc.pid)
                cpu_percent = p.cpu_percent(interval=0.1)
                mem_percent = p.memory_percent()
                create_time = p.create_time()
                uptime_sec = time.time() - create_time
                usage[name] = {
                    'cpu': cpu_percent,
                    'mem': mem_percent,
                    'uptime': uptime_sec,
                }
            except Exception:
                usage[name] = {'cpu': 0.0, 'mem': 0.0, 'uptime': 0}
        else:
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
    parts.append(f"{s}с")
    return ' '.join(parts)

def get_system_info():
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    hostname = socket.gethostname()
    os_info = platform.system() + " " + platform.release()
    uptime_seconds = time.time() - psutil.boot_time()
    uptime_str = format_uptime(uptime_seconds)
    python_version = platform.python_version()
    cpu_arch = platform.machine()
    return {
        'cpu': cpu,
        'ram': ram,
        'hostname': hostname,
        'os_info': os_info,
        'uptime': uptime_str,
        'python_version': python_version,
        'cpu_arch': cpu_arch,
    }

@app.route('/')
def index():
    bots = STATUS
    return render_template_string('''
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>BotCommander</title>
<style>
  body {
    background-color: #1a1a1a;
    color: #e0e0e0;
    font-family: "Courier New", monospace;
    font-size: 13px;
    margin: 0;
    padding: 20px;
    line-height: 1.4;
  }
  h1 {
    text-align: center;
    font-size: 18px;
    margin-bottom: 20px;
    color: #888;
    font-weight: normal;
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .system-info {
    text-align: center;
    margin-bottom: 30px;
    font-size: 11px;
    color: #666;
    font-family: monospace;
    white-space: pre-wrap;
    border-bottom: 1px solid #333;
    padding-bottom: 15px;
  }
  table {
    width: 95%;
    margin: 0 auto;
    border-collapse: collapse;
    border: none;
    font-size: 12px;
  }
  th, td {
    border: none;
    border-bottom: 1px solid #333;
    padding: 8px 12px;
    text-align: left;
  }
  th {
    background-color: transparent;
    color: #666;
    font-weight: normal;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 1px;
  }
  td {
    color: #aaa;
  }
  a {
    color: #888;
    text-decoration: none;
    border-bottom: 1px dotted #555;
    margin: 0 4px;
    font-size: 11px;
  }
  a:hover {
    color: #ccc;
    border-bottom-color: #ccc;
  }
  .status-ON {
    color: #7a7a7a;
  }
  .status-DOWN, .status-ERROR {
    color: #666;
  }
  .status-STARTING {
    color: #999;
  }
  .status-OFFLINE {
    color: #444;
  }
  tr:hover {
    background-color: #222;
  }
  tr.offline {
    opacity: 0.6;
  }
  tr.starting {
    background-color: #252525;
  }
</style>
<script>
function fetchStatus() {
  fetch('/status').then(response => response.json()).then(data => {
    document.getElementById('system-info').textContent = data.system_info;
    const tbody = document.getElementById('bots-tbody');
    tbody.innerHTML = '';
    for (const [name, info] of Object.entries(data.bots)) {
      const row = document.createElement('tr');
      if(info.status === 'OFFLINE') row.className = 'offline';
      else if(info.status.startsWith('STARTING')) row.className = 'starting';

      row.innerHTML = `
        <td>${name}</td>
        <td title="${info.errors.length ? info.errors.join('\\n') : 'No errors'}" class="status-${info.status.split(' ')[0]}">${info.status}</td>
        <td>${info.cpu.toFixed(1)}</td>
        <td>${info.mem.toFixed(1)}</td>
        <td>${info.uptime || '-'}</td>
        <td>
          ${info.status === 'OFFLINE'
            ? `<a href="/enable/${name}">Enable</a>`
            : `<a href="/restart/${name}">Restart</a> |
               <a href="/stop/${name}">Stop</a> |
               <a href="/disable/${name}">Turn off</a>`}
        </td>
      `;
      tbody.appendChild(row);
    }
  });
}
setInterval(fetchStatus, 1000);
window.onload = fetchStatus;
</script>
</head>
<body>
  <h1>BotCommander</h1>
  <div id="system-info" class="system-info">Loading...</div>
  <table>
    <thead>
      <tr>
        <th>Bot name</th>
        <th>Status</th>
        <th>CPU (%)</th>
        <th>RAM (%)</th>
        <th>Uptime</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="bots-tbody">
    </tbody>
  </table>
</body>
</html>
''', bots=bots)

@app.route('/status')
def status():
    system = get_system_info()
    bot_stats = get_bot_cpu_usage()
    bots_data = {}
    for name, status in STATUS.items():
        errors = ERROR_HISTORY.get(name, [])
        usage = bot_stats.get(name, {'cpu': 0.0, 'mem': 0.0, 'uptime': 0})
        bots_data[name] = {
            'status': status,
            'cpu': usage['cpu'],
            'mem': usage['mem'],
            'uptime': format_uptime(usage['uptime']) if usage['uptime'] > 0 else '-',
            'errors': errors,
        }
    system_info_text = (
        f"{system['hostname']} | {system['os_info']}\n"
        f"CPU: {system['cpu']:.1f}% | RAM: {system['ram']:.1f}% | Uptime: {system['uptime']}\n"
        f"Python: {system['python_version']} | Arch: {system['cpu_arch']}"
    )
    return jsonify({
        'system_info': system_info_text,
        'bots': bots_data,
    })

@app.route('/restart/<botname>')
def restart_bot(botname):
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        proc.terminate()
    STATUS[botname] = 'STARTING'
    return redirect(url_for('index'))

@app.route('/stop/<botname>')
def stop_bot(botname):
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        proc.terminate()
    STATUS[botname] = 'OFFLINE'
    return redirect(url_for('index'))

@app.route('/disable/<botname>')
def disable_bot(botname):
    proc = PROCESSES.get(botname)
    if proc and proc.poll() is None:
        proc.terminate()
    DISABLED.add(botname)
    STATUS[botname] = 'OFFLINE'
    return redirect(url_for('index'))

@app.route('/enable/<botname>')
def enable_bot(botname):
    if botname in DISABLED:
        DISABLED.remove(botname)
        STATUS[botname] = 'STARTING'
        path = os.path.join(BOTS_DIR, botname)
        t = threading.Thread(target=bot_worker, args=(botname, path), daemon=True)
        t.start()
    return redirect(url_for('index'))

if __name__ == '__main__':
    start_all_bots()
    app.run(host='0.0.0.0', port=9999)
