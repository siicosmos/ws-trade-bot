"""Apply a staged release update (called by the launcher before
relaunching the app).

The updater stages the new build in .update_staging/ and writes
the pending marker, then exits so open file handles (sqlite
wal, logs) are released. This script swaps the code dirs in,
preserving the state files, and clears the staging area.

usage: python scripts/apply_update.py <repo_root>
"""

import json
import os
import shutil
import sys
import time

# the repo root must be importable before the core import - the
# launcher runs this as scripts/apply_update.py from consumer/
_root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, _root)

from core.ops.release_updater import (  # noqa: E402
    PENDING_FILE,
    STAGING_DIR,
)
from core.ops.updater import update_record_path  # noqa: E402

# code dirs replaced wholesale on an update
CODE_DIRS = ("core", "consumer")
# root files shipped in the zip (subpaths allowed)
CODE_FILES = ("run.py", "requirements.txt",
              os.path.join("config", "consumer.example.config.yaml"))


def apply(root):
    """Apply the staged update; True when one was applied, False
    when nothing was pending."""
    pending_path = os.path.join(root, PENDING_FILE)
    pending = None
    try:
        with open(pending_path, encoding="utf-8") as f:
            pending = json.load(f)
    except OSError:
        return False   # nothing pending
    except ValueError:
        # a torn marker would block staging forever ("staged
        # update pending restart") - it is garbage, drop it
        try:
            os.remove(pending_path)
        except OSError:
            pass
        return False

    # the staging name comes from a json file anyone with write
    # access to consumer/ could tamper with - only the literal
    # expected directory name is honored (a ".." would make the
    # cleanup rmtree delete arbitrary directories)
    staging_name = pending.get("staging") or STAGING_DIR
    if staging_name != STAGING_DIR:
        print(f"apply_update: unexpected staging name "
              f"{staging_name!r} - ignoring the pending marker")
        os.remove(pending_path)
        return False
    staging = os.path.join(root, STAGING_DIR)
    if not os.path.isdir(staging):
        # staged build missing - drop the marker, keep running
        os.remove(pending_path)
        return False

    # the swap replaces code only: the live configs live in
    # config/ and the ledgers in db/ - paths this script never
    # touches, so there is no state to back up and nothing it can
    # wipe (the previous scheme moved the state files out of
    # consumer/ individually and rmtree'd the dir - when the
    # state files were not in consumer/ the wipe took the config,
    # tokens and ledger with it)
    #
    # order matters: copy the staged tree OVER the live one
    # first (a mid-copy failure leaves the old code intact and
    # running), and only after the copy succeeded remove the
    # files the new release deleted - a destructive step first
    # (rmtree + overlay) could brick the tree on a locked file
    # and would keep resurrecting modules the new release removed
    for d in CODE_DIRS:
        src_dir = os.path.join(staging, d)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = os.path.join(root, d)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        staged = set()
        for dirpath, _dirnames, filenames in os.walk(src_dir):
            rel = os.path.relpath(dirpath, src_dir)
            for fn in filenames:
                staged.add(
                    fn if rel == "." else os.path.join(rel, fn)
                )
        for dirpath, _dirnames, filenames in os.walk(dst_dir):
            rel = os.path.relpath(dirpath, dst_dir)
            for fn in filenames:
                if rel != "." and "__pycache__" in rel.split(os.sep):
                    continue
                if fn.endswith(".pyc"):
                    continue
                if fn not in staged:
                    try:
                        os.remove(os.path.join(dirpath, fn))
                    except OSError:
                        pass

    for name in CODE_FILES:
        src = os.path.join(staging, name)
        if os.path.exists(src):
            dst = os.path.join(root, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    # the release marker: at the repo root - outside the swapped
    # dirs (a later failure cannot leave the updater blind)
    ver = os.path.join(staging, "VERSION")
    if os.path.exists(ver):
        shutil.copy2(ver, os.path.join(root, "VERSION"))
    # launcher .bat files are skipped: cmd re-reads the running
    # batch file, replacing it mid-loop is undefined
    scripts_src = os.path.join(staging, "scripts")
    if os.path.isdir(scripts_src):
        scripts_dst = os.path.join(root, "scripts")
        os.makedirs(scripts_dst, exist_ok=True)
        staged_scripts = set()
        for fn in os.listdir(scripts_src):
            if fn.endswith(".bat"):
                continue
            staged_scripts.add(fn)
            shutil.copy2(os.path.join(scripts_src, fn),
                         os.path.join(scripts_dst, fn))
        # scripts the new release dropped (the other roles'
        # launchers) do not linger on the install
        for fn in os.listdir(scripts_dst):
            if fn in staged_scripts or fn.endswith(".bat"):
                continue
            try:
                os.remove(os.path.join(scripts_dst, fn))
            except OSError:
                pass
    # record + clear the staging area
    commit = str(pending.get("commit") or "?")
    try:
        from core.ops.updater import atomic_write_json

        atomic_write_json(
            update_record_path(root, "consumer"),
            {"how": "release", "commit": commit, "ts": time.time()},
        )
        # any other live role on this checkout (the info server)
        # sees this marker and restarts - lazily-imported modules
        # would otherwise mix the old and new code
        atomic_write_json(
            os.path.join(root, ".code_swapped.json"),
            {"commit": commit, "ts": time.time()},
        )
    except OSError:
        pass
    shutil.rmtree(staging, ignore_errors=True)
    os.remove(pending_path)
    print(f"apply_update: applied {commit}")
    return True


def main(argv):
    root = argv[1] if len(argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    try:
        apply(root)
    except Exception as e:
        print(f"apply_update failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))