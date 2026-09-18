import os
import tempfile

import yaml

EDITABLE_SCALARS = {
    "risk_per_trade_pct": ("float", 0, 100),
    "max_contracts_per_trade": ("int", 0, 1000),
    "max_open_risk_pct": ("float", 0, 100),
    "stop_loss_pct": ("float", 0, 100),
    "trailing_stop_pct": ("float", 0, 100),
    "stop_check_seconds": ("int", 5, 3600),
    "max_consecutive_losses": ("int", 0, 100),
    "min_dte_days": ("int", 0, 365),
    "max_trades_per_day": ("int", 0, 1000),
    "cooldown_seconds": ("int", 0, 86400),
    "dedupe_window_minutes": ("int", 0, 1440),
}
EDITABLE_LISTS = ("ticker_whitelist", "skip_underlyings")
EDITABLE_READER = {
    "channel_marker": ("str", 0, 100),
    "poll_interval": ("float", 0.2, 10),
    "max_items": ("int", 5, 200),
}
EDITABLE_ACCOUNT_NUMERIC = {
    "risk_per_trade_pct": (0.0, 100.0),
    "max_contracts_per_trade": (0, 1000),
    "paper_value": (0.0, 100000000.0),
}


def get_settings(cfg) -> dict:
    trading = {k: getattr(cfg.trading, k) for k in EDITABLE_SCALARS}
    for k in EDITABLE_LISTS:
        trading[k] = getattr(cfg.trading, k)
    trading["size_tiers"] = cfg.trading.size_tiers
    reader = {k: getattr(cfg.reader, k) for k in EDITABLE_READER}
    accounts = [
        {
            "account_id": a.account_id,
            "label": a.label,
            "risk_per_trade_pct": a.risk_per_trade_pct,
            "max_contracts_per_trade": a.max_contracts_per_trade,
            "paper_value": a.paper_value,
            "enabled": a.enabled,
        }
        for a in cfg.wealthsimple.accounts
    ]
    return {
        "trading": trading,
        "reader": reader,
        "accounts": accounts,
        "auto_update": {
            "enabled": cfg.auto_update.enabled,
            "interval_seconds": cfg.auto_update.interval_seconds,
        },
        "quotes": {
            "provider": cfg.quotes.provider,
        },
    }


