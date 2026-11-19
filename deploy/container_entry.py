"""Container entrypoint: run the proxy and the panel in one process tree.

No supervisord dependency. Spawns both children with the same environment
(tokens/ports/server are injected as env vars by config.py) and tears the
whole container down if either child exits, so an orchestrator restarts it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _spawn(extra_args: list[str]) -> subprocess.Popen:
    # subprocess inherits os.environ — that is how FXPROXY_* reaches config.py.
    cmd = [sys.executable, "-m", "fxproxy", *extra_args]
    return subprocess.Popen(cmd)


def main() -> int:
    country = os.environ.get("FXPROXY_COUNTRY", "").strip()
    run_args = ["run", "--host", os.environ.get("FXPROXY_HOST", "0.0.0.0")]
    if country:
        run_args += ["--country", country]

    web_host = os.environ.get("FXPROXY_WEB_HOST", "0.0.0.0")
    web_port = os.environ.get("FXPROXY_WEB_PORT", "8765")
    web_args = ["web", "--no-open", "--web-host", web_host,
                "--web-port", web_port]

    children = [_spawn(run_args), _spawn(web_args)]
    print(f"[container] proxy + panel started (pids {[c.pid for c in children]})",
          flush=True)

    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        for c in children:
            if c.poll() is None:
                c.terminate()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    try:
        while not stopping:
            for c in children:
                rc = c.poll()
                if rc is not None and not stopping:
                    print(f"[container] child pid={c.pid} exited rc={rc}; "
                          "shutting down", flush=True)
                    stopping = True
                    for other in children:
                        if other.poll() is None:
                            other.terminate()
                    break
            if stopping:
                break
            time.sleep(0.5)

        deadline = time.time() + 8
        for c in children:
            if c.poll() is None:
                try:
                    c.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    c.kill()
        return 0
    finally:
        for c in children:
            if c.poll() is None:
                c.kill()


if __name__ == "__main__":
    raise SystemExit(main())
