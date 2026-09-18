import os

_TOKEN_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ws_tokens.env")
)
_last_written = None


def persist_env_tokens(path=None) -> bool:
    global _last_written
    target = path or _TOKEN_PATH
    access = os.environ.get("WS_ACCESS_TOKEN")
    refresh = os.environ.get("WS_REFRESH_TOKEN")
    if not access or not refresh:
        return False
    current = (access, refresh)
    if current == _last_written:
        return False
    with open(target, "w") as f:
        f.write(f"export WS_ACCESS_TOKEN={access}\n")
        f.write(f"export WS_REFRESH_TOKEN={refresh}\n")
    os.chmod(target, 0o600)
    _last_written = current
    return True
