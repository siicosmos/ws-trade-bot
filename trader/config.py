import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


@dataclass
class PipelineConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: str = ""
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class DiscordConfig:
    webhook_url: str = ""
    reader_log_webhook_url: str = ""
    pipeline_log_webhook_url: str = ""
    update_webhook_url: str = ""
    # raw alert copy-paste feed: every alert the reader delivers
    # is posted here as plain text
    raw_alert_webhook_url: str = ""
    # send parsed alerts to the webhook
    notify: bool = True



def _default_size_tiers() -> dict:
    # stop_loss_pct: per-size stop loss - wider for lotto plays
    # (they are meant to go to zero) and tighter for big sizes
    return {
        "lotto": {"risk_pct_max": 0.5, "contracts_min": 1, "contracts_max": 1,
                  "stop_loss_pct": 50.0, "back_to_entry": True},
        "micro": {"risk_pct_max": 0.5, "contracts_min": 1, "contracts_max": 1,
                  "stop_loss_pct": 50.0},
        "tiny": {"risk_pct_max": 1.0, "contracts_min": 1, "contracts_max": 1,
                 "stop_loss_pct": 40.0},
        "small": {"risk_pct_max": 2.0, "contracts_min": 1, "contracts_max": 2,
                  "stop_loss_pct": 30.0},
        "medium": {"risk_pct_max": 5.0, "contracts_min": 1, "contracts_max": 5,
                   "stop_loss_pct": 25.0},
        "large": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10,
                  "stop_loss_pct": 20.0},
        "big": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10,
                "stop_loss_pct": 20.0},
        "full": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10,
                 "stop_loss_pct": 20.0},
    }


def _default_stock_size_tiers() -> dict:
    # percent of the account value per size keyword - separate
    # from the option contract tiers
    return {
        "tiny": 2.5,
        "small": 5.0,
        "medium": 10.0,
        "large": 20.0,
        "full": 50.0,
    }


def _norm_tier(d: dict) -> dict:
    out = {
        "risk_pct_max": float(d.get("risk_pct_max", 5.0)),
        "contracts_min": int(d.get("contracts_min", 1)),
        "contracts_max": int(d.get("contracts_max", 10)),
    }
    # optional per-size overrides - kept only when configured
    if d.get("stop_loss_pct") is not None:
        out["stop_loss_pct"] = float(d["stop_loss_pct"])
    if "back_to_entry" in d:
        out["back_to_entry"] = bool(d.get("back_to_entry"))
    return out


@dataclass
class WSAccountConfig:
    account_id: str = ""
    label: str = ""
    risk_per_trade_pct: Optional[float] = None
    max_contracts_per_trade: Optional[int] = None
    paper_value: Optional[float] = None
    enabled: bool = True


@dataclass
class TradingConfig:
    mode: str = "notify"
    dry_run: bool = True
    order_type: str = "limit"
    limit_offset_pct: float = 0.5
    position_size_cad: float = 100.0
    # per-tier percent-of-account-value budget for stock buys
    # (separate from the option contract tiers); unsized stock
    # alerts default to the medium tier
    stock_size_tiers: dict = field(default_factory=_default_stock_size_tiers)
    risk_per_trade_pct: float = 5.0
    size_tiers: dict = field(default_factory=_default_size_tiers)
    stop_loss_pct: float = 25.0
    trailing_stop_pct: float = 0.0
    stop_check_seconds: int = 30
    # "hero or zero" / "profits only" alerts spend at most this
    # fraction of today's realized sell gains
    max_contracts_per_trade: int = 10
    max_open_risk_pct: float = 30.0
    stop_loss_pct: float = 25.0
    trailing_stop_pct: float = 0.0
    stop_check_seconds: int = 30
    # "hero or zero" / "profits only" alerts spend at most this
    # fraction of today's realized sell gains
    lotto_gain_budget_pct: float = 75.0
    # sell a 0dte option back to its entry when the day's gain
    # evaporates (per-tier override via size_tiers)
    back_to_entry_enabled: bool = True
    max_consecutive_losses: int = 2
    min_dte_days: int = 0
    paper_account_value: float = 10000.0
    skip_underlyings: List[str] = field(default_factory=list)
    max_trades_per_day: int = 5
    cooldown_seconds: int = 60
    dedupe_window_minutes: int = 10
    history_retention_days: int = 365
    ticker_whitelist: List[str] = field(default_factory=list)
    sell_only_if_held: bool = True
    place_stop_loss: bool = False


