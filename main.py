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
from flask import Flask, render_template_string, redirect, url_for, jsonify

app = Flask(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

BOTS_DIR = './bot'
STATUS = {}
PROCESSES = {}
DISABLED = set()
ERROR_HISTORY = {}
STATE_FILE = './bot_state.json'

def load_state():
    global DISABLED
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                DISABLED = set(data.get('disabled', []))
        except Exception:
            pass

def save_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'disabled': list(DISABLED)}, f)
    except Exception:
        pass

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
    load_state()
    for name in os.listdir(BOTS_DIR):
        path = os.path.join(BOTS_DIR, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, 'main.py')):
            if name in DISABLED:
                STATUS[name] = 'OFFLINE'
            else:
                STATUS[name] = 'STARTING'
                t = threading.Thread(target=bot_worker, args=(name, path), daemon=True)
                t.start()

def get_bot_cpu_usage():
    usage = {}
    for name, proc in PROCESSES.items():
        try:
            if proc.poll() is None:
                p = psutil.Process(proc.pid)
                cpu_percent = p.cpu_percent(interval=None)
                mem_info = p.memory_info()
                mem_percent = p.memory_percent(memtype='rss')
                create_time = p.create_time()
                uptime_sec = time.time() - create_time
                
                if cpu_percent == 0.0:
                    time.sleep(0.1)
                    cpu_percent = p.cpu_percent(interval=None)
                
                usage[name] = {
                    'cpu': cpu_percent,
                    'mem': mem_percent,
                    'uptime': uptime_sec,
                }
            else:
                usage[name] = {'cpu': 0.0, 'mem': 0.0, 'uptime': 0}
        except Exception:
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
    cpu = psutil.cpu_percent(interval=0.5)
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

@app.route('/')
def index():
    bots = STATUS
    return render_template_string('''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
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
  }
  .header {
    text-align: center;
    margin-bottom: 30px;
  }
  .header-icon {
    font-size: 32px;
    margin-bottom: 10px;
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
  .bot-row.starting {
    background: rgba(220, 220, 170, 0.05);
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
  .status-STARTING {
    color: #dcdcaa;
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
    else if (bot.status.startsWith('STARTING')) row.classList.add('starting');
    
    const statusClass = 'status-' + bot.status.split(' ')[0];
    const hasErrors = bot.errors && bot.errors.length > 0;
    
    let actionsHtml = '';
    if (bot.status === 'OFFLINE') {
      actionsHtml = `<a href="/enable/${bot.name}" class="btn btn-primary">Start</a>`;
    } else {
      actionsHtml = `
        <a href="/restart/${bot.name}" class="btn">Restart</a>
        <a href="/stop/${bot.name}" class="btn btn-danger">Stop</a>
        <a href="/disable/${bot.name}" class="btn">Disable</a>
      `;
    }
    
    row.innerHTML = `
      <div class="col-name">${bot.name}${hasErrors ? '<span class="error-badge" title="' + bot.errors.join('\\n') + '"></span>' : ''}</div>
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
    <div class="header-icon">⌘</div>
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
        f"CPU: {system['cpu']:.1f}% | RAM: {system['ram']:.1f}% ({system['ram_used_gb']:.1f}/{system['ram_total_gb']:.1f} GB) | "
        f"Uptime: {system['uptime']} | Python: {system['python_version']} | {system['cpu_arch']}"
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
    save_state()
    return redirect(url_for('index'))

@app.route('/enable/<botname>')
def enable_bot(botname):
    if botname in DISABLED:
        DISABLED.remove(botname)
        save_state()
        STATUS[botname] = 'STARTING'
        path = os.path.join(BOTS_DIR, botname)
        t = threading.Thread(target=bot_worker, args=(botname, path), daemon=True)
        t.start()
    return redirect(url_for('index'))

if __name__ == '__main__':
    start_all_bots()
    app.run(host='0.0.0.0', port=9999)
