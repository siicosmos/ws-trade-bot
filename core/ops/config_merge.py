"""Merge new config keys into the live config files.

When a release adds config knobs, the user's live config does
not have them (the loader falls back to defaults silently) - the
knobs are invisible and untunable. At startup the roles merge
the example's missing keys into their live config: ADD-ONLY,
with the example's comments, so the file documents itself and
the user's values are never touched.
"""

import os
import re
import time

_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][\w-]*\s*):")


def _split_sections(text):
    """Split into (preamble, [(section_key, [lines])]). A section
    starts at a column-0 key line; comments/blanks before the
    first key are the preamble."""
    preamble, sections, current = [], [], None
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if m and not line.startswith((" ", "\t")):
            current = (m.group(2), [line])
            sections.append(current)
            continue
        if current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    return preamble, sections


def _split_blocks(lines):
    """Split a section's lines into [(pending_comments, block)]
    where a block is a key line + its continuations (deeper
    indents, trailing comments). A full comment at the key's own
    indent leads the NEXT key."""
    blocks, pending, current, base = [], [], None, None
    for line in lines:
        m = _KEY_RE.match(line)
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        is_key = bool(m)
        if is_key and base is None:
            base = indent
        if is_key and base is not None and indent <= base:
            current = [line]
            blocks.append((pending, current))
            pending = []
            continue
        if current is not None and (
            not stripped or indent > base
            or (stripped.startswith("#") and indent > base)
        ):
            current.append(line)
        else:
            pending.append(line)
    return blocks


def _block_key(block):
    m = _KEY_RE.match(block[0]) if block else None
    return m.group(2).strip() if m else None


def _atomic_write_text(path, text):
    import tempfile

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    os.unlink(tmp)
                    raise
                time.sleep(0.2 * (attempt + 1))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_new_config_keys(live_path: str, example_path: str):
    """Add the example's missing keys to the live config (their
    section, or a new section at the end), with the example's
    comments. Returns the added key names ([] when nothing
    changed)."""
    if not os.path.exists(live_path) or not os.path.exists(
        example_path
    ):
        return []
    with open(live_path, encoding="utf-8") as f:
        live_text = f.read()
    with open(example_path, encoding="utf-8") as f:
        example_text = f.read()

    _, live_sections = _split_sections(live_text)
    live_by_key = {k: lines for k, lines in live_sections}
    live_keys_by_section = {}
    for key, lines in live_sections:
        keys = set()
        for line in lines[1:]:
            m = _KEY_RE.match(line)
            if m and (len(line) - len(line.lstrip())) > 0:
                keys.add(m.group(2).strip())
        live_keys_by_section[key] = keys

    _, example_sections = _split_sections(example_text)
    added = []
    merged_text = live_text.rstrip("\n")

    for ex_key, ex_lines in example_sections:
        ex_blocks = _split_blocks(ex_lines[1:])
        if ex_key not in live_by_key:
            # the whole section is new - append it verbatim
            section_text = "\n".join(ex_lines).rstrip("\n")
            merged_text += "\n\n" + section_text
            for pending, block in ex_blocks:
                name = _block_key(block)
                if name:
                    added.append(f"{ex_key}.{name}")
            continue
        live_keys = live_keys_by_section.get(ex_key, set())
        missing = [
            (pending, block) for pending, block in ex_blocks
            if _block_key(block) and _block_key(block) not in live_keys
        ]
        if not missing:
            continue
        live_lines = live_by_key[ex_key]
        last_content = len(live_lines)
        while last_content > 1 and not live_lines[last_content - 1].strip():
            last_content -= 1
        new_lines = live_lines[:last_content]
        for pending, block in missing:
            added.append(f"{ex_key}.{_block_key(block)}")
            new_lines.append("")   # a blank before each new key
            new_lines.extend(pending + block)
        new_lines.append("")   # keep the section's trailing blank
        old_text = "\n".join(live_lines).rstrip("\n")
        new_text = "\n".join(new_lines).rstrip("\n")
        if old_text in merged_text:
            merged_text = merged_text.replace(old_text, new_text, 1)
        else:
            merged_text = merged_text.replace(
                "\n".join(live_lines), "\n".join(new_lines), 1
            )

    if not added:
        return []
    _atomic_write_text(live_path, merged_text.rstrip("\n") + "\n")
    return added