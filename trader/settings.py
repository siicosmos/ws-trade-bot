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
    "lotto_gain_budget_pct": ("float", 0, 100),
    "max_consecutive_losses": ("int", 0, 100),
    "max_daily_loss_pct": ("float", 0, 100),
    "min_dte_days": ("int", 0, 365),
    "max_trades_per_day": ("int", 0, 1000),
    "cooldown_seconds": ("int", 0, 86400),
    "dedupe_window_minutes": ("int", 0, 1440),
    "limit_offset_pct": ("float", 0, 5),
    "history_retention_days": ("int", 0, 3650),
}
EDITABLE_ENUMS = {"order_type": ("market", "limit")}
EDITABLE_BOOLS = (
    "sell_only_if_held", "back_to_entry_enabled", "trading_paused",
)
EDITABLE_LISTS = ("ticker_whitelist", "skip_underlyings")
EDITABLE_READER = {
    "channel_marker": ("str", 0, 100),
    "poll_interval": ("float", 0.2, 10),
    "max_items": ("int", 5, 200),
    "discord_reopen_seconds": ("int", 5, 600),
    "discord_restart_seconds": ("int", 30, 3600),
}
EDITABLE_READER_LISTS = ("channels",)
EDITABLE_READER_MAP = ("channel_servers",)
EDITABLE_ACCOUNT_NUMERIC = {
    "risk_per_trade_pct": (0.0, 100.0),
    "max_contracts_per_trade": (0, 1000),
    "paper_value": (0.0, 100000000.0),
}


