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
BASH = shutil.which("bash") or (str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"))
CLI = r"""
import http.client,json,subprocess,sys,time,urllib.request
from pathlib import Path
bash=subprocess.Popen([sys.argv[4],'--noprofile','--norc','-c',
    '''"$1" -u -c 'import json,os,time;print(json.dumps({"grandchild_pid":os.getpid()}),flush=True);time.sleep(60)' & wait''',
    'synthetic-shell',Path(sys.executable).as_posix()],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=sys.stderr)
print(json.dumps({'bash_pid':bash.pid}),flush=True)
if sys.argv[1] in ('normal','error'):
    sys.stdin.readline()
    sys.exit(0 if sys.argv[1]=='normal' else 7)
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
node,url,mode,cli_code,bash=sys.argv[1:]
original=subprocess.Popen
proxy=None
def spawn(args,**kwargs):
    global proxy
    if args[0] == 'synthetic-cli':
        env=kwargs['env']
        child=original([sys.executable,'-u','-c',cli_code,mode,env['ANTHROPIC_BASE_URL'],env['ANTHROPIC_API_KEY'],bash],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,**kwargs)
        print(json.dumps({'proxy_pid':proxy.pid,'cli_pid':child.pid,'guardian_pid':a.localProcessTree._guardian.pid,'port':urlsplit(env['ANTHROPIC_BASE_URL']).port}),flush=True)
        return child
    proxy=original(args,**kwargs)
    return proxy
config={'SONNET_PROVIDER':'local','LOCAL_LLM_URL':url,'LOCAL_LLM_MODEL':'synthetic-model','LOCAL_LLM_API_KEY':'synthetic-local-key'}
with patch.object(a,'provider_config',return_value=config),patch.object(a.shutil,'which',side_effect=lambda n:'synthetic-cli' if n=='claude' else node),patch.object(a.subprocess,'run',return_value=Mock(stdout='2.1.304')),patch.object(a.subprocess,'Popen',side_effect=spawn),patch.object(a,'run_owned',side_effect=AssertionError('Plan auth forbidden')):
    a.localProcessTree.ensure_windows_local_tree()
    sys.exit(a.launch('local',local_only=True))
"""


@unittest.skipUnless(os.name == "nt" and NODE and Path(BASH).is_file(), "Windows, Node.js and Git Bash required")
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
        parent = subprocess.Popen([sys.executable, "-u", "-c", LAUNCHER, NODE, url, mode, CLI, BASH],
                                  cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="backslashreplace")
        def cleanup():
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            for stream in (parent.stdin, parent.stdout, parent.stderr):
                stream.close()
        self.addCleanup(cleanup)
        ready = queue.Queue()
        diagnostics = []
        def read_ready():
            info = {}
            for line in parent.stdout:
                if line.startswith('{'):
                    info.update(json.loads(line))
                    if {"proxy_pid", "cli_pid", "guardian_pid", "bash_pid", "grandchild_pid"} <= info.keys():
                        ready.put(info)
                        return
                else:
                    diagnostics.append(line.strip())
        threading.Thread(target=read_ready, daemon=True).start()
        try:
            info = ready.get(timeout=10)
        except queue.Empty:
            self.fail("Synthetic process tree did not become ready: " + repr(diagnostics[-3:]))
        for name in ("guardian", "bash", "grandchild"):
            info[name + "_handle"] = self.owned_handle(info[name + "_pid"])
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
                self.close_connection = True
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
                for name in ("guardian", "bash", "grandchild"):
                    self.assertEqual(self.kernel.WaitForSingleObject(info[name + "_handle"], 5000), 0)
                self.assertFalse(self.listener_open(info["port"]))
                if mode == "active":
                    self.assertTrue(closed.wait(5))
                    self.assertEqual(requests, ["/v1/messages"])
                self.assertNotIn('"provider":"plan"', parent.stderr.read())

    def test_killed_launcher_closes_entire_tree_and_active_connection(self):
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
                self.assertEqual(self.kernel.WaitForSingleObject(cli, 5000), 0)
                for name in ("guardian", "bash", "grandchild"):
                    self.assertEqual(self.kernel.WaitForSingleObject(info[name + "_handle"], 5000), 0)
                output = parent.stderr.read()
                self.assertNotIn('"provider":"plan"', output)

    def test_normal_and_error_exit_preserve_codes_and_clean_remaining_children(self):
        for mode, code in (("normal", 0), ("error", 7)):
            with self.subTest(mode=mode):
                parent, info, proxy, cli = self.start_launcher("http://127.0.0.1:9/v1/messages", mode)
                parent.stdin.write("finish\n")
                parent.stdin.flush()
                self.assertEqual(parent.wait(timeout=10), code)
                for handle in (proxy, cli, *(info[n + "_handle"] for n in ("guardian", "bash", "grandchild"))):
                    self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)
                self.assertFalse(self.listener_open(info["port"]))

    def test_guardian_failure_closes_tree_without_touching_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)
        self.addCleanup(cleanup)
        parent, info, proxy, cli = self.start_launcher("http://127.0.0.1:9/v1/messages", "idle")
        self.assertTrue(self.kernel.TerminateProcess(info["guardian_handle"], 1))
        parent.wait(timeout=10)
        for handle in (proxy, cli, info["bash_handle"], info["grandchild_handle"]):
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)
        self.assertFalse(self.listener_open(info["port"]))
        self.assertIsNone(unrelated.poll())

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
