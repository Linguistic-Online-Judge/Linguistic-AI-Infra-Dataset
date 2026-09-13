"""Run student and admin browsers in a fresh SQLite child app, never daily user state."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if port == 8080:
        raise RuntimeError("The daily development port is not a browser test target")
    directory = root / "runtime/browser-tests" / ("admin-acceptance-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    with (directory / "server.stdout.log").open("w") as stdout, (
        directory / "server.stderr.log"
    ).open("w") as stderr:
        server = subprocess.Popen([
            sys.executable, "-m", "linguistic_oj.local_dev", "--root", str(root),
            "--port", str(port), "--state-dir", str(directory / "state"),
        ], cwd=root, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 30
            url = f"http://127.0.0.1:{port}"
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"Isolated server exited; inspect {directory}")
                try:
                    with urlopen(url + "/health/ready", timeout=1) as response:
                        if json.load(response) == {"status": "ready"}:
                            break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated server readiness timed out")
                time.sleep(0.1)
            for runner in ("run.mjs", "admin-run.mjs"):
                result = subprocess.run(
                    ["node", f"tests/browser/{runner}", "--url", url],
                    cwd=root, timeout=300, check=False,
                )
                if result.returncode:
                    return result.returncode
            return 0
        finally:
            if os.name == "nt" and server.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                    capture_output=True, timeout=10, check=False,
                )
            elif server.poll() is None:
                server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