def get_settings(cfg) -> dict:
    trading = {k: getattr(cfg.trading, k) for k in EDITABLE_SCALARS}
    for k in EDITABLE_ENUMS:
        trading[k] = getattr(cfg.trading, k)
    for k in EDITABLE_BOOLS:
        trading[k] = getattr(cfg.trading, k)
    for k in EDITABLE_LISTS:
        trading[k] = getattr(cfg.trading, k)
    trading["size_tiers"] = cfg.trading.size_tiers
    trading["stock_size_tiers"] = dict(
        getattr(cfg.trading, "stock_size_tiers", {}) or {}
    )
    reader = {k: getattr(cfg.reader, k) for k in EDITABLE_READER}
    reader["auto_scroll"] = cfg.reader.auto_scroll
    for k in EDITABLE_READER_LISTS:
        reader[k] = getattr(cfg.reader, k)
    reader["channel_servers"] = dict(
        getattr(cfg.reader, "channel_servers", {}) or {}
    )
    reader["auto_switch"] = bool(
        getattr(cfg.reader, "auto_switch_channel", True)
    )
    reader["discord_reopen_seconds"] = int(
        getattr(cfg.reader, "discord_reopen_seconds", 15)
    )
    reader["discord_restart_seconds"] = int(
        getattr(cfg.reader, "discord_restart_seconds", 90)
    )
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
            "enabled": cfg.quotes.enabled,
            "provider": cfg.quotes.provider,
            "moomoo_host": cfg.quotes.moomoo_host,
            "moomoo_port": cfg.quotes.moomoo_port,
        },
        "paper": {
            "enabled": bool(
                getattr(getattr(cfg, "paper", None), "enabled", False)
            ),
            "mirror": bool(
                getattr(getattr(cfg, "paper", None), "mirror", True)
            ),
            "mirror_interval_seconds": int(
                getattr(
                    getattr(cfg, "paper", None),
                    "mirror_interval_seconds", 60,
                )
            ),
        },
        "wealthsimple": {
            "positions_refresh_seconds": (
                cfg.wealthsimple.positions_refresh_seconds
            ),
            "values_refresh_seconds": (
                cfg.wealthsimple.values_refresh_seconds
            ),
            "stock_margin_rate": cfg.wealthsimple.stock_margin_rate,
        },
        "discord": {
            "notify": bool(
                getattr(
                    getattr(cfg, "discord", None), "notify", True
                )
            ),
            "webhook_url": cfg.discord.webhook_url,
            "reader_log_webhook_url": cfg.discord.reader_log_webhook_url,
            "pipeline_log_webhook_url": (
                cfg.discord.pipeline_log_webhook_url
            ),
            "update_webhook_url": cfg.discord.update_webhook_url,
            "raw_alert_webhook_url": getattr(
                cfg.discord, "raw_alert_webhook_url", ""
            ),
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

    for key, choices in EDITABLE_ENUMS.items():
        if key not in trading_payload:
            continue
        value = str(trading_payload[key]).strip().lower()
        if value not in choices:
            errors.append(
                f"trading.{key}: must be one of "
                + ", ".join(choices)
            )
            continue
        setattr(cfg.trading, key, value)
        applied[f"trading.{key}"] = value

    for key in EDITABLE_BOOLS:
        if key not in trading_payload:
            continue
        value = bool(trading_payload[key])
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

    raw_stock = trading_payload.get("stock_size_tiers")
    if raw_stock is not None:
        # a mapping or "tier=dollars, tier=dollars"
        if isinstance(raw_stock, dict):
            pairs = list(raw_stock.items())
        elif isinstance(raw_stock, str):
            pairs = []
            for part in raw_stock.split(","):
                part = part.strip()
                if not part:
                    continue
                name, sep, dollars = part.partition("=")
                if not sep:
                    errors.append(
                        f"trading.stock_size_tiers: {part!r} "
                        f"is not tier=dollars"
                    )
                    continue
                pairs.append((name, dollars))
        else:
            pairs = None
        if pairs is None:
            errors.append(
                "trading.stock_size_tiers: expected a mapping or a "
                "comma-separated tier=dollars list"
            )
        else:
            tiers_out = {}
            for k, v in pairs:
                tname = str(k).strip().lower()[:32]
                try:
                    dollars = float(v)
                except (TypeError, ValueError):
                    errors.append(
                        f"trading.stock_size_tiers.{tname}: "
                        f"not a number"
                    )
                    continue
                if not (0 <= dollars <= 100):
                    errors.append(
                        f"trading.stock_size_tiers.{tname}: "
                        f"must be a percent between 0 and 100"
                    )
                    continue
                tiers_out[tname] = dollars
            cfg.trading.stock_size_tiers = tiers_out
            applied["trading.stock_size_tiers"] = tiers_out

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
            # optional per-size stop loss - absent means the
            # global stop applies
            if spec.get("stop_loss_pct") not in (None, ""):
                try:
                    tier["stop_loss_pct"] = float(spec["stop_loss_pct"])
                except (TypeError, ValueError):
                    errors.append(
                        f"trading.size_tiers.{name}: bad stop_loss_pct"
                    )
                    continue
                if not (0 < tier["stop_loss_pct"] <= 100):
                    errors.append(
                        f"trading.size_tiers.{name}.stop_loss_pct "
                        f"must be 0-100"
                    )
                    continue
            if "back_to_entry" in spec:
                tier["back_to_entry"] = bool(spec["back_to_entry"])
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

    for key in EDITABLE_READER_LISTS:
        if key not in reader_payload:
            continue
        raw = reader_payload[key]
        if not isinstance(raw, list):
            errors.append(f"reader.{key}: expected a list")
            continue
        value = sorted(
            {
                str(c).strip().lower()[:100]
                for c in raw
                if str(c).strip()
            }
        )
        setattr(cfg.reader, key, value)
        applied[f"reader.{key}"] = value

    raw_servers = reader_payload.get("channel_servers")
    if raw_servers is not None:
        # accepts a mapping or "channel=server, channel=server"
        if isinstance(raw_servers, dict):
            pairs = list(raw_servers.items())
        elif isinstance(raw_servers, str):
            pairs = []
            for part in raw_servers.split(","):
                part = part.strip()
                if not part:
                    continue
                name, sep, server = part.partition("=")
                if not sep:
                    errors.append(
                        f"reader.channel_servers: {part!r} "
                        f"is not channel=server"
                    )
                    continue
                pairs.append((name, server))
        else:
            pairs = None
        if pairs is None:
            errors.append(
                "reader.channel_servers: expected a mapping or a "
                "comma-separated channel=server list"
            )
        else:
            servers = {}
            for k, v in pairs:
                cname = str(k).strip().lower()[:100]
                sname = str(v).strip()[:100]
                if not cname or not sname:
                    errors.append(
                        "reader.channel_servers: bad pair"
                    )
                    continue
                servers[cname] = sname
            cfg.reader.channel_servers = servers
            applied["reader.channel_servers"] = servers

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

    reader_payload = payload.get("reader") or {}
    if "auto_scroll" in reader_payload:
        cfg.reader.auto_scroll = bool(reader_payload["auto_scroll"])
        applied["reader.auto_scroll"] = cfg.reader.auto_scroll

    discord_notify = (payload.get("discord") or {}).get("notify")
    if discord_notify is not None:
        cfg.discord.notify = bool(discord_notify)
        applied["discord.notify"] = cfg.discord.notify

    paper_payload = payload.get("paper") or {}
    paper_cfg = getattr(cfg, "paper", None)
    if paper_cfg is not None:
        if "enabled" in paper_payload:
            paper_cfg.enabled = bool(paper_payload["enabled"])
            applied["paper.enabled"] = paper_cfg.enabled
        if "mirror" in paper_payload:
            paper_cfg.mirror = bool(paper_payload["mirror"])
            applied["paper.mirror"] = paper_cfg.mirror
        if "mirror_interval_seconds" in paper_payload:
            try:
                interval = int(
                    paper_payload["mirror_interval_seconds"]
                )
            except (TypeError, ValueError):
                interval = 0
            if not (15 <= interval <= 86400):
                errors.append(
                    "paper.mirror_interval_seconds: must be 15-86400"
                )
            else:
                paper_cfg.mirror_interval_seconds = interval
                applied[
                    "paper.mirror_interval_seconds"
                ] = interval

    quotes_payload = payload.get("quotes") or {}
    if "enabled" in quotes_payload:
        cfg.quotes.enabled = bool(quotes_payload["enabled"])
        applied["quotes.enabled"] = cfg.quotes.enabled
    if "moomoo_host" in quotes_payload:
        host = str(quotes_payload["moomoo_host"]).strip()[:100]
        cfg.quotes.moomoo_host = host
        applied["quotes.moomoo_host"] = host
    if "moomoo_port" in quotes_payload:
        try:
            port = int(quotes_payload["moomoo_port"])
        except (TypeError, ValueError):
            port = 0
        if not (1 <= port <= 65535):
            errors.append("quotes.moomoo_port: must be 1-65535")
        else:
            cfg.quotes.moomoo_port = port
            applied["quotes.moomoo_port"] = port
    if "provider" in quotes_payload:
        provider = str(quotes_payload["provider"]).strip().lower()
        if provider not in ("ws", "moomoo"):
            errors.append("quotes.provider: must be ws or moomoo")
        else:
            cfg.quotes.provider = provider
            applied["quotes.provider"] = provider

    discord_payload = payload.get("discord") or {}
    for field in (
        "webhook_url",
        "reader_log_webhook_url",
        "pipeline_log_webhook_url",
        "update_webhook_url",
        "raw_alert_webhook_url",
    ):
        if field in discord_payload:
            url = str(discord_payload[field]).strip()
            if url and not url.startswith("https://"):
                errors.append(
                    f"discord.{field}: must be an https URL"
                )
            else:
                setattr(cfg.discord, field, url)
                applied[f"discord.{field}"] = url

    ws_payload = payload.get("wealthsimple") or {}
    if "stock_margin_rate" in ws_payload:
        try:
            rate = float(ws_payload["stock_margin_rate"])
        except (TypeError, ValueError):
            rate = -1.0
        if not (0.0 <= rate <= 1.0):
            errors.append(
                "wealthsimple.stock_margin_rate: must be 0-1"
            )
        else:
            cfg.wealthsimple.stock_margin_rate = rate
            applied["wealthsimple.stock_margin_rate"] = rate
    for field in ("positions_refresh_seconds", "values_refresh_seconds"):
        if field in ws_payload:
            try:
                value = int(ws_payload[field])
            except (TypeError, ValueError):
                value = -1
            if not (10 <= value <= 86400):
                errors.append(
                    f"wealthsimple.{field}: must be 10-86400"
                )
            else:
                setattr(cfg.wealthsimple, field, value)
                applied[f"wealthsimple.{field}"] = value

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
    for key in EDITABLE_ENUMS:
        trading[key] = getattr(cfg.trading, key)
    for key in EDITABLE_BOOLS:
        trading[key] = getattr(cfg.trading, key)
    for key in EDITABLE_LISTS:
        trading[key] = getattr(cfg.trading, key)
    trading["size_tiers"] = cfg.trading.size_tiers
    if getattr(cfg.trading, "stock_size_tiers", None):
        trading["stock_size_tiers"] = cfg.trading.stock_size_tiers
    trading["stock_size_tiers"] = dict(
        getattr(cfg.trading, "stock_size_tiers", {}) or {}
    )

    dc = raw.setdefault("discord", {})
    for field in (
        "webhook_url",
        "reader_log_webhook_url",
        "pipeline_log_webhook_url",
        "update_webhook_url",
        "raw_alert_webhook_url",
    ):
        if field in raw.get("discord", {}) or getattr(
            cfg.discord, field, ""
        ):
            dc[field] = getattr(cfg.discord, field)

    ws = raw.setdefault("wealthsimple", {})
    for field in ("positions_refresh_seconds", "values_refresh_seconds"):
        ws[field] = getattr(cfg.wealthsimple, field)
    if "stock_margin_rate" in ws or getattr(
        cfg.wealthsimple, "stock_margin_rate", None
    ) not in (None, 0.30):
        ws["stock_margin_rate"] = cfg.wealthsimple.stock_margin_rate

    au = raw.setdefault("auto_update", {})
    au["enabled"] = cfg.auto_update.enabled
    au["interval_seconds"] = cfg.auto_update.interval_seconds

    quotes = raw.setdefault("quotes", {})
    quotes["enabled"] = cfg.quotes.enabled
    quotes["provider"] = cfg.quotes.provider
    quotes["moomoo_host"] = getattr(
        cfg.quotes, "moomoo_host", ""
    )
    quotes["moomoo_port"] = getattr(
        cfg.quotes, "moomoo_port", 11111
    )

    paper_cfg = getattr(cfg, "paper", None)
    if paper_cfg is not None:
        paper = raw.setdefault("paper", {})
        paper["enabled"] = paper_cfg.enabled
        paper["mirror"] = paper_cfg.mirror
        if getattr(paper_cfg, "mirror", False):
            paper["mirror_interval_seconds"] = (
                paper_cfg.mirror_interval_seconds
            )
    discord_cfg = getattr(cfg, "discord", None)
    if discord_cfg is not None and "notify" in (
        raw.get("discord") or {}
    ):
        raw["discord"]["notify"] = discord_cfg.notify
    elif discord_cfg is not None and getattr(
        discord_cfg, "notify", True
    ) is False:
        raw.setdefault("discord", {})["notify"] = False

    reader = raw.setdefault("reader", {})
    for key in EDITABLE_READER:
        reader[key] = getattr(cfg.reader, key)
    for key in EDITABLE_READER_LISTS:
        reader[key] = getattr(cfg.reader, key)
    reader["auto_scroll"] = cfg.reader.auto_scroll
    if getattr(cfg.reader, "channel_servers", None):
        reader["channel_servers"] = cfg.reader.channel_servers

    dc = raw.setdefault("discord", {})
    for field in (
        "webhook_url",
        "reader_log_webhook_url",
        "pipeline_log_webhook_url",
        "update_webhook_url",
        "raw_alert_webhook_url",
    ):
        if field in raw.get("discord", {}) or getattr(
            cfg.discord, field, ""
        ):
            dc[field] = getattr(cfg.discord, field)

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
        text = yaml.safe_dump(
            raw, default_flow_style=False, sort_keys=False,
            allow_unicode=True, width=4096,
        )
        out = []
        for line in text.split("\n"):
            if (
                out
                and line
                and not line[0].isspace()
                and not line.startswith("- ")
                and line not in ("---", "...")
                and out[-1] != ""
            ):
                out.append("")
            out.append(line)
        f.write("\n".join(out))
    os.replace(tmp, config_path)
