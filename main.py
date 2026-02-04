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
    background-color: #1e1e1e;
    color: #d4d4d4;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    margin: 0;
    padding: 40px;
  }
  .header {
    text-align: center;
    margin-bottom: 30px;
  }
  .header h1 {
    font-size: 24px;
    font-weight: 300;
    margin: 0;
    color: #ffffff;
    letter-spacing: 2px;
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
  .bot-row {
    background: #252526;
    border: 1px solid #333;
    border-radius: 4px;
    margin-bottom: 12px;
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 20px;
    transition: border-color 0.2s;
  }
  .bot-row:hover {
    border-color: #404040;
  }
  .bot-row.offline {
    opacity: 0.6;
  }
  .bot-row.starting {
    border-color: #dcdcaa;
  }
  .bot-name {
    width: 120px;
    font-weight: 500;
    color: #ffffff;
  }
  .bot-status {
    width: 100px;
    font-size: 12px;
    font-weight: 500;
  }
  .bot-metric {
    width: 80px;
    text-align: center;
    font-family: monospace;
  }
  .bot-metric-label {
    font-size: 10px;
    color: #666;
    text-transform: uppercase;
    margin-bottom: 4px;
  }
  .bot-actions {
    margin-left: auto;
    display: flex;
    gap: 16px;
  }
  a {
    color: #4a9eff;
    text-decoration: none;
    font-size: 12px;
  }
  a:hover {
    text-decoration: underline;
  }
  .status-ON {
    color: #4ec9b0;
  }
  .status-DOWN, .status-ERROR {
    color: #f48771;
  }
  .status-STARTING {
    color: #dcdcaa;
  }
  .status-OFFLINE {
    color: #6e6e6e;
  }
</style>
<script>
function fetchStatus() {
  fetch('/status').then(response => response.json()).then(data => {
    document.getElementById('system-info').textContent = data.system_info;
    const container = document.getElementById('bots-container');
    container.innerHTML = '';
    for (const [name, info] of Object.entries(data.bots)) {
      const row = document.createElement('div');
      row.className = 'bot-row';
      if (info.status === 'OFFLINE') row.classList.add('offline');
      else if (info.status.startsWith('STARTING')) row.classList.add('starting');
      
      const statusClass = 'status-' + info.status.split(' ')[0];
      
      let actionsHtml = '';
      if (info.status === 'OFFLINE') {
        actionsHtml = `<a href="/enable/${name}">Enable</a>`;
      } else {
        actionsHtml = `
          <a href="/restart/${name}">Restart</a>
          <a href="/stop/${name}">Stop</a>
          <a href="/disable/${name}">Disable</a>
        `;
      }
      
      row.innerHTML = `
        <div class="bot-name">${name}</div>
        <div class="bot-status ${statusClass}">${info.status}</div>
        <div class="bot-metric">
          <div class="bot-metric-label">CPU</div>
          <div>${info.cpu.toFixed(1)}%</div>
        </div>
        <div class="bot-metric">
          <div class="bot-metric-label">RAM</div>
          <div>${info.mem.toFixed(1)}%</div>
        </div>
        <div class="bot-metric">
          <div class="bot-metric-label">Uptime</div>
          <div>${info.uptime || '-'}</div>
        </div>
        <div class="bot-actions">${actionsHtml}</div>
      `;
      container.appendChild(row);
    }
  });
}
setInterval(fetchStatus, 1000);
window.onload = fetchStatus;
</script>
</head>
<body>
  <div class="header">
    <h1>BotCommander</h1>
  </div>
  <div id="system-info" class="system-info">Loading...</div>
  <div id="bots-container"></div>
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
        f"{system['hostname']} | {system['os_info']} | "
        f"CPU: {system['cpu']:.1f}% | RAM: {system['ram']:.1f}% | Uptime: {system['uptime']} | "
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