def apply_settings(cfg, payload: dict, config_path=None) -> tuple:
    errors = []
    applied = {}

    trading_payload = payload.get("trading") or {}
    for key, (kind, lo, hi) in EDITABLE_SCALARS.items():
        if key not in trading_payload:
            continue
        raw = trading_payload[key]
        try:
            value = float(raw) if kind == "float" else int(float(raw))
        except (TypeError, ValueError):
            errors.append(f"trading.{key}: not a number")
            continue
        if value < lo or value > hi:
            errors.append(f"trading.{key}: must be between {lo} and {hi}")
            continue
        setattr(cfg.trading, key, value)
        applied[f"trading.{key}"] = value

    for key in EDITABLE_LISTS:
        if key not in trading_payload:
            continue
        raw = trading_payload[key]
        if isinstance(raw, str):
            items = [x.strip().upper() for x in raw.split(",") if x.strip()]
        elif isinstance(raw, list):
            items = [str(x).strip().upper() for x in raw if str(x).strip()]
        else:
            errors.append(f"trading.{key}: expected list or comma string")
            continue
        setattr(cfg.trading, key, items)
        applied[f"trading.{key}"] = items

    tiers = trading_payload.get("size_tiers")
    if isinstance(tiers, dict):
        for name, spec in tiers.items():
            if not isinstance(spec, dict):
                errors.append(f"trading.size_tiers.{name}: expected mapping")
                continue
            try:
                tier = {
                    "risk_pct_max": float(spec.get("risk_pct_max", 5)),
                    "contracts_min": int(spec.get("contracts_min", 1)),
                    "contracts_max": int(spec.get("contracts_max", 10)),
                }
            except (TypeError, ValueError):
                errors.append(f"trading.size_tiers.{name}: bad numbers")
                continue
            if not (0 <= tier["risk_pct_max"] <= 100):
                errors.append(
                    f"trading.size_tiers.{name}.risk_pct_max must be 0-100"
                )
                continue
            if tier["contracts_min"] < 0 or tier["contracts_max"] < tier["contracts_min"]:
                errors.append(
                    f"trading.size_tiers.{name}: invalid contract range"
                )
                continue
            cfg.trading.size_tiers[str(name).lower()] = tier
            applied[f"trading.size_tiers.{name}"] = tier

    reader_payload = payload.get("reader") or {}
    for key, (kind, lo, hi) in EDITABLE_READER.items():
        if key not in reader_payload:
            continue
        raw = reader_payload[key]
        if kind == "str":
            value = str(raw)
            if len(value) > hi:
                errors.append(f"reader.{key}: too long")
                continue
        else:
            try:
                value = float(raw) if kind == "float" else int(float(raw))
            except (TypeError, ValueError):
                errors.append(f"reader.{key}: not a number")
                continue
            if value < lo or value > hi:
                errors.append(f"reader.{key}: must be between {lo} and {hi}")
                continue
        setattr(cfg.reader, key, value)
        applied[f"reader.{key}"] = value

    accounts_payload = payload.get("accounts")
    if isinstance(accounts_payload, list):
        existing = {a.label: a for a in cfg.wealthsimple.accounts}
        for entry in accounts_payload:
            if not isinstance(entry, dict):
                errors.append("accounts: expected mappings")
                continue
            label = str(entry.get("label") or "").strip()
            if not label or label not in existing:
                errors.append(f"accounts: unknown account label {label!r}")
                continue
            acct = existing[label]

            new_id = entry.get("account_id")
            if new_id is not None:
                new_id = str(new_id).strip()[:100]
                if new_id != acct.account_id:
                    acct.account_id = new_id
                    applied[f"accounts.{label}.account_id"] = new_id

            for field, (lo, hi) in EDITABLE_ACCOUNT_NUMERIC.items():
                if field not in entry:
                    continue
                raw = entry[field]
                if raw is None or raw == "":
                    if getattr(acct, field) is not None:
                        setattr(acct, field, None)
                        applied[f"accounts.{label}.{field}"] = None
                    continue
                try:
                    value = float(raw) if lo != int(lo) or isinstance(lo, float) else int(float(raw))
                except (TypeError, ValueError):
                    errors.append(f"accounts.{label}.{field}: not a number")
                    continue
                if value < lo or value > hi:
                    errors.append(
                        f"accounts.{label}.{field}: must be between "
                        f"{lo:g} and {hi:g}"
                    )
                    continue
                if field == "max_contracts_per_trade":
                    value = int(value)
                setattr(acct, field, value)
                applied[f"accounts.{label}.{field}"] = value

            if "enabled" in entry:
                acct.enabled = bool(entry["enabled"])
                applied[f"accounts.{label}.enabled"] = acct.enabled

    au = payload.get("auto_update") or {}
    if "enabled" in au:
        cfg.auto_update.enabled = bool(au["enabled"])
        applied["auto_update.enabled"] = cfg.auto_update.enabled
    if "interval_seconds" in au:
        try:
            value = int(au["interval_seconds"])
        except (TypeError, ValueError):
            value = -1
        if not (30 <= value <= 86400):
            errors.append("auto_update.interval_seconds: must be 30-86400")
        else:
            cfg.auto_update.interval_seconds = value
            applied["auto_update.interval_seconds"] = value

    if not errors and config_path and applied:
        try:
            _persist(cfg, config_path)
        except OSError as e:
            errors.append(f"could not write config: {e}")

    return applied, errors


def _persist(cfg, config_path):
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    trading = raw.setdefault("trading", {})
    for key in EDITABLE_SCALARS:
        trading[key] = getattr(cfg.trading, key)
    for key in EDITABLE_LISTS:
        trading[key] = getattr(cfg.trading, key)
    trading["size_tiers"] = cfg.trading.size_tiers

    au = raw.setdefault("auto_update", {})
    au["enabled"] = cfg.auto_update.enabled
    au["interval_seconds"] = cfg.auto_update.interval_seconds

    quotes = raw.setdefault("quotes", {})
    quotes["provider"] = cfg.quotes.provider

    reader = raw.setdefault("reader", {})
    for key in EDITABLE_READER:
        reader[key] = getattr(cfg.reader, key)

    ws = raw.setdefault("wealthsimple", {})
    ws["accounts"] = [
        {
            "account_id": a.account_id,
            "label": a.label,
            "risk_per_trade_pct": a.risk_per_trade_pct,
            "max_contracts_per_trade": a.max_contracts_per_trade,
            "paper_value": a.paper_value,
            "enabled": a.enabled,
        }
        for a in cfg.wealthsimple.accounts
    ]

    directory = os.path.dirname(os.path.abspath(config_path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".yaml.tmp")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False)
    os.replace(tmp, config_path)
