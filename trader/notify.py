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


def notify_alert(webhook_url: str, alert, sizing=None):
    if not webhook_url:
        return

    if alert.kind == "option":
        title = f"{alert.action} {alert.underlying} {alert.strike:g}{alert.right}"
        if alert.expiry:
            title += f" exp {alert.expiry}"
        if alert.premium:
            title += f" @ ${alert.premium:g}"
        fields = {
            "type": alert.action,
            "underlying": alert.underlying,
            "strike": alert.strike,
            "right": "CALL" if alert.right == "C" else "PUT",
            "expiry": alert.expiry,
            "premium": alert.premium,
            "scale": alert.scale,
            "size": alert.size,
        }
    else:
        title = f"{alert.action} {alert.ticker}"
        if alert.entry:
            title += f" @ ${alert.entry:g}"
        fields = {
            "type": alert.action,
            "ticker": alert.ticker,
            "price": alert.entry or alert.premium,
        }

    fields = {k: v for k, v in fields.items() if v not in (None, "")}

    for row in sizing or []:
        label = row.get("label", "?")
        value = row.get("value")
        contracts = row.get("contracts")
        if value is not None and contracts is not None:
            line = (
                f"{contracts} contracts "
                f"(${row.get('budget') or 0:,.0f} risk @ {row.get('risk_pct')}%)"
            )
            warnings = row.get("warnings") or []
            if warnings:
                line += " — WARNING: " + "; ".join(warnings)
            fields[f"{label} sizing"] = line
        elif value is not None:
            fields[f"{label} sizing"] = (
                f"${value:,.0f} (no price to size against)"
            )
        else:
            fields[f"{label} sizing"] = "value unavailable"

    if alert.raw:
        fields["message"] = alert.raw[:200]

    notify_discord(webhook_url, title, fields, ok=alert.action == "BUY")