@dataclass
class WealthsimpleConfig:
    accounts: List[WSAccountConfig] = field(default_factory=list)
    exchange_hint: str = ""
    positions_refresh_seconds: int = 30
    values_refresh_seconds: int = 60
    # maintenance margin requirement rate for stock holdings in
    # margin accounts (WS uses 30% for most listings), with optional
    # per-symbol overrides
    stock_margin_rate: float = 0.30
    margin_rate_overrides: dict = field(default_factory=dict)


@dataclass
class PaperConfig:
    # paper trading alongside notify mode: accounts are seeded
    # from their live values and positions, then alerts execute
    # against the paper ledger
    enabled: bool = False
    # mirror the user's own real fills (from the activity feed)
    # into the paper ledger at their execution prices
    mirror: bool = True
    mirror_interval_seconds: int = 60


@dataclass
class ParserConfig:
    custom_patterns: List[str] = field(default_factory=list)


@dataclass
class ReaderConfig:
    pipeline_url: str = "http://localhost:8080/alert"
    poll_interval: float = 0.5
    channel_marker: str = ""
    max_items: int = 40
    auth_token: str = ""
    channels: list = field(default_factory=list)
    auto_scroll: bool = True
    # per-channel server mapping for auto navigation: the
    # channel's server must be selected before its channels
    # appear in discord's ui tree
    channel_servers: dict = field(default_factory=dict)
    auto_switch_channel: bool = True
    discord_reopen_seconds: int = 15
    discord_restart_seconds: int = 90


@dataclass
class AutoUpdateConfig:
    enabled: bool = True
    interval_seconds: int = 600


@dataclass
class QuotesConfig:
    enabled: bool = False
    provider: str = "ws"
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111


@dataclass
class Config:
    pipeline: PipelineConfig
    discord: DiscordConfig
    trading: TradingConfig
    wealthsimple: WealthsimpleConfig
    paper: PaperConfig
    parser: ParserConfig
    auto_update: AutoUpdateConfig
    quotes: QuotesConfig
    reader: ReaderConfig


def _get(d, key, default):
    value = d.get(key, default)
    return default if value is None else value


def _opt_float(d, key):
    value = d.get(key)
    return None if value is None else float(value)


def _opt_int(d, key):
    value = d.get(key)
    return None if value is None else int(value)


