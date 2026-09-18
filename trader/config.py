import os
from dataclasses import dataclass, field
from typing import List

import yaml


@dataclass
class PipelineConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class DiscordConfig:
    webhook_url: str = ""


@dataclass
class TradingConfig:
    mode: str = "notify"
    dry_run: bool = True
    order_type: str = "limit"
    limit_offset_pct: float = 0.5
    position_size_cad: float = 100.0
    risk_per_trade_pct: float = 5.0
    max_contracts_per_trade: int = 10
    max_open_risk_pct: float = 30.0
    size_risk_multiplier: dict = field(default_factory=dict)
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
    account_id: str = ""
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
        size_risk_multiplier=dict(_get(trading_raw, "size_risk_multiplier", {})),
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
            account_id=str(_get(ws_raw, "account_id", "")),
            exchange_hint=str(_get(ws_raw, "exchange_hint", "")),
        ),
        parser=ParserConfig(
            custom_patterns=list(_get(parser_raw, "custom_patterns", [])),
        ),
    )
