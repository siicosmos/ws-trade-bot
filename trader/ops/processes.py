"""Stale-process cleanup for self-restarting entry points.

A hard restart (os._exit, crash, or the .bat restart loop) can
leave the previous instance alive - holding the dashboard port
or the Discord UIA tree. Each entry point terminates leftovers
of itself before starting work.
"""

import os


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


def terminate_stale_instances(script_path, log=print):
    """Kill python processes running this same entry point.

    Matches a python process whose command line references this
    exact script (full path or basename) and whose working
    directory is this script's directory. Returns the pids that
    were terminated.
    """
    try:
        import psutil
    except ImportError:
        return []

    me = os.getpid()
    protected = _protected_pids(me)
    script = os.path.normcase(os.path.abspath(script_path))
    script_dir = os.path.normcase(os.path.dirname(script))
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
                if cwd != script_dir:
                    continue
                stale.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:  # psutil internals vary by platform
        try:
            log(f"stale-process check failed: {e}")
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
