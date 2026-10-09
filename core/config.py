import os
import tempfile
import time
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
    # consumer = the trading app (default, today's behavior);
    # info = the alert source server: reader ingest + feed,
    # no trading
    role: str = "consumer"


@dataclass
class DiscordConfig:
    # this app's log tail (consumer.log / info.log) posted to
    # discord in batches; empty = off
    consumer_log_webhook_url: str = ""
    update_webhook_url: str = ""
    # send parsed alerts to the webhook
    notify: bool = True
    # trade alerts to your phone (consumer)
    trade_alert_webhook_url: str = ""



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
    # margin | non_margin - "" = auto-detect from the Wealthsimple
    # API's unifiedAccountType (registered plans RRSP/TFSA/FHSA/...
    # never carry margin; they model as non_margin)
    type: str = ""
    risk_per_trade_pct: Optional[float] = None
    max_contracts_per_trade: Optional[int] = None
    # per-account open-risk cap: a small account may need a much
    # higher share of its own value deployed (the $ exposure
    # stays small) without raising the cap for the other accounts
    max_open_risk_pct: Optional[float] = None
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
    max_contracts_per_trade: int = 10
    max_open_risk_pct: float = 30.0
    # correlation-aware risk: a single (underlying, expiry, right)
    # cluster may never exceed this % of the account value (the
    # global open-risk cap still applies on top). 0 = off
    cluster_cap_pct: float = 50.0
    # notice when an actual fill lands this % away from the
    # order's estimated price (data only, no auto-pause)
    max_slippage_pct: float = 2.0
    # a partially-filled order whose market price ran this % away
    # from the estimate gets its remainder cancelled - the filled
    # part stays as the position for future alerts. 0 = off
    partial_fill_cancel_pct: float = 10.0
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
    # hard daily-loss circuit breaker: stop taking new BUY alerts
    # once today's realized pnl sinks below this % of account
    # value (0 = off). exits (alert sells, stops, b2e) stay
    # allowed - only entries are gated
    max_daily_loss_pct: float = 0.0
    # runtime kill switch: flips from the dashboard without a
    # restart - blocks every new BUY (options and stocks) while
    # set; exits (alert sells, stop monitor) stay allowed
    trading_paused: bool = False


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
    info_server_url: str = "http://localhost:8080/alert"
    poll_interval: float = 0.5
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
    # release-install clients (no .git) download the consumer zip
    # from GitHub Releases; a private repo needs a read-only token
    # here (or the GITHUB_TOKEN env var). git installs ignore it
    github_token: str = ""
    # the rolling release tag the client polls
    release_tag: str = "consumer-latest"


@dataclass
class QuotesConfig:
    enabled: bool = False
    provider: str = "ws"
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111


@dataclass
class ConsumerEntry:
    """A consumer app allowed to read the alert feed.

    token authenticates both directions: the consumer's feed
    client uses it against this server's feed API, and this
    server presents it when pushing alerts to the consumer's
    own /alert endpoint (push_url; empty = pull-only)."""
    label: str = ""
    token: str = ""
    push_url: str = ""
    # verify the consumer's TLS certificate on push (the self
    # signed dev certs need this off; a proper CA or Tailscale
    # cert turns it on)
    push_verify_ssl: bool = False


@dataclass
class FeedConfig:
    """Consumer side: where the alert feed lives."""
    url: str = ""
    token: str = ""
    poll_seconds: float = 1.0
    # the info server usually runs a self-signed cert - verify
    # only when explicitly asked for
    verify_ssl: bool = False


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
    consumers: List[ConsumerEntry] = field(default_factory=list)
    feed: FeedConfig = field(default_factory=FeedConfig)


