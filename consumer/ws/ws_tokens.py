import os

_TOKEN_PATH = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "config", "ws_tokens.env")
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
    with open(target, "w", encoding="utf-8") as f:
        f.write(f"export WS_ACCESS_TOKEN={access}\n")
        f.write(f"export WS_REFRESH_TOKEN={refresh}\n")
    os.chmod(target, 0o600)
    _last_written = current
    return True


def load_env_tokens(path=None) -> bool:
    target = path or _TOKEN_PATH
    if not os.path.exists(target):
        return False
    loaded = False
    with open(target, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.replace("export", "").strip()
            value = value.strip()
            if key not in ("WS_ACCESS_TOKEN", "WS_REFRESH_TOKEN"):
                continue
            if not os.environ.get(key) and value:
                os.environ[key] = value
                loaded = True
    return loaded
