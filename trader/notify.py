import requests


def notify_discord(webhook_url: str, title: str, fields: dict, ok: bool = True):
    if not webhook_url:
        return
    color = 3066993 if ok else 15158332
    embed = {
        "title": title,
        "color": color,
        "fields": [
            {"name": str(k), "value": str(v)[:1000], "inline": True}
            for k, v in fields.items()
        ],
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except requests.RequestException:
        pass
