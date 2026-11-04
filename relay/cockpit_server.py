#!/usr/bin/env python3
"""
relay/cockpit_server.py — tare.tools Owner Cockpit Web UI V0.1
============================================================
Lightweight, zero-dependency, self-contained local web server providing:
1. Live DAG Work Graph Visualization & Node Inspector
2. Real-Time Hardware Telemetry Monitor (CPU, RAM, GPU)
3. Direct Browser Intake Submission Box (POST /api/intake)
4. Multi-Train Status & Swarm Coordination Board
"""
from __future__ import annotations

import cgi
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
INTAKE_HUMAN_DIR = RELAY_DIR / "intake" / "human"
WORK_GRAPH_FILE = RELAY_ROOT / "work-graph.json"

if str(RELAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RELAY_ROOT))
if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import relay_mesh, resource_telemetry, intake_processor

PORT = int(os.environ.get("RELAY_COCKPIT_PORT", "8765"))
HOST = os.environ.get("RELAY_COCKPIT_HOST", "0.0.0.0")


def get_tailscale_ip() -> Optional[str]:
    """Detects Tailscale IPv4 if Tailscale is installed and active."""
    ts_exe = shutil.which("tailscale")
    if not ts_exe:
        for p in [r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"]:
            if os.path.exists(p):
                ts_exe = p
                break
    if ts_exe:
        try:
            proc = subprocess.run([ts_exe, "ip", "-4"], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            pass
    return None


def get_lan_ip() -> str:
    """Returns the local network IP for mobile/LAN access."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


HTML_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>tare.tools — Owner Cockpit V0.1</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --card-border: #30363d;
      --text: #c9d1d9;
      --text-bright: #f0f6fc;
      --accent: #58a6ff;
      --green: #238636;
      --green-bright: #3fb950;
      --purple: #8957e5;
      --yellow: #d29922;
      --red: #f85149;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 24px; }
    .container { max-width: 1300px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }
    
    header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--card-border); padding-bottom: 16px; }
    h1 { color: var(--text-bright); font-size: 24px; display: flex; align-items: center; gap: 10px; }
    .badge { background: #21262d; border: 1px solid var(--card-border); padding: 4px 10px; border-radius: 20px; font-size: 12px; color: var(--accent); }
    
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
    
    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
    .card h2 { font-size: 16px; color: var(--text-bright); border-bottom: 1px solid #21262d; padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
    
    /* Telemetry Pill */
    .node-pill { background: #21262d; border: 1px solid var(--card-border); border-radius: 6px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
    .node-header { display: flex; justify-content: space-between; font-weight: bold; color: var(--text-bright); }
    .metrics-row { display: flex; gap: 12px; font-size: 13px; flex-wrap: wrap; }
    .metric { background: #161b22; padding: 4px 8px; border-radius: 4px; border: 1px solid #30363d; }
    .metric.busy { border-color: var(--yellow); color: var(--yellow); }
    .metric.ok { border-color: var(--green-bright); color: var(--green-bright); }
    
    /* Table */
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d; }
    th { color: #8b949e; font-weight: 600; }
    tr:hover { background: #21262d; }
    .status-tag { padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
    .tag-green { background: rgba(46,160,67,0.15); color: #3fb950; border: 1px solid rgba(46,160,67,0.4); }
    .tag-yellow { background: rgba(210,153,34,0.15); color: #d29922; border: 1px solid rgba(210,153,34,0.4); }
    .tag-purple { background: rgba(137,87,229,0.15); color: #a371f7; border: 1px solid rgba(137,87,229,0.4); }
    .tag-red { background: rgba(248,81,73,0.15); color: #f85149; border: 1px solid rgba(248,81,73,0.4); }

    /* Intake Form */
    .form-group { display: flex; flex-direction: column; gap: 6px; }
    label { font-size: 12px; font-weight: 600; color: #8b949e; }
    input, textarea { background: #0d1117; border: 1px solid var(--card-border); color: var(--text-bright); padding: 10px; border-radius: 6px; font-size: 13px; }
    input:focus, textarea:focus { outline: none; border-color: var(--accent); }
    button { background: var(--green); color: white; border: none; padding: 10px 16px; border-radius: 6px; font-weight: bold; cursor: pointer; transition: 0.2s; }
    button:hover { background: #2ea043; }
    
    .toast { position: fixed; bottom: 20px; right: 20px; background: #238636; color: white; padding: 12px 20px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); display: none; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🚂 tare.tools <span style="font-weight: normal; font-size: 18px; color: #8b949e;">— Owner Cockpit V0.1</span></h1>
      <div style="display: flex; gap: 10px; align-items: center;">
        <span class="badge" id="lastSync">Sincronizando...</span>
        <button onclick="refreshData()" style="padding: 6px 12px; font-size: 12px; background: #21262d; border: 1px solid var(--card-border);">🔄 Atualizar</button>
      </div>
    </header>

    <!-- Row 1: Hardware Telemetry -->
    <div class="card">
      <h2>🖥️ Nós do Swarm & Telemetria de Hardware <span style="font-size: 12px; color: #8b949e;" id="nodeCount">0 nós</span></h2>
      <div class="grid-3" id="nodesGrid">Carregando telemetria...</div>
    </div>

    <!-- Row 2: Active Trains & Intake Box -->
    <div class="grid-2">
      <!-- Active Trains Board -->
      <div class="card">
        <h2>🚆 Trens Ativos & Coordenação <span style="font-size: 12px; color: #8b949e;" id="trainCount">0 ativos</span></h2>
        <div style="overflow-x: auto;">
          <table>
            <thead>
              <tr>
                <th>Trem</th>
                <th>Status</th>
                <th>Papel</th>
                <th>Rev</th>
                <th>Tarefas</th>
              </tr>
            </thead>
            <tbody id="trainsBody">
              <tr><td colspan="5">Carregando trens...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Web Intake Form -->
      <div class="card">
        <h2>📥 Nova Demanda / Ideia (Intake Box)</h2>
        <form id="intakeForm" onsubmit="submitIntake(event)">
          <div class="form-group">
            <label for="title">Título da Demanda:</label>
            <input type="text" id="title" placeholder="Ex: Otimizar velocidade do visualizer ou adicionar recurso X" required>
          </div>
          <div class="form-group">
            <label for="body">Contexto / Detalhes:</label>
            <textarea id="body" rows="4" placeholder="Descreva o problema ou o que os agentes devem fazer..."></textarea>
          </div>
          <div style="display: flex; justify-content: flex-end;">
            <button type="submit">🚀 Enviar para o Grafo</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Row 3: Landed Trains -->
    <div class="card">
      <h2>✅ Trens Recentemente Aterrissados (Landed)</h2>
      <div id="landedList" style="display: flex; flex-direction: column; gap: 8px; font-size: 13px;">Carregando histórico...</div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    async function refreshData() {
      try {
        const res = await fetch('/api/state');
        const data = await res.json();
        renderBoard(data);
      } catch (err) {
        console.error("Erro ao carregar dados:", err);
      }
    }

    function renderBoard(data) {
      document.getElementById('lastSync').textContent = 'Última sync: ' + (data.timestamp ? data.timestamp.substring(11, 19) : 'agora');
      
      // Render Nodes
      const nodesGrid = document.getElementById('nodesGrid');
      nodesGrid.innerHTML = '';
      document.getElementById('nodeCount').textContent = (data.nodes ? data.nodes.length : 0) + ' nós ativos';
      if (data.nodes && data.nodes.length > 0) {
        data.nodes.forEach(n => {
          const res = n.resources || {};
          const isGpuBusy = res.gpu_load_pct >= 80;
          const gpuText = res.gpu_available ? `${res.gpu_name} (${res.gpu_load_pct}% carga, ${Math.round(res.gpu_vram_free_mb/1024)}GB VRAM livre)` : 'Sem GPU dedicada';
          
          const div = document.createElement('div');
          div.className = 'node-pill';
          div.innerHTML = `
            <div class="node-header">
              <span>💻 ${n.hostname}</span>
              <span class="status-tag ${n.pressure.includes('BUSY') ? 'tag-yellow' : 'tag-green'}">${n.pressure}</span>
            </div>
            <div class="metrics-row">
              <div class="metric">⚡ CPU: <b>${res.cpu_load_pct || 0}%</b> (${res.cpu_cores} cores)</div>
              <div class="metric">🧠 RAM: <b>${res.ram_available_gb || 0} GB</b> livres (${res.ram_load_pct || 0}%)</div>
              <div class="metric">💾 Disco: <b>${res.disk_free_gb || 0} GB</b> livres</div>
            </div>
            <div class="metric ${isGpuBusy ? 'busy' : 'ok'}" style="font-size: 11px;">
              🎮 GPU: ${gpuText}
            </div>
          `;
          nodesGrid.appendChild(div);
        });
      } else {
        nodesGrid.innerHTML = '<div style="color: #8b949e;">No active node detected.</div>';
      }

      // Render Active Trains
      const tbody = document.getElementById('trainsBody');
      tbody.innerHTML = '';
      document.getElementById('trainCount').textContent = (data.trains ? data.trains.length : 0) + ' ativos';
      if (data.trains && data.trains.length > 0) {
        data.trains.forEach(t => {
          let tagClass = 'tag-yellow';
          if (t.status === 'LANDED') tagClass = 'tag-green';
          if (t.status.includes('APPROVED')) tagClass = 'tag-green';
          if (t.status.includes('AUDIT')) tagClass = 'tag-purple';
          if (t.status.includes('REJECTED') || t.status.includes('HOLD')) tagClass = 'tag-red';

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><b>${t.train_id}</b></td>
            <td><span class="status-tag ${tagClass}">${t.status}</span></td>
            <td><code>${t.current_role}</code></td>
            <td>r${t.revision}</td>
            <td>${(t.task_ids || []).join(', ')}</td>
          `;
          tbody.appendChild(tr);
        });
      } else {
        tbody.innerHTML = '<tr><td colspan="5" style="color: #8b949e;">Nenhum trem ativo no momento (Swarm ocioso).</td></tr>';
      }

      // Render Landed
      const landedList = document.getElementById('landedList');
      landedList.innerHTML = '';
      if (data.recentLanded && data.recentLanded.length > 0) {
        data.recentLanded.forEach(l => {
          const item = document.createElement('div');
          item.innerHTML = `🏁 <b>${l.train_id}</b> — tarefas: <code>${(l.task_ids || []).join(', ')}</code>`;
          landedList.appendChild(item);
        });
      } else {
        landedList.innerHTML = '<span style="color: #8b949e;">Nenhum trem no histórico recente.</span>';
      }
    }

    async function submitIntake(ev) {
      ev.preventDefault();
      const title = document.getElementById('title').value;
      const body = document.getElementById('body').value;
      
      try {
        const res = await fetch('/api/intake', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, body })
        });
        const data = await res.json();
        if (data.ok) {
          showToast("✨ Demanda adicionada ao backlog e admitida no grafo!");
          document.getElementById('intakeForm').reset();
          refreshData();
        } else {
          showToast("❌ Erro ao enviar: " + data.reason);
        }
      } catch (err) {
        showToast("❌ Erro na requisição: " + err);
      }
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 4000);
    }

    refreshData();
    setInterval(refreshData, 3000); // Polling suave a cada 3s
  </script>
</body>
</html>
"""


class CockpitHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif url.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            
            # Read current board and telemetry
            board = relay_io.read_json(RELAY_DIR / "BOARD.json") or {}
            self.wfile.write(json.dumps(board, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/intake":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw)
                title = str(payload.get("title", "")).strip()
                body = str(payload.get("body", "")).strip()

                if not title:
                    self._send_json({"ok": False, "reason": "Title is required"}, 400)
                    return

                # Write intake item directly to relay/intake/human/
                INTAKE_HUMAN_DIR.mkdir(parents=True, exist_ok=True)
                item_slug = intake_processor._slug(title, 32)
                item_filename = f"INTAKE_WEB_{int(time.time())}_{item_slug}.md"
                item_path = INTAKE_HUMAN_DIR / item_filename
                
                content = f"# {title}\n\n**Author:** Operator (via Cockpit Web UI)\n**Timestamp:** {relay_mesh.now_iso()}\n\n{body}\n"
                item_path.write_text(content, encoding="utf-8")

                # Sweep into work graph
                intake_processor.sweep_human()
                relay_mesh.update_board()

                self._send_json({"ok": True, "filename": item_filename, "title": title})
            except Exception as exc:
                self._send_json({"ok": False, "reason": str(exc)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data: Dict[str, Any], code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # Suppress default noisy stdio logging


def start_cockpit_server(host: str = HOST, port: int = PORT) -> socketserver.TCPServer:
    """Starts the Cockpit web server in a daemon thread."""
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((host, port), CockpitHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    lan_ip = get_lan_ip()
    ts_ip = get_tailscale_ip()
    print(f"[COCKPIT SERVER] Running at:")
    print(f"  • Localhost (este PC):    http://localhost:{port}/")
    print(f"  • Wi-Fi / Celular / LAN:   http://{lan_ip}:{port}/")
    if ts_ip:
        print(f"  • Tailscale Mesh Privado: http://{ts_ip}:{port}/ (Acesse do 4G/5G em qualquer lugar)")
    return server


if __name__ == "__main__":
    relay_io.utf8_console()
    lan_ip = get_lan_ip()
    ts_ip = get_tailscale_ip()
    print(f"================================================================================")
    print(f"  🚂 tare.tools — Owner Cockpit V0.1 (Zero-Trust Private Mesh)")
    print(f"================================================================================")
    print(f"  • Localhost (este PC):    http://localhost:{PORT}/")
    print(f"  • Wi-Fi / Celular / LAN:   http://{lan_ip}:{PORT}/ (Mesmo Wi-Fi)")
    if ts_ip:
        print(f"  • Tailscale Mesh Privado: http://{ts_ip}:{PORT}/ (De qualquer lugar no 4G/5G)")
    else:
        print(f"  • Tailscale:              Instale o Tailscale para acesso 100% privado fora de casa.")
    print(f"================================================================================")
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer((HOST, PORT), CockpitHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[COCKPIT SERVER] Stopping server.")
