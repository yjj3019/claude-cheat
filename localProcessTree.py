"""Own a Windows Local CLI process tree until its standalone launcher exits."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


_guardian = None


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in [
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def ensure_windows_local_tree():
    """Call only from the standalone Local CLI entry point, before spawning children."""
    global _guardian
    if os.name != "nt":
        return
    if _guardian is not None:
        if _guardian.poll() is not None:
            raise RuntimeError("Local process cleanup unavailable")
        return
    kernel = _kernel()
    parent = kernel.OpenProcess(0x100101, True, os.getpid())  # SYNCHRONIZE | SET_QUOTA | TERMINATE
    if not parent:
        raise OSError("Local process cleanup unavailable")
    try:
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [parent]})
        # No provider configuration is needed by the guardian.
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        _guardian = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).resolve()), str(parent)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     startupinfo=startup, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
                                     env=env, text=True, encoding="utf-8")
    finally:
        kernel.CloseHandle(parent)
    ready = queue.Queue()
    def read_ready():
        try:
            ready.put(_guardian.stdout.readline())
        except (OSError, UnicodeError):
            ready.put("")
    threading.Thread(target=read_ready, daemon=True).start()
    try:
        message = json.loads(ready.get(timeout=15))
        if not isinstance(message, dict) or set(message) != {"ready"} or message["ready"] is not True:
            raise ValueError("Guardian not ready")
    except (queue.Empty, ValueError, TypeError):
        # Killing this guardian could kill its parent; the CLI entry point must exit instead.
        raise RuntimeError("Local process cleanup unavailable") from None
    _guardian.stdout.close()


def _watch(parent):
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, parent):
            print(json.dumps({"ready": False}), flush=True)
            return 1
        print(json.dumps({"ready": True}), flush=True)
        return 0 if kernel.WaitForSingleObject(parent, 0xFFFFFFFF) == 0 else 1
    finally:
        if job:
            kernel.CloseHandle(job)
        kernel.CloseHandle(parent)


if __name__ == "__main__":
    try:
        sys.exit(_watch(int(sys.argv[1])))
    except (OSError, ValueError, IndexError):
        print(json.dumps({"ready": False}), flush=True)
        sys.exit(1)
