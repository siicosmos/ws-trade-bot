import getpass
import os
import stat
import sys

_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _root not in sys.path:
    sys.path.append(_root)

from wealthsimple_python import WealthsimpleV2


def main():
    username = input("Wealthsimple email: ").strip()
    password = getpass.getpass("Password: ")
    otp = input("2FA code (leave blank if disabled): ").strip() or None

    ws = WealthsimpleV2(username=username, password=password, otp=otp)

    access = os.environ.get("WS_ACCESS_TOKEN") or getattr(ws, "access_token", None)
    refresh = os.environ.get("WS_REFRESH_TOKEN") or getattr(ws, "refresh_token", None)

    # save where the consumer app reads: config/ws_tokens.env
    # (the keyring stays the primary store - this is the headless
    # fallback)
    token_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "config", "ws_tokens.env",
    )
    if access and refresh:
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(f"export WS_ACCESS_TOKEN={access}\n")
            f.write(f"export WS_REFRESH_TOKEN={refresh}\n")
        os.chmod(token_path, 0o600)
        print(f"tokens saved to {os.path.abspath(token_path)} (gitignored)")
    else:
        print("WARNING: could not capture tokens - stored in keyring only")

    print("\nyour accounts (add them in the dashboard settings or")
    print("consumer.config.yaml wealthsimple.accounts):")
    for a in ws.get_accounts():
        print(f"  {a['id']}  {a.get('nickname', '')}  {a.get('accountType', '')}")


if __name__ == "__main__":
    main()
