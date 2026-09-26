"""/metrics stays whole when the API runs several uvicorn workers (WEB_CONCURRENCY)."""
import os
import socket
import subprocess
import sys
import time
import urllib.request


def _counted(base: str) -> float:
    body = urllib.request.urlopen(f"{base}/metrics").read().decode()
    return sum(float(line.rsplit(" ", 1)[1]) for line in body.splitlines()
               if line.startswith("http_requests_total{") and 'route="/health"' in line)


def test_every_worker_answers_for_all_of_them(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = os.environ | {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "WEB_CONCURRENCY": "3",
                        "PG_MAX_CONNECTIONS": "4"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rec.api.app:app", "--port", str(port),
         "--log-level", "warning"], env=env)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/health")
                break
            except OSError:
                time.sleep(0.5)
        before = _counted(base)
        for _ in range(30):
            urllib.request.urlopen(f"{base}/health")
        # each scrape lands on whichever worker accepts it; all must see every request
        assert {_counted(base) - before for _ in range(6)} == {30.0}
    finally:
        server.terminate()
        server.wait(timeout=30)
