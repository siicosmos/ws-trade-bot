import requests

from .parser import format_scale


def notify_discord(webhook_url: str, title: str, fields: dict, ok: bool = True,
                  color=None):
    if not webhook_url:
        return
    if color is None:
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


def notify_correction(webhook_url: str, text: str):
    if not webhook_url:
        return
    notify_discord(
        webhook_url,
        "CORRECTION from admin - check channel",
        {"message": (text or "")[:300]},
        color=15105570,
    )


def notify_alert(webhook_url: str, alert, sizing=None, correction=False):
    if not webhook_url:
        return

    if alert.kind == "option":
        title = f"{alert.action} {alert.underlying} {alert.strike:g}{alert.right}"
        if alert.expiry:
            title += f" exp {alert.expiry}"
        if alert.premium:
            title += f" @ ${alert.premium:g}"
        if alert.gain_pct is not None:
            title += f" {alert.gain_pct:+g}%"
        fields = {
            "type": alert.action,
            "underlying": alert.underlying,
            "strike": alert.strike,
            "right": "CALL" if alert.right == "C" else "PUT",
            "expiry": alert.expiry,
            "premium": alert.premium,
            "scale": format_scale(alert.scale),
            "size": alert.size,
        }
    else:
        title = f"{alert.action} {alert.ticker}"
        if alert.entry:
            title += f" @ ${alert.entry:g}"
        if alert.gain_pct is not None:
            title += f" {alert.gain_pct:+g}%"
        fields = {
            "type": alert.action,
            "ticker": alert.ticker,
            "price": alert.entry or alert.premium,
        }

    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    if correction:
        fields["note"] = "ADMIN CORRECTION - may supersede the previous alert"

    for row in sizing or []:
        label = row.get("label", "?")
        value = row.get("value")
        contracts = row.get("contracts")
        if value is not None and contracts is not None:
            warnings = row.get("warnings") or []
            if contracts == 0:
                if warnings:
                    line = "Skipping — " + "; ".join(warnings)
                else:
                    line = "Skipping — budget too small"
            else:
                risk = row.get("actual_risk")
                if risk is None or risk == 0:
                    risk = row.get("budget") or 0
                noun = "contract" if contracts == 1 else "contracts"
                line = (
                    f"Buy {contracts} {noun} — ${risk:,.0f} risk "
                    f"({row.get('risk_pct')}% budget - "
                    f"${row.get('budget') or 0:,.2f})"
                )
                if warnings:
                    line += " ⚠ " + "; ".join(warnings)
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
