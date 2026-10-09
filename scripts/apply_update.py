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
# root files shipped in the zip (subpaths allowed)
CODE_FILES = ("run.py", "requirements.txt",
              os.path.join("config", "consumer.config.yaml"))


def apply(root):
    """Apply the staged update; True when one was applied, False
    when nothing was pending."""
    pending_path = os.path.join(root, PENDING_FILE)
    try:
        with open(pending_path, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return False   # nothing pending (or corrupt - ignore it)

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

    backup = os.path.join(root, ".update_backup")
    shutil.rmtree(backup, ignore_errors=True)

    # 1. move the ENTIRE consumer dir aside with a rename - a
    # rename is atomic and loses nothing, so no rmtree can ever
    # touch the user's config/db/tokens before they are safe.
    # (the old code moved the state files individually and then
    # rmtree'd the dir - when the state files were not in
    # consumer/ - e.g. the app was started manually from the repo
    # root and its db landed there - the backup found nothing and
    # the rmtree wiped the folder with everything in it)
    consumer = os.path.join(root, "consumer")
    old_consumer = os.path.join(backup, "consumer")
    os.makedirs(backup, exist_ok=True)
    if os.path.isdir(consumer):
        shutil.move(consumer, old_consumer)
    # state files dropped at the repo root by a manual start
    # (run.py creates the db in its cwd) come along too
    root_saved = []
    for name in STATE_FILES:
        src = os.path.join(root, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(backup, "root-" + name))
            root_saved.append(name)

    try:
        # 2. replace the code dirs: core/ holds no user state -
        # rmtree + copy. consumer/ was renamed aside above (its
        # state files ride inside it) - the new code copies in
        # fresh and the state moves back after
        if os.path.isdir(os.path.join(staging, "core")):
            shutil.rmtree(os.path.join(root, "core"),
                          ignore_errors=True)
            shutil.copytree(os.path.join(staging, "core"),
                            os.path.join(root, "core"))
        if os.path.isdir(os.path.join(staging, "consumer")):
            shutil.copytree(os.path.join(staging, "consumer"),
                            os.path.join(root, "consumer"))
        for name in CODE_FILES:
            src = os.path.join(staging, name)
            if os.path.exists(src):
                dst = os.path.join(root, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
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
        # put the old consumer dir back before giving up - the
        # data must survive (the swap may have died before the
        # new code was fully in place)
        if os.path.isdir(old_consumer) and not os.path.exists(
            os.path.join(root, "consumer", "run.py")
        ) and not os.path.isdir(
            os.path.join(root, "consumer", "trading")
        ):
            shutil.rmtree(consumer, ignore_errors=True)
            shutil.move(old_consumer, consumer)
        raise

    # 3. restore the state files from the old consumer dir: the
    # new code dirs are in place - everything the user owns
    # (config, tokens, db, exit marker) moves back in; files at
    # the repo root from a manual start come home too
    old_dir = os.path.join(backup, "consumer")
    if os.path.isdir(old_dir):
        for name in os.listdir(old_dir):
            src = os.path.join(old_dir, name)
            dst = os.path.join(consumer, name)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    for name in root_saved:
        src = os.path.join(backup, "root-" + name)
        dst = os.path.join(consumer, name)
        if not os.path.exists(dst):
            shutil.move(src, dst)
    shutil.rmtree(backup, ignore_errors=True)

    # 4. record + clear the staging area
    commit = str(pending.get("commit") or "?")
    try:
        from core.ops.updater import atomic_write_json

        atomic_write_json(
            os.path.join(root, UPDATE_RECORD),
            {"how": "release", "commit": commit, "ts": time.time()},
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