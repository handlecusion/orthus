"""Managed agent-subprocess spawn (orthus/proc.py).

These exercise the REAL subprocess machinery (short-lived python children) — the
tree-kill test is the regression guard for the 2026-06-28 codex orphan pileup:
on timeout the whole process group must die, not just the direct child.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from orthus.proc import run_managed_subprocess


def test_batch_capture_returns_output():
    stdout, stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, "-c", "print('hello')"], timeout_s=10
    )
    assert stdout == "hello\n"
    assert stderr == ""
    assert rc == 0
    assert timed_out is False


def test_streaming_surfaces_each_line():
    chunks: list[tuple[str, str]] = []
    stdout, _stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, "-u", "-c", "print('a'); print('b')"],
        timeout_s=10,
        on_chunk=lambda stream, line: chunks.append((stream, line)),
    )
    assert ("stdout", "a\n") in chunks and ("stdout", "b\n") in chunks
    assert stdout == "a\nb\n"
    assert rc == 0 and timed_out is False


def test_nonzero_returncode_surfaced():
    _stdout, _stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, "-c", "import sys; sys.exit(3)"], timeout_s=10
    )
    assert rc == 3
    assert timed_out is False


def test_stdin_is_devnull_when_no_input():
    # A child reading stdin must get immediate EOF (DEVNULL), never block on input.
    stdout, _stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.read())))"],
        timeout_s=10,
    )
    assert stdout == "0"
    assert rc == 0 and timed_out is False


def test_input_text_is_fed_and_closed():
    stdout, _stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        timeout_s=10,
        input_text="echo me",
    )
    assert stdout == "echo me"
    assert rc == 0 and timed_out is False


def test_timeout_tree_kills_grandchild(tmp_path):
    """The core regression guard: a timed-out agent's CHILD process is reaped too.

    The parent spawns a grandchild that records its pid and sleeps 60s; the parent
    also sleeps. With the old `proc.kill()` (direct child only) the grandchild would
    orphan and survive — exactly the codex-app-server pileup. The process-group
    SIGKILL must take the whole tree down.
    """
    pidfile = tmp_path / "grandchild.pid"
    child_py = tmp_path / "child.py"
    child_py.write_text(
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    parent_py = tmp_path / "parent.py"
    parent_py.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_py)!r}])\n"
        "time.sleep(60)\n"
    )

    stdout, stderr, rc, timed_out = run_managed_subprocess(
        [sys.executable, str(parent_py)], timeout_s=2
    )
    assert timed_out is True
    assert rc is None

    # Wait for the grandchild to have written its pid (it does so at startup).
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.1)
    pid = int(pidfile.read_text().strip())

    # The grandchild must be gone (reaped by the process-group kill).
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)  # cleanup before failing
        pytest.fail("grandchild survived the timeout — process-group tree-kill failed")
