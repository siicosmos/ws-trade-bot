"""Apply a staged release update (called by the launcher before
relaunching the app).

The updater stages the new build in .update_staging/ and writes
.update_pending.json, then exits so open file handles (sqlite
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
    STATE_FILES,
    STAGING_DIR,
)
from core.ops.updater import UPDATE_RECORD  # noqa: E402

# code dirs replaced wholesale on an update
CODE_DIRS = ("core", "consumer")
# root files shipped in the zip
CODE_FILES = ("run.py", "requirements.txt", "config.example.yaml")


def apply(root):
    """Apply the staged update; True when one was applied, False
    when nothing was pending."""
    pending_path = os.path.join(root, PENDING_FILE)
    try:
        with open(pending_path) as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return False   # nothing pending (or corrupt - ignore it)

    staging = os.path.join(root, pending.get("staging") or STAGING_DIR)
    if not os.path.isdir(staging):
        # staged build missing - drop the marker, keep running
        os.remove(pending_path)
        return False

    backup = os.path.join(root, ".update_backup")
    shutil.rmtree(backup, ignore_errors=True)

    # 1. move the state files out of the code dirs
    consumer = os.path.join(root, "consumer")
    saved = []
    for name in STATE_FILES:
        src = os.path.join(consumer, name)
        if os.path.exists(src):
            os.makedirs(backup, exist_ok=True)
            shutil.move(src, os.path.join(backup, name))
            saved.append(name)

    try:
        # 2. replace the code dirs
        for d in CODE_DIRS:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
            if os.path.isdir(os.path.join(staging, d)):
                shutil.copytree(os.path.join(staging, d),
                                os.path.join(root, d))
        for name in CODE_FILES:
            src = os.path.join(staging, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(root, name))
        # launcher .bat files are skipped: cmd re-reads the
        # running batch file, replacing it mid-loop is undefined
        scripts_src = os.path.join(staging, "scripts")
        if os.path.isdir(scripts_src):
            scripts_dst = os.path.join(root, "scripts")
            os.makedirs(scripts_dst, exist_ok=True)
            for fn in os.listdir(scripts_src):
                if fn.endswith(".bat"):
                    continue
                shutil.copy2(os.path.join(scripts_src, fn),
                             os.path.join(scripts_dst, fn))
        ver = os.path.join(staging, "VERSION")
        if os.path.exists(ver):
            shutil.copy2(ver, os.path.join(root, "VERSION"))
    except Exception:
        # put the state files back before giving up - the old
        # code dirs are gone but the data must survive
        for name in saved:
            src = os.path.join(backup, name)
            if os.path.exists(src) and not os.path.exists(
                os.path.join(consumer, name)
            ):
                shutil.move(src, os.path.join(consumer, name))
        raise

    # 3. restore the state files
    for name in saved:
        shutil.move(os.path.join(backup, name),
                    os.path.join(consumer, name))
    shutil.rmtree(backup, ignore_errors=True)

    # 4. record + clear the staging area
    commit = str(pending.get("commit") or "?")
    try:
        with open(os.path.join(root, UPDATE_RECORD), "w") as f:
            json.dump(
                {"how": "release", "commit": commit, "ts": time.time()},
                f,
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