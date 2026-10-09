"""Stale-process cleanup for self-restarting entry points.

A hard restart (os._exit, crash, or the .bat restart loop) can
leave the previous instance alive - holding the dashboard port
or the Discord UIA tree. Each entry point terminates leftovers
of itself before starting work.
"""

import os

from core.redact import format_error


def _protected_pids(me):
    """Me plus every ancestor.

    On Windows a venv python.exe is a launcher that spawns the
    real interpreter as a child with an identical command line -
    the launcher is our parent. Killing it (exit code 15) makes
    the .bat loop restart us while this interpreter runs on as
    an orphan: a self-restart loop with zombie copies."""
    pids = {me}
    try:
        import psutil

        proc = psutil.Process(me)
        for _ in range(10):
            ppid = proc.ppid()
            if ppid <= 0 or ppid in pids:
                break
            pids.add(ppid)
            proc = psutil.Process(ppid)
    except Exception:
        pass
    return pids


def _candidate_config(cmd, cwd):
    """A candidate process's resolved -c/--config value, resolved
    against its own working directory. No -c means the default
    config.yaml in that directory."""
    for i, c in enumerate(cmd):
        s = str(c)
        if s in ("-c", "--config"):
            if i + 1 < len(cmd):
                return os.path.normcase(os.path.abspath(
                    os.path.join(cwd, str(cmd[i + 1]))
                ))
        elif s.startswith("--config="):
            return os.path.normcase(os.path.abspath(
                os.path.join(cwd, s.split("=", 1)[1])
            ))
    return os.path.normcase(
        os.path.abspath(os.path.join(cwd, "config.yaml"))
    )


def terminate_stale_instances(script_path, config_path=None, log=print):
    """Kill python processes running this same entry point.

    Matches a python process whose command line references this
    exact script (full path or basename). With config_path set,
    the candidate must also run the SAME resolved config (its -c
    value resolved against its own working directory): each role
    lives in its own folder with its own config.yaml, and the
    info server and the consumer app share run.py - without this
    check they would terminate each other in a restart loop.
    Without config_path the legacy rule applies (same script
    directory). Returns the pids that were terminated.

    Concurrency note: there is a check-then-kill window - a pid
    that matched both filters at scan time could in theory be a
    different process by the terminate() call (pid reuse). That
    is acceptable here: this is a single-user local machine, and
    the two-key filter already scopes the scan to this repo's own
    entry points, so a chance collision on both keys is
    astronomically unlikely. A shared or multi-tenant host would
    need real ownership verification (pid + psutil create_time(),
    never a pid alone) or a supervisor that owns the lifecycle
    (systemd/supervisord) - do not lift this function into that
    environment unchanged.
    """
    try:
        import psutil
    except ImportError:
        return []

    me = os.getpid()
    protected = _protected_pids(me)
    script = os.path.normcase(os.path.abspath(script_path))
    script_dir = os.path.normcase(os.path.dirname(script))
    my_cfg = (
        os.path.normcase(os.path.abspath(config_path))
        if config_path else None
    )
    stale = []
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["pid"] in protected:
                    continue
                name = (p.info["name"] or "").lower()
                if "python" not in name:
                    continue
                cmd = p.info["cmdline"] or []
                if not any(
                    os.path.normcase(os.path.abspath(str(c)
                                  if os.path.isabs(str(c)) else str(c)))
                    == script
                    or os.path.normcase(
                        os.path.basename(str(c))
                    ) == os.path.basename(script)
                    for c in cmd
                ):
                    continue
                try:
                    cwd = os.path.normcase(p.cwd())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                if my_cfg is not None:
                    if _candidate_config(cmd, cwd) != my_cfg:
                        continue
                elif cwd != script_dir:
                    continue
                stale.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:  # psutil internals vary by platform
        try:
            log(f"stale-process check failed: {format_error(e)}")
        except Exception:
            pass
        return []

    for p in stale:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        result = psutil.wait_procs(stale, timeout=3)
        gone, alive = (
            result if len(result) == 2 else (stale, [])
        )
    except Exception:
        gone, alive = stale, []
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    killed = [p.pid for p in stale]
    if killed:
        try:
            log(
                "terminated stale process(es): "
                + ", ".join(str(k) for k in killed)
            )
        except Exception:
            pass
    return killed
