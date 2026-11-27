# SPDX-License-Identifier: MIT
"""Direct JSON requests and wall-clock-bounded requests for model probes."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A provider credential must never follow a redirect to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url, headers, body, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    with opener.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read(4 * 1024 * 1024 + 1))


def request_json(url, headers, body=None, timeout=12, *, deadline=None):
    if deadline is None or deadline == float("inf"):
        return _request(url, headers, body, timeout)
    if time.monotonic() >= deadline:
        raise TimeoutError("probe_deadline")

    # urllib's timeout bounds an individual socket operation. A slow header or
    # body can keep those operations succeeding indefinitely. Own the transport
    # in a process so expiry also closes the socket and reaps the outstanding read.
    payload = json.dumps([url, headers, body, timeout]).encode("utf-8")
    with subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve())],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as worker:
        try:
            output, _ = worker.communicate(payload, timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError("probe_deadline") from None
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()
    if worker.returncode == 2 or time.monotonic() >= deadline:
        raise TimeoutError("probe_timeout")
    if worker.returncode:
        raise ValueError("probe_request_failed")
    status, message = json.loads(output)
    return status, message


if __name__ == "__main__":
    try:
        result = _request(*json.load(sys.stdin.buffer))
        sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    except TimeoutError:
        sys.exit(2)
    except urllib.error.URLError as exc:
        sys.exit(2 if isinstance(exc.reason, TimeoutError) else 1)
