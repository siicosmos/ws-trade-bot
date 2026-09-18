import os
from dataclasses import dataclass, field
from typing import List, Optional

import yaml


@dataclass
class PipelineConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: str = ""


@dataclass
class DiscordConfig:
    webhook_url: str = ""


def _default_size_tiers() -> dict:
    return {
        "lotto": {"risk_pct_max": 0.5, "contracts_min": 1, "contracts_max": 1},
        "micro": {"risk_pct_max": 0.5, "contracts_min": 1, "contracts_max": 1},
        "tiny": {"risk_pct_max": 1.0, "contracts_min": 1, "contracts_max": 1},
        "small": {"risk_pct_max": 2.0, "contracts_min": 1, "contracts_max": 2},
        "medium": {"risk_pct_max": 5.0, "contracts_min": 1, "contracts_max": 5},
        "large": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10},
        "big": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10},
        "full": {"risk_pct_max": 10.0, "contracts_min": 1, "contracts_max": 10},
    }


def _norm_tier(d: dict) -> dict:
    return {
        "risk_pct_max": float(d.get("risk_pct_max", 5.0)),
        "contracts_min": int(d.get("contracts_min", 1)),
        "contracts_max": int(d.get("contracts_max", 10)),
    }


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
    risk_per_trade_pct: float = 5.0
    size_tiers: dict = field(default_factory=_default_size_tiers)
    max_contracts_per_trade: int = 10
    max_open_risk_pct: float = 30.0
    paper_account_value: float = 10000.0
    skip_underlyings: List[str] = field(default_factory=list)
    max_trades_per_day: int = 5
    cooldown_seconds: int = 60
    dedupe_window_minutes: int = 10
    ticker_whitelist: List[str] = field(default_factory=list)
    sell_only_if_held: bool = True
    place_stop_loss: bool = False


@dataclass
class WealthsimpleConfig:
    accounts: List[WSAccountConfig] = field(default_factory=list)
    exchange_hint: str = ""


@dataclass
class ParserConfig:
    custom_patterns: List[str] = field(default_factory=list)


@dataclass
class Config:
    pipeline: PipelineConfig
    discord: DiscordConfig
    trading: TradingConfig
    wealthsimple: WealthsimpleConfig
    parser: ParserConfig


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

    webhook = os.environ.get("DISCORD_WEBHOOK_URL") or str(
        _get(raw.get("discord") or {}, "webhook_url", "")
    )

    mode = str(_get(trading_raw, "mode", "notify")).lower()
    if mode not in ("notify", "paper", "live"):
        mode = "notify"

    raw_tiers = _get(trading_raw, "size_tiers", None)
    if raw_tiers:
        size_tiers = {
            str(k).lower(): _norm_tier(v) for k, v in raw_tiers.items()
        }
    else:
        size_tiers = _default_size_tiers()

    trading = TradingConfig(
        mode=mode,
        dry_run=mode != "live",
        order_type=str(_get(trading_raw, "order_type", "limit")).lower(),
        limit_offset_pct=float(_get(trading_raw, "limit_offset_pct", 0.5)),
        position_size_cad=float(_get(trading_raw, "position_size_cad", 100.0)),
        risk_per_trade_pct=float(_get(trading_raw, "risk_per_trade_pct", 5.0)),
        max_contracts_per_trade=int(
            _get(trading_raw, "max_contracts_per_trade", 10)
        ),
        max_open_risk_pct=float(_get(trading_raw, "max_open_risk_pct", 30.0)),
        size_tiers=size_tiers,
        paper_account_value=float(
            _get(trading_raw, "paper_account_value", 10000.0)
        ),
        skip_underlyings=[
            t.upper() for t in _get(trading_raw, "skip_underlyings", [])
        ],
        max_trades_per_day=int(_get(trading_raw, "max_trades_per_day", 5)),
        cooldown_seconds=int(_get(trading_raw, "cooldown_seconds", 60)),
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
        ),
        discord=DiscordConfig(webhook_url=webhook),
        trading=trading,
        wealthsimple=WealthsimpleConfig(
            accounts=_load_accounts(ws_raw),
            exchange_hint=str(_get(ws_raw, "exchange_hint", "")),
        ),
        parser=ParserConfig(
            custom_patterns=list(_get(parser_raw, "custom_patterns", [])),
        ),
    )
