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
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# A stuck taskkill must never block launcher shutdown.
TASKKILL_TIMEOUT_SECONDS = 5.0


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in [
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("GetProcessTimes", [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                             ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)], wintypes.BOOL),
        ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        ("Process32FirstW", [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        ("Process32NextW", [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def process_identity(pid):
    """Return (pid, creation time) for a readable process, else None.

    The pair separates a recorded pid from a reused one. None means "could not verify"
    (gone or access denied), never a match: callers must stay conservative on it.
    """
    if os.name != "nt" or not isinstance(pid, int):
        return None
    kernel = _kernel()
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel_time, user_time = wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return (pid, (created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def _process_parents():
    """Map every visible process id to its parent id from one Toolhelp snapshot."""
    if os.name != "nt":
        return {}
    try:
        kernel = _kernel()
        snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    except OSError:
        return {}
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return {}
    parents = {}
    entry = _ProcessEntry32()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return parents


def own_tree_pids():
    """The launcher and its ancestors: terminating one of these would end the launcher."""
    pids, pid = set(), os.getpid()
    parents = _process_parents()
    while pid and pid not in pids:
        pids.add(pid)
        pid = parents.get(pid, 0)
    return pids


def _taskkill_tree(pid, timeout=TASKKILL_TIMEOUT_SECONDS):
    """Kill a pid and its descendants; bounded so a stuck taskkill cannot block shutdown."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False, timeout=timeout)


def kill_owned_tree(process, identity, *, timeout=TASKKILL_TIMEOUT_SECONDS):
    """Terminate the tree of a spawned process only while the pid still names that process.

    identity is the (pid, creation time) pair captured while the child was known alive.
    A verified mismatch, or a pid inside our own tree, refuses every kill ("refused").
    An unverifiable identity, a failed or timed-out taskkill, and a taskkill refusal
    all fall back to terminating the owned root handle only ("root"); a verified tree
    kill reports "tree". The root kill always runs after a kill decision.
    """
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid in own_tree_pids():
        return "refused"
    current = process_identity(pid)
    verified = identity is not None and current is not None and tuple(identity) == tuple(current)
    if identity is not None and current is not None and not verified:
        return "refused"
    outcome = "root"
    try:
        if verified and os.name == "nt":
            try:
                _taskkill_tree(pid, timeout)
                outcome = "tree"
            except (OSError, subprocess.SubprocessError):
                outcome = "root"
    finally:
        try:
            process.kill()
        except OSError:
            pass
    return outcome


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
        # Exactly {"ready": true} means success; a failure may add step/error for diagnosis.
        if not isinstance(message, dict) or message.get("ready") is not True:
            extra = set(message) - {"ready", "step", "error"} if isinstance(message, dict) else set()
            step = (message or {}).get("step") if isinstance(message, dict) else None
            detail = (message or {}).get("error") if isinstance(message, dict) else None
            suffix = f" ({step} failed, Win32 {detail})" if step else ""
            if extra:
                raise ValueError("Guardian not ready (unexpected fields)")
            raise ValueError(f"Guardian not ready{suffix}")
        if set(message) != {"ready"}:
            raise ValueError("Guardian not ready (unexpected fields)")
    except queue.Empty:
        raise RuntimeError("Local process cleanup unavailable (guardian produced no answer)") from None
    except (ValueError, TypeError) as error:
        # Killing this guardian could kill its parent; the CLI entry point must exit instead.
        raise RuntimeError(f"Local process cleanup unavailable: {error}") from None
    _guardian.stdout.close()


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


def _make_kill_on_close_job(kernel):
    """Return ``(job_handle, failing_step, win32_error)`` for an anonymous kill-on-close job."""
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None, "CreateJobObjectW", ctypes.get_last_error()
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        detail = ctypes.get_last_error()
        kernel.CloseHandle(job)
        return None, "SetInformationJobObject", detail
    return job, None, 0


def close_job(job):
    """Close a job handle; every process still in the job is terminated."""
    if job:
        _kernel().CloseHandle(job)


def create_job_for(pid):
    """Put one already-started process in a kill-on-close job and return its handle.

    Closing the handle terminates descendants the process left behind even when its root
    has exited, which ``taskkill /T`` can no longer find at that point. Returns None on
    non-Windows or when the job cannot be created/assigned (the caller keeps its existing
    cleanup path). Assignment after spawn has a small race, but a CLI's children normally
    start after argument parsing; a failed assignment never leaves an open job behind.
    """
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    kernel = _kernel()
    job, step, detail = _make_kill_on_close_job(kernel)
    if not job:
        return None
    handle = kernel.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                False, pid)
    if not handle:
        kernel.CloseHandle(job)
        return None
    try:
        if not kernel.AssignProcessToJobObject(job, handle):
            kernel.CloseHandle(job)
            return None
        return job
    finally:
        kernel.CloseHandle(handle)


def _watch(parent):
    kernel = _kernel()
    job, step, detail = _make_kill_on_close_job(kernel)
    try:
        if not job:
            # Name the failing call: "check permissions" alone cannot be acted on.
            print(json.dumps({"ready": False, "step": step, "error": detail}), flush=True)
            return 1
        if not kernel.AssignProcessToJobObject(job, parent):
            print(json.dumps({"ready": False, "step": "AssignProcessToJobObject",
                              "error": ctypes.get_last_error()}), flush=True)
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