def dump_yaml_config(raw, config_path):
    """Atomic yaml write, preserving blank-line layout between
    top-level sections."""
    directory = os.path.dirname(os.path.abspath(config_path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # windows: the destination can be briefly locked (an open
    # handle or a real-time scanner) - os.replace then fails with
    # permission denied; retry briefly before giving up
    for attempt in range(5):
        try:
            os.replace(tmp, config_path)
            break
        except PermissionError:
            if attempt == 4:
                os.unlink(tmp)
                raise
            time.sleep(0.2 * (attempt + 1))




def _get(d, key, default):
    value = d.get(key, default)
    return default if value is None else value


def _valid_custom_patterns(patterns):
    """Compile each custom pattern once at load - an invalid
    regex is dropped with a note (it would otherwise raise
    re.error inside the alert-processing path on every message,
    and a pathological one could hang the thread)."""
    import re as _re

    valid = []
    for pattern in patterns or []:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            _re.compile(pattern)
        except _re.error as e:
            print(
                f"config: dropping invalid custom_patterns entry "
                f"{pattern!r}: {e}"
            )
            continue
        valid.append(pattern)
    return valid


def _section(raw, key):
    """The named config section as a dict - a scalar/list where a
    mapping belongs (a typo, an accidental paste) must not crash
    the startup with an AttributeError inside _get; the section
    is treated as absent instead."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


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
        acct_type = str(entry.get("type", "") or "").strip().lower()
        if acct_type not in ("", "margin", "non_margin"):
            acct_type = ""
        accounts.append(
            WSAccountConfig(
                account_id=str(entry.get("account_id", "")),
                label=str(entry.get("label", "")),
                type=acct_type,
                risk_per_trade_pct=_opt_float(entry, "risk_per_trade_pct"),
                max_contracts_per_trade=_opt_int(
                    entry, "max_contracts_per_trade"
                ),
                max_open_risk_pct=_opt_float(entry, "max_open_risk_pct"),
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
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # the web-server section is named for the role (consumer: /
    # info: - the section key IS the role); a config with neither
    # defaults to the consumer role with default host/port
    pipeline_raw = None
    role = "consumer"
    for key, section_role in (("info", "info"), ("consumer", "consumer")):
        section = raw.get(key)
        if isinstance(section, dict) and section:
            pipeline_raw = section
            role = section_role
            break
    if pipeline_raw is None:
        pipeline_raw = {}
    trading_raw = _section(raw, "trading")
    ws_raw = _section(raw, "wealthsimple")
    parser_raw = _section(raw, "parser")

    # role: consumer = the trading app; info = the alert source
    # server (reader ingest + feed, no trading) - the section key
    # already decided (missing section = consumer default)
    if role not in ("info", "consumer"):
        role = "consumer"

    consumers = []
    for entry in raw.get("consumers") or []:
        if not isinstance(entry, dict):
            continue
        consumers.append(
            ConsumerEntry(
                label=str(entry.get("label", "")).strip(),
                token=str(entry.get("token", "")).strip(),
                push_url=str(entry.get("push_url", "")).strip(),
                push_verify_ssl=bool(entry.get("push_verify_ssl", False)),
            )
        )

    feed_raw = _section(raw, "feed")
    feed = FeedConfig(
        url=str(_get(feed_raw, "url", "")).strip(),
        token=str(_get(feed_raw, "token", "")).strip(),
        poll_seconds=float(_get(feed_raw, "poll_seconds", 1.0)),
        verify_ssl=bool(_get(feed_raw, "verify_ssl", False)),
    )

    discord_raw = _section(raw, "discord")
    webhook = (
        os.environ.get("DISCORD_WEBHOOK_URL")
        or str(_get(discord_raw, "trade_alert_webhook_url", ""))
    )

    mode = str(_get(trading_raw, "mode", "notify")).lower()
    if mode not in ("notify", "paper", "live"):
        mode = "notify"

    raw_tiers = _get(trading_raw, "size_tiers", None)
    size_tiers = _default_size_tiers()
    if raw_tiers:
        for tier_name, tier_raw in raw_tiers.items():
            size_tiers[str(tier_name).lower()] = _norm_tier(tier_raw)

    raw_stock_tiers = _section(trading_raw, "stock_size_tiers")
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
        cluster_cap_pct=float(_get(trading_raw, "cluster_cap_pct", 50.0)),
        max_slippage_pct=float(_get(trading_raw, "max_slippage_pct", 2.0)),
        partial_fill_cancel_pct=float(
            _get(trading_raw, "partial_fill_cancel_pct", 10.0)
        ),
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
        max_daily_loss_pct=float(
            _get(trading_raw, "max_daily_loss_pct", 0.0)
        ),
        trading_paused=bool(
            _get(trading_raw, "trading_paused", False)
        ),
    )

    return Config(
        pipeline=PipelineConfig(
            host=str(_get(pipeline_raw, "host", "0.0.0.0")),
            port=int(_get(pipeline_raw, "port", 8080)),
            auth_token=str(_get(pipeline_raw, "auth_token", "")),
            tls_cert=str(_get(pipeline_raw, "tls_cert", "")),
            tls_key=str(_get(pipeline_raw, "tls_key", "")),
            role=role,
        ),
        discord=DiscordConfig(
            notify=bool(_get(discord_raw, "notify", True)),
            trade_alert_webhook_url=webhook,
            consumer_log_webhook_url=str(
                _get(discord_raw, "consumer_log_webhook_url", "")
            ),
            update_webhook_url=str(
                _get(discord_raw, "update_webhook_url", "")
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
            custom_patterns=_valid_custom_patterns(
                _get(parser_raw, "custom_patterns", [])
            ),
        ),
        paper=PaperConfig(
            enabled=bool(_get(_section(raw, "paper"), "enabled", False)),
            mirror=bool(_get(_section(raw, "paper"), "mirror", True)),
            mirror_interval_seconds=int(
                _get(_section(raw, "paper"),
                     "mirror_interval_seconds", 60)
            ),
        ),
        auto_update=AutoUpdateConfig(
            enabled=bool(_get(_section(raw, "auto_update"), "enabled", True)),
            interval_seconds=int(
                _get(_section(raw, "auto_update"), "interval_seconds", 600)
            ),
            github_token=str(
                _get(_section(raw, "auto_update"), "github_token", "")
            ),
            release_tag=str(
                _get(_section(raw, "auto_update"), "release_tag",
                     "consumer-latest")
            ),
        ),
        reader=ReaderConfig(
            info_server_url=str(
                _get(_section(raw, "reader"), "info_server_url",
                     "http://localhost:8080/alert")
            ),
            poll_interval=float(
                _get(_section(raw, "reader"), "poll_interval", 0.5)
            ),
            max_items=int(_get(_section(raw, "reader"), "max_items", 40)),
            auth_token=str(_get(_section(raw, "reader"), "auth_token", "")),
            channels=[
                str(c).strip().lower()
                for c in (_section(raw, "reader")).get("channels") or []
                if str(c).strip()
            ],
            channel_servers={
                str(k).strip().lower(): str(v).strip()
                for k, v in (
                    (_section(raw, "reader")).get("channel_servers")
                    or {}
                ).items()
                if str(k).strip() and str(v).strip()
            },
            auto_switch_channel=bool(
                _get(_section(raw, "reader"),
                     "auto_switch_channel", True)
            ),
            discord_reopen_seconds=int(
                _get(_section(raw, "reader"),
                     "discord_reopen_seconds", 15)
            ),
            discord_restart_seconds=int(
                _get(_section(raw, "reader"),
                     "discord_restart_seconds", 90)
            ),
            auto_scroll=bool(
                _get(_section(raw, "reader"), "auto_scroll", True)
            ),
        ),
        quotes=QuotesConfig(
            enabled=bool(_get(_section(raw, "quotes"), "enabled", False)),
            provider=str(
                _get(_section(raw, "quotes"), "provider", "ws")
            ).lower(),
            moomoo_host=str(
                _get(_section(raw, "quotes"), "moomoo_host", "127.0.0.1")
            ),
            moomoo_port=int(
                _get(_section(raw, "quotes"), "moomoo_port", 11111)
            ),
        ),
        consumers=consumers,
        feed=feed,
    )
