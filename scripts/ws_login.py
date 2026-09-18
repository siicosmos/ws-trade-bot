import getpass
import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wealthsimple_python import WealthsimpleV2


def main():
    username = input("Wealthsimple email: ").strip()
    password = getpass.getpass("Password: ")
    otp = input("2FA code (leave blank if disabled): ").strip() or None

    ws = WealthsimpleV2(username=username, password=password, otp=otp)

    access = os.environ.get("WS_ACCESS_TOKEN") or getattr(ws, "access_token", None)
    refresh = os.environ.get("WS_REFRESH_TOKEN") or getattr(ws, "refresh_token", None)

    token_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "ws_tokens.env"
    )
    if access and refresh:
        with open(token_path, "w") as f:
            f.write(f"export WS_ACCESS_TOKEN={access}\n")
            f.write(f"export WS_REFRESH_TOKEN={refresh}\n")
        os.chmod(token_path, 0o600)
        print(f"tokens saved to {os.path.abspath(token_path)} (gitignored)")
    else:
        print("WARNING: could not capture tokens - stored in keyring only")

    print("\nyour accounts (put the one to trade in config.yaml wealthsimple.account_id):")
    for a in ws.get_accounts():
        print(f"  {a['id']}  {a.get('nickname', '')}  {a.get('accountType', '')}")


if __name__ == "__main__":
    main()
