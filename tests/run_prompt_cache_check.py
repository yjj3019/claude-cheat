# SPDX-License-Identifier: MIT
"""Run the required cache probe against a disposable Windows x64 CPU engine."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile


ENGINE_URL = ("https://github.com/ggml-org/llama.cpp/releases/download/b12818/"
              "llama-b12818-bin-win-cpu-x64.zip")
ENGINE_SHA256 = "cc1e850be7b46be0e1e425c7c6df7eb2608970748862fd120611a1cd263e9d94"
MODEL_URL = ("https://raw.githubusercontent.com/samx18/llamacpp-inf-flow/"
             "38a500b7b096ad207d6a563a4b86327096fe65bd/models/stories260K.gguf")
MODEL_SHA256 = "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
ROOT = Path(__file__).resolve().parents[1]


def download(url, destination, expected):
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as target:
        shutil.copyfileobj(response, target)
    with destination.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != expected:
        raise RuntimeError(f"SHA-256 mismatch for {destination.name}")


def wait_until_ready(server, port):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"llama-server exited during startup ({server.returncode})")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            if response.status == 200 and json.load(response).get("status") == "ok":
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()
        time.sleep(0.2)
    raise TimeoutError("llama-server did not become ready")


def run_probe(port):
    env = dict(os.environ, LLAMA_CACHE_PROBE_REQUIRED="1", LLAMA_CACHE_PROBE_URL=f"http://127.0.0.1:{port}")
    result = subprocess.run(
        ["node", "--test", "--test-reporter=tap", "tests/prompt_cache_reference.test.mjs"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True, encoding="utf-8", timeout=120,
    )
    print(result.stdout, end="", flush=True)
    result.check_returncode()
    # Node can exit successfully with an empty, skipped or TODO suite.
    counts = dict(re.findall(r"^# (tests|pass|skipped|todo) (\d+)$", result.stdout, re.MULTILINE))
    if (counts.get("pass") in (None, "0") or counts.get("tests") != counts["pass"]
            or counts.get("skipped") != "0" or counts.get("todo") != "0"):
        raise RuntimeError("The required real-engine cache probe did not fully execute")


def main():
    with tempfile.TemporaryDirectory(prefix="prompt-cache-check-") as directory:
        work = Path(directory)
        archive, model = work / "engine.zip", work / "model.gguf"
        download(ENGINE_URL, archive, ENGINE_SHA256)
        download(MODEL_URL, model, MODEL_SHA256)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(work)
        binary, = work.rglob("llama-server.exe")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with subprocess.Popen(
            [str(binary), "--host", "127.0.0.1", "--port", str(port), "-m", str(model),
             "-c", "2048", "-np", "1", "-t", "2", "-ngl", "0", "--no-webui"],
            cwd=work, creationflags=subprocess.CREATE_NO_WINDOW,
        ) as server:
            try:
                wait_until_ready(server, port)
                run_probe(port)
                if server.poll() is not None:
                    raise RuntimeError("llama-server exited during the cache probe")
            finally:
                server.kill()


if __name__ == "__main__":
    main()
