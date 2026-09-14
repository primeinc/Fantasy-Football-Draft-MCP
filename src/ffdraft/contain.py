"""Run one command and kill everything it started when it exits.

`python -B -m ffdraft.contain -- CMD...` exits with CMD's code. A process CMD
or its descendants start directly, detached or not, does not outlive it.

  Windows  this process joins a new job object with
           JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE before starting CMD, so CMD and
           every descendant are in the job; the job does not set BREAKAWAY_OK,
           so CREATE_BREAKAWAY_FROM_JOB does not leave it. The job handle is
           never closed explicitly: when this process exits, or is killed on a
           timeout, the last handle closes and Windows terminates every process
           still in the job (Microsoft Learn: JOBOBJECT_BASIC_LIMIT_INFORMATION,
           AssignProcessToJobObject). A process a service creates on the job's
           behalf (Task Scheduler through schtasks, WMI Win32_Process.Create,
           the service control manager) is never in the job and survives.
  POSIX    CMD leads a new session; after it exits its process group is killed.
           A descendant that calls setsid itself leaves the group and survives.

The runner starts every agent and every `just check` through this, so code the
fixer wrote cannot leave a directly started process that writes after the
runner's last fingerprint.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def _join_kill_on_close_job() -> None:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())


def run(cmd: list[str]) -> int:
    if sys.platform == "win32":
        _join_kill_on_close_job()
        return subprocess.call(cmd)
    child = subprocess.Popen(cmd, start_new_session=True)
    try:
        return child.wait()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--":
        print("usage: python -m ffdraft.contain -- CMD...", file=sys.stderr)
        return 2
    return run(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