def _load_accounts(ws_raw: dict) -> List[WSAccountConfig]:
    accounts = []
    for entry in ws_raw.get("accounts") or []:
        if isinstance(entry, str):
            accounts.append(WSAccountConfig(account_id=entry))
            continue
        accounts.append(
            WSAccountConfig(
                account_id=str(entry.get("account_id", "")),
                label=str(entry.get("label", "")),
                risk_per_trade_pct=_opt_float(entry, "risk_per_trade_pct"),
                max_contracts_per_trade=_opt_int(
                    entry, "max_contracts_per_trade"
                ),
                paper_value=_opt_float(entry, "paper_value"),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    if not accounts:
        single = str(ws_raw.get("account_id", "") or "")
        if single:
            accounts.append(WSAccountConfig(account_id=single, label="default"))
    return accounts


def load_config(path: str) -> Config:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    pipeline_raw = raw.get("pipeline") or {}
    trading_raw = raw.get("trading") or {}
    ws_raw = raw.get("wealthsimple") or {}
    parser_raw = raw.get("parser") or {}

    discord_raw = raw.get("discord") or {}
    webhook = os.environ.get("DISCORD_WEBHOOK_URL") or str(
        _get(discord_raw, "webhook_url", "")
    )

    mode = str(_get(trading_raw, "mode", "notify")).lower()
    if mode not in ("notify", "paper", "live"):
        mode = "notify"

    raw_tiers = _get(trading_raw, "size_tiers", None)
    size_tiers = _default_size_tiers()
    if raw_tiers:
        for tier_name, tier_raw in raw_tiers.items():
            size_tiers[str(tier_name).lower()] = _norm_tier(tier_raw)

    raw_stock_tiers = (
        trading_raw.get("stock_size_tiers") or {}
        if isinstance(trading_raw, dict) else {}
    )
    stock_size_tiers = _default_stock_size_tiers()
    for k, v in raw_stock_tiers.items():
        name = str(k).strip().lower()
        if not name:
            continue
        try:
            stock_size_tiers[name] = float(v)
        except (TypeError, ValueError):
            continue

    trading = TradingConfig(
        mode=mode,
        dry_run=mode != "live",
        order_type=str(_get(trading_raw, "order_type", "limit")).lower(),
        limit_offset_pct=float(_get(trading_raw, "limit_offset_pct", 0.5)),
        position_size_cad=float(_get(trading_raw, "position_size_cad", 100.0)),
        stock_size_tiers=stock_size_tiers,
        risk_per_trade_pct=float(_get(trading_raw, "risk_per_trade_pct", 5.0)),
        max_contracts_per_trade=int(
            _get(trading_raw, "max_contracts_per_trade", 10)
        ),
        max_open_risk_pct=float(_get(trading_raw, "max_open_risk_pct", 30.0)),
        stop_loss_pct=float(_get(trading_raw, "stop_loss_pct", 25.0)),
        trailing_stop_pct=float(_get(trading_raw, "trailing_stop_pct", 0.0)),
        stop_check_seconds=int(_get(trading_raw, "stop_check_seconds", 30)),
        lotto_gain_budget_pct=float(
            _get(trading_raw, "lotto_gain_budget_pct", 75.0)
        ),
        back_to_entry_enabled=bool(
            _get(trading_raw, "back_to_entry_enabled", True)
        ),
        max_consecutive_losses=int(
            _get(trading_raw, "max_consecutive_losses", 2)
        ),
        min_dte_days=int(_get(trading_raw, "min_dte_days", 0)),
        size_tiers=size_tiers,
        paper_account_value=float(
            _get(trading_raw, "paper_account_value", 10000.0)
        ),
        skip_underlyings=[
            t.upper() for t in _get(trading_raw, "skip_underlyings", [])
        ],
        max_trades_per_day=int(_get(trading_raw, "max_trades_per_day", 5)),
        cooldown_seconds=int(_get(trading_raw, "cooldown_seconds", 60)),
        history_retention_days=int(
            _get(trading_raw, "history_retention_days", 365)
        ),
        dedupe_window_minutes=int(
            _get(trading_raw, "dedupe_window_minutes", 10)
        ),
        ticker_whitelist=[
            t.upper() for t in _get(trading_raw, "ticker_whitelist", [])
        ],
        sell_only_if_held=bool(_get(trading_raw, "sell_only_if_held", True)),
        place_stop_loss=bool(_get(trading_raw, "place_stop_loss", False)),
    )

    return Config(
        pipeline=PipelineConfig(
            host=str(_get(pipeline_raw, "host", "0.0.0.0")),
            port=int(_get(pipeline_raw, "port", 8080)),
            auth_token=str(_get(pipeline_raw, "auth_token", "")),
            tls_cert=str(_get(pipeline_raw, "tls_cert", "")),
            tls_key=str(_get(pipeline_raw, "tls_key", "")),
        ),
        discord=DiscordConfig(
        notify=bool(_get(discord_raw, "notify", True)),
            webhook_url=webhook,
            reader_log_webhook_url=str(
                _get(discord_raw, "reader_log_webhook_url", "")
            ),
            pipeline_log_webhook_url=str(
                _get(discord_raw, "pipeline_log_webhook_url", "")
            ),
            update_webhook_url=str(
                _get(discord_raw, "update_webhook_url", "")
            ),
            raw_alert_webhook_url=str(
                _get(discord_raw, "raw_alert_webhook_url", "")
            ),
        ),
        trading=trading,
        wealthsimple=WealthsimpleConfig(
            accounts=_load_accounts(ws_raw),
            exchange_hint=str(_get(ws_raw, "exchange_hint", "")),
            positions_refresh_seconds=int(
                _get(ws_raw, "positions_refresh_seconds", 30)
            ),
            values_refresh_seconds=int(
                _get(ws_raw, "values_refresh_seconds", 60)
            ),
            stock_margin_rate=float(
                _get(ws_raw, "stock_margin_rate", 0.30)
            ),
            margin_rate_overrides=dict(
                _get(ws_raw, "margin_rate_overrides", {}) or {}
            ),
        ),
        parser=ParserConfig(
            custom_patterns=list(_get(parser_raw, "custom_patterns", [])),
        ),
        paper=PaperConfig(
            enabled=bool(_get(raw.get("paper") or {}, "enabled", False)),
            mirror=bool(_get(raw.get("paper") or {}, "mirror", True)),
            mirror_interval_seconds=int(
                _get(raw.get("paper") or {},
                     "mirror_interval_seconds", 60)
            ),
        ),
        auto_update=AutoUpdateConfig(
            enabled=bool(_get(raw.get("auto_update") or {}, "enabled", True)),
            interval_seconds=int(
                _get(raw.get("auto_update") or {}, "interval_seconds", 600)
            ),
        ),
        reader=ReaderConfig(
            pipeline_url=str(
                _get(raw.get("reader") or {}, "pipeline_url",
                     "http://localhost:8080/alert")
            ),
            poll_interval=float(
                _get(raw.get("reader") or {}, "poll_interval", 0.5)
            ),
            channel_marker=str(
                _get(raw.get("reader") or {}, "channel_marker", "")
            ),
            max_items=int(_get(raw.get("reader") or {}, "max_items", 40)),
            auth_token=str(_get(raw.get("reader") or {}, "auth_token", "")),
            channels=[
                str(c).strip().lower()
                for c in (raw.get("reader") or {}).get("channels") or []
                if str(c).strip()
            ],
            channel_servers={
                str(k).strip().lower(): str(v).strip()
                for k, v in (
                    (raw.get("reader") or {}).get("channel_servers")
                    or {}
                ).items()
                if str(k).strip() and str(v).strip()
            },
            auto_switch_channel=bool(
                _get(raw.get("reader") or {},
                     "auto_switch_channel", True)
            ),
            discord_reopen_seconds=int(
                _get(raw.get("reader") or {},
                     "discord_reopen_seconds", 15)
            ),
            discord_restart_seconds=int(
                _get(raw.get("reader") or {},
                     "discord_restart_seconds", 90)
            ),
            auto_scroll=bool(
                _get(raw.get("reader") or {}, "auto_scroll", True)
            ),
        ),
        quotes=QuotesConfig(
            enabled=bool(_get(raw.get("quotes") or {}, "enabled", False)),
            provider=str(
                _get(raw.get("quotes") or {}, "provider", "ws")
            ).lower(),
            moomoo_host=str(
                _get(raw.get("quotes") or {}, "moomoo_host", "127.0.0.1")
            ),
            moomoo_port=int(
                _get(raw.get("quotes") or {}, "moomoo_port", 11111)
            ),
        ),
    )
