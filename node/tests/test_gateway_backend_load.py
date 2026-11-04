"""The gateway reports a NInfer backend's load so clients can ask the resident model only while idle."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("gateway_backend_load", ROOT / "tare_node/qualified_model_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_backend_load_reads_ninfer_requests_and_is_none_otherwise():
    class Load(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"object": "ninfer.load", "requests": {
                "admitted": 1, "running": 1, "waiting": 0, "prefilling": 0}}).encode()
            self.send_response(200 if self.path == "/v1/load" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Load)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        runtime = SimpleNamespace(
            model_id="qwen38-gsq", process=process,
            config={"models": {"qwen38-gsq": {"runtime": {"kind": "ninfer"}},
                               "qwen38": {"runtime": {"kind": "llama"}}}},
            backend_url=lambda path: f"http://127.0.0.1:{server.server_port}{path}")
        load = gateway.FleetRuntime.backend_load
        assert load(runtime) == {"admitted": 1, "running": 1, "waiting": 0}
        assert load(SimpleNamespace(**{**vars(runtime), "model_id": "qwen38"})) is None  # not NInfer
        assert load(SimpleNamespace(**{**vars(runtime), "model_id": None})) is None  # nothing resident
    finally:
        process.kill()
        server.shutdown()
