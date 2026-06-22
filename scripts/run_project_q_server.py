from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LOG_DIR = ROOT / ".project_q"
LOG_DIR.mkdir(parents=True, exist_ok=True)
log = (LOG_DIR / "server.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log

from project_q.server import run_server  # noqa: E402


server = run_server()
try:
    server.serve_forever()
finally:
    server.RequestHandlerClass.app.clear_runtime_metadata()
    server.server_close()
    log.close()
