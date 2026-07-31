"""Managed subprocess spawning for LLM-agent CLIs (claude / codex / hermes).

Every agent CLI we run (inline /ask, agent_task delegation, the `ORTHUS_LLM=cli`
distill/qa/compile backend) goes through `run_managed_subprocess` so the same
three safety properties hold everywhere — the lack of which caused the
2026-06-28 prod hang (≈162 orphaned `codex exec` / `codex-app-server`):

1. **Process-group tree kill on timeout.** The child is started in its OWN
   session/process group (`start_new_session=True`); on timeout we
   `os.killpg(SIGKILL)` the whole group so the agent's grandchildren
   (`codex-app-server`, a spawned orthus-mcp stdio server, …) die WITH it instead
   of orphaning. `subprocess.run`/`Popen.kill()` only signal the direct child,
   which is exactly how the orphans accumulated.
2. **stdin is never the controlling terminal.** With no `input_text` we attach
   `stdin=DEVNULL` so an agent that reads stdin can't block forever waiting for
   input it will never get (and can't see the parent's stdin).
3. The new session also detaches the child from the API process's process group,
   so `killpg` can NEVER reach back up and kill the central API itself.

This module owns NO concurrency cap — callers bound their own arrival rate
(`agent_runner._ask_concurrency_semaphore`, `cli._llm_cli_semaphore`).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable

# (stream_name, line) -> None. ``stream_name`` is "stdout" or "stderr".
OnChunk = Callable[[str, str], None]


def kill_process_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group; fall back to the direct child.

    Safe because the child was started with ``start_new_session=True``, so its
    process-group id is its own pid — the central API process is in a different
    group and is never signalled.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Group gone / unsupported (non-POSIX): best-effort direct kill.
        try:
            proc.kill()
        except OSError:
            pass


def run_managed_subprocess(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float,
    input_text: str | None = None,
    on_chunk: OnChunk | None = None,
) -> tuple[str, str, int | None, bool]:
    """Run ``argv`` to completion, returning ``(stdout, stderr, returncode, timed_out)``.

    - The child runs in its own process group; on timeout the WHOLE group is
      SIGKILL'd (tree kill) and ``timed_out=True`` is returned with
      ``returncode=None`` — same shape every caller already builds its result from.
    - ``input_text`` is written to the child's stdin then closed; without it stdin
      is ``DEVNULL`` so the child can't block on input.
    - ``on_chunk`` (optional) is called with each ``(stream, line)`` as it arrives;
      a sink error is swallowed so it never stops capture.
    """
    popen_kwargs: dict = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        # Own session/group so a timeout can tree-kill grandchildren and the kill
        # can never propagate up to the API process.
        "start_new_session": True,
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
    }
    if env is not None:
        popen_kwargs["env"] = env

    proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 - argv list, never shell=True
    out_parts: list[str] = []
    err_parts: list[str] = []

    def _pump(pipe, parts: list[str], stream_name: str) -> None:
        try:
            for line in pipe:
                parts.append(line)
                if on_chunk is not None:
                    try:
                        on_chunk(stream_name, line)
                    except Exception:  # noqa: BLE001 - a sink error must not stop capture
                        pass
        except Exception:  # noqa: BLE001 - pipe read race on kill; keep what we have
            pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_parts, "stdout"), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_parts, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    # Feed stdin AFTER the output pumps are draining, so a child that echoes a
    # large prompt back can't deadlock on a full stdout pipe while we write stdin.
    if input_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        proc.wait()
    # Pumps exit promptly once the pipes close at process exit.
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    returncode = None if timed_out else proc.returncode
    return "".join(out_parts), "".join(err_parts), returncode, timed_out
