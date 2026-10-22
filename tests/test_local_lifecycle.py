"""Windows process ownership tests: real proxy, synthetic CLI/upstream, no private settings."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
CLI = r"""
import http.client,json,sys,time,urllib.request
if sys.argv[1] == 'active':
    body=json.dumps({'model':'claude-sonnet-5-5','max_tokens':64,'messages':[{'role':'user','content':'synthetic'}]}).encode()
    request=urllib.request.Request(sys.argv[2]+'/v1/messages',data=body,headers={'Content-Type':'application/json','X-Local-Route-Token':sys.argv[3]})
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=10).read()
    except (OSError,http.client.HTTPException):
        pass
time.sleep(60)
"""
LAUNCHER = r"""
import json,subprocess,sys
from unittest.mock import patch,Mock
from urllib.parse import urlsplit
import openrouterAdvisor as a
node,url,mode,cli_code=sys.argv[1:]
original=subprocess.Popen
proxy=None
def spawn(args,**kwargs):
    global proxy
    if args[0] == 'synthetic-cli':
        env=kwargs['env']
        child=original([sys.executable,'-u','-c',cli_code,mode,env['ANTHROPIC_BASE_URL'],env['ANTHROPIC_API_KEY']],**kwargs)
        print(json.dumps({'proxy_pid':proxy.pid,'cli_pid':child.pid,'port':urlsplit(env['ANTHROPIC_BASE_URL']).port}),flush=True)
        return child
    proxy=original(args,**kwargs)
    return proxy
config={'SONNET_PROVIDER':'local','LOCAL_LLM_URL':url,'LOCAL_LLM_MODEL':'synthetic-model','LOCAL_LLM_API_KEY':'synthetic-local-key'}
with patch.object(a,'provider_config',return_value=config),patch.object(a.shutil,'which',side_effect=lambda n:'synthetic-cli' if n=='claude' else node),patch.object(a.subprocess,'run',return_value=Mock(stdout='2.1.304')),patch.object(a.subprocess,'Popen',side_effect=spawn),patch.object(a,'run_owned',side_effect=AssertionError('Plan auth forbidden')):
    sys.exit(a.launch('local',local_only=True))
"""


@unittest.skipUnless(os.name == "nt" and NODE, "Windows and Node.js required")
class LocalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def owned_handle(self, pid):
        handle = self.kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        self.assertTrue(handle, "test child must be alive when captured")
        def cleanup():
            if self.kernel.WaitForSingleObject(handle, 0) == 258:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 5000)
            self.kernel.CloseHandle(handle)
        self.addCleanup(cleanup)
        return handle

    def listener_open(self, port):
        with socket.socket() as connection:
            connection.settimeout(.2)
            return connection.connect_ex(("127.0.0.1", port)) == 0

    def start_launcher(self, url, mode):
        # -c retains normal Ctrl+C/stdio behavior; only CLI inference is synthetic.
        parent = subprocess.Popen([sys.executable, "-u", "-c", LAUNCHER, NODE, url, mode, CLI],
                                  cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8")
        def cleanup():
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            for stream in (parent.stdin, parent.stdout, parent.stderr):
                stream.close()
        self.addCleanup(cleanup)
        ready = queue.Queue()
        def read_ready():
            for line in parent.stdout:
                if line.startswith('{"proxy_pid"'):
                    ready.put(json.loads(line))
                    return
        threading.Thread(target=read_ready, daemon=True).start()
        info = ready.get(timeout=10)
        return parent, info, self.owned_handle(info["proxy_pid"]), self.owned_handle(info["cli_pid"])

    def stalled_upstream(self):
        entered, closed = threading.Event(), threading.Event()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"type":')
                self.wfile.flush()
                entered.set()
                self.connection.settimeout(10)
                try:
                    if not self.connection.recv(1):
                        closed.set()
                except ConnectionResetError:
                    closed.set()
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1/messages", entered, closed, requests

    def test_killed_cli_unwinds_launcher_and_closes_proxy_and_active_connection(self):
        for mode in ("idle", "active"):
            with self.subTest(mode=mode):
                url, entered, closed, requests = self.stalled_upstream()
                parent, info, proxy, cli = self.start_launcher(url, mode)
                self.assertTrue(self.listener_open(info["port"]))
                if mode == "active":
                    self.assertTrue(entered.wait(5))
                self.assertTrue(self.kernel.TerminateProcess(cli, 1))
                self.assertNotEqual(parent.wait(timeout=10), 0)
                self.assertEqual(self.kernel.WaitForSingleObject(proxy, 5000), 0)
                self.assertFalse(self.listener_open(info["port"]))
                if mode == "active":
                    self.assertTrue(closed.wait(5))
                    self.assertEqual(requests, ["/v1/messages"])
                self.assertNotIn('"provider":"plan"', parent.stderr.read())

    def test_killed_launcher_closes_proxy_and_active_connection_but_not_cli_tree(self):
        for mode in ("idle", "active"):
            with self.subTest(mode=mode):
                url, entered, closed, requests = self.stalled_upstream()
                parent, info, proxy, cli = self.start_launcher(url, mode)
                self.assertTrue(self.listener_open(info["port"]))
                if mode == "active":
                    self.assertTrue(entered.wait(5))
                parent.kill()
                parent.wait(timeout=5)
                self.assertEqual(self.kernel.WaitForSingleObject(proxy, 5000), 0)
                self.assertFalse(self.listener_open(info["port"]))
                if mode == "active":
                    self.assertTrue(closed.wait(5))
                    self.assertEqual(requests, ["/v1/messages"])
                # The pipe owns only the proxy. Keep this limitation explicit and clean our test CLI.
                self.assertEqual(self.kernel.WaitForSingleObject(cli, 0), 258)
                self.assertTrue(self.kernel.TerminateProcess(cli, 1))
                self.assertEqual(self.kernel.WaitForSingleObject(cli, 5000), 0)
                output = parent.stderr.read()
                self.assertIn('"event":"local_parent_disconnected"', output)
                self.assertNotIn('"provider":"plan"', output)

    def test_parent_pipe_option_does_not_change_ordinary_mode(self):
        env = {**os.environ, "SONNET_PROVIDER": "local", "LOCAL_ROUTE_TOKEN": "synthetic-lifecycle-nonce",
               "LOCAL_ROUTE_ONLY": "0", "LOCAL_LLM_URL": "http://127.0.0.1:9/v1/messages",
               "LOCAL_LLM_MODEL": "synthetic-model", "LOCAL_LLM_API_KEY": "synthetic-key"}
        process = subprocess.Popen([NODE, str(ROOT / "claudeRouteProxy.mjs"), "--parent-pipe"], env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        try:
            info = json.loads(process.stdout.readline())
            process.stdin.close()
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=.2)
            self.assertTrue(self.listener_open(info["port"]))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
