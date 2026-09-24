"""Run browser acceptance against a fresh, isolated local app; leave user data alone."""

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
    directory = root / "runtime/browser-tests" / ("acceptance-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    state = directory / "state"
    with (directory / "server.stdout.log").open("w") as stdout, (
        directory / "server.stderr.log"
    ).open("w") as stderr:
        server = subprocess.Popen([
            sys.executable, "-m", "linguistic_oj.local_dev", "--root", str(root),
            "--port", str(port), "--state-dir", str(state),
        ], cwd=root, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 30
            url = f"http://127.0.0.1:{port}"
            while True:
                if server.poll() is not None:
                    raise RuntimeError("isolated server exited during startup")
                try:
                    with urlopen(url + "/health/ready", timeout=1) as response:
                        ready = json.load(response)
                    if ready == {"status": "ready"}:
                        break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("isolated server readiness timed out")
                time.sleep(0.1)
            return subprocess.run(
                ["node", "tests/browser/run.mjs", "--url", url], cwd=root,
                timeout=240, check=False,
            ).returncode
        finally:
            if os.name == "nt" and server.poll() is None:
                # The Windows venv launcher can own a child Python process.
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
