"""`contain`: a process the command leaves behind does not outlive it."""
import subprocess
import sys

import pytest

# The child starts a detached grandchild that holds the test's stdout pipe open,
# reports its pid on it, tells the child it is running, and waits; only then does
# the child exit. The pipe reaches EOF when every holder is gone, so reading it
# to the end waits on exactly the grandchild's death.
GRANDCHILD = ("import os, sys, threading; print(os.getpid(), flush=True); "
              "sys.stderr.write('ready\\n'); sys.stderr.flush(); threading.Event().wait(120)")
CHILD = f"""
import subprocess, sys
# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP. Not DETACHED_PROCESS: the venv's
# python.exe is a launcher that starts the base interpreter, and a console child of
# a detached process gets a new, visible console window.
flags = 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
g = subprocess.Popen([sys.executable, "-c", {GRANDCHILD!r}], creationflags=flags,
                     start_new_session=sys.platform != "win32", stdin=subprocess.DEVNULL,
                     stdout=sys.stdout, stderr=subprocess.PIPE)
g.stderr.readline()
"""


def _grandchild_outlives(contained: bool) -> bool:
    cmd = [sys.executable, "-c", CHILD]
    if contained:
        cmd = [sys.executable, "-m", "ffdraft.contain", "--", *cmd]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    pid = int(proc.stdout.readline())
    assert proc.wait(timeout=30) == 0
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        proc.communicate(timeout=30)
        return True
    return False


@pytest.mark.skipif(sys.platform != "win32", reason="POSIX containment is the process group only")
def test_a_detached_grandchild_dies_with_the_command():
    # The control first: uncontained, the grandchild is still holding the pipe.
    assert _grandchild_outlives(contained=False) is True
    assert _grandchild_outlives(contained=True) is False


@pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows only")
def test_a_timeout_kill_takes_the_grandchild_with_it():
    # subprocess.run on a timeout kills its direct child, here the venv's python.exe
    # launcher, then reads the pipes to the end. Those reach EOF only when the
    # grandchild holding them is gone, so a survivor would hang the runner.
    waits = CHILD + "\nimport threading\nthreading.Event().wait(120)\n"
    proc = subprocess.Popen([sys.executable, "-B", "-m", "ffdraft.contain", "--",
                             sys.executable, "-c", waits], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    pid = int(proc.stdout.readline())
    proc.kill()
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        proc.communicate(timeout=30)
        pytest.fail(f"grandchild {pid} outlived the killed contain")


def test_the_exit_code_is_the_commands():
    cmd = [sys.executable, "-m", "ffdraft.contain", "--", sys.executable, "-c", "raise SystemExit(7)"]
    assert subprocess.run(cmd, timeout=30).returncode == 7


def test_no_command_is_a_usage_error():
    from ffdraft import contain
    assert contain.main([]) == 2 and contain.main(["x"]) == 2
