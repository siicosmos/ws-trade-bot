"""Shared helpers for the release/update plumbing."""

import re

_SLUG_RE = re.compile(
    r"(?:git@github\.com:|github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$"
)


def repo_slug_from_url(url: str) -> str:
    """owner/repo from a git remote url - ssh
    (git@github.com:owner/repo.git), https
    (https://github.com/owner/repo.git) and bare slugs all
    normalize the same way (an https url contains ':' too, so a
    naive split(':') mangles it into //github.com/owner/repo and
    the release poll 404s forever)."""
    url = (url or "").strip()
    if url.count("/") == 1 and ":" not in url:
        return url.removesuffix(".git")
    m = _SLUG_RE.search(url)
    return m.group(1) if m else ""