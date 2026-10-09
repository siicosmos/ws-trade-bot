import re
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class Alert:
    action: str
    ticker: str
    kind: str = "stock"
    underlying: str = ""
    expiry: Optional[str] = None
    strike: Optional[float] = None
    right: Optional[str] = None
    premium: Optional[float] = None
    scale: Optional[float] = None
    gain_pct: Optional[float] = None
    size: Optional[str] = None
    # "profits only": the alert may only spend what was realized
    # selling today (lotto rules apply to the budget either way)
    profits_only: bool = False
    # set on auto-exits (stop loss / take profit / trailing): the
    # sell limit is priced marketable (protection over price)
    stop_exit: bool = False
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    raw: str = ""

    def contract_key(self) -> str:
        if self.kind != "option":
            return self.ticker
        return f"{self.underlying}-{self.expiry}-{self.strike:g}-{self.right}"

    def dedupe_key(self) -> str:
        if self.kind == "option":
            return (
                f"OPT|{self.underlying}|{self.expiry}|{self.strike:g}|"
                f"{self.right}|{self.action}|{self.premium}"
            )
        return f"STK|{self.ticker}|{self.action}|{self.entry}"

    def to_dict(self):
        return {
            "kind": self.kind,
            "action": self.action,
            "ticker": self.ticker,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "right": self.right,
            "premium": self.premium,
            "scale": self.scale,
            "gain_pct": self.gain_pct,
            "size": self.size,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "raw": self.raw[:500],
        }


OPT_BUY_RE = re.compile(
    r"\bBOUGHT(?:\s+MORE)?\s+"
    r"(?:(?P<expiry>0DTE|\d{1,2}/\d{1,2})\s+)?"
    r"(?P<underlying>[A-Z]{2,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<right>[cCpP])\b"
    r"(?:\s*@\s*(?P<premium>\d*\.?\d+))?"
)

OPT_SELL_RE = re.compile(
    r"\b(?P<verb>ALL\s+OUT|SOLD(?:\s+MOST)?)\s+"
    r"(?:(?P<frac>[1-9]/[2-9])\s+)?"
    r"(?:(?P<expiry>0DTE|\d{1,2}/\d{1,2})\s+)?"
    r"(?P<underlying>[A-Z]{2,5})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<right>[cCpP])\b"
    r"(?:\s*@\s*(?P<premium>\d*\.?\d+))?"
)

STOCK_SERVICE_RE = re.compile(
    r"\b(?P<verb>BOUGHT|SOLD|ALL\s+OUT)\s+"
    r"(?P<underlying>[A-Z]{2,5})\s+shares\b"
    r"(?:\s*@\s*(?P<price>\d*\.?\d+))?"
)

SIZE_RE = re.compile(
    r"\b(?P<size>tiny|small|medium|large|big|full|lotto|micro)\s+size\b", re.I
)

STOPWORDS = {
    "BUY", "SELL", "SL", "TP", "PT", "LONG", "SHORT", "EXIT", "CLOSE",
    "ENTRY", "TARGET", "STOP", "NOW", "USD", "CAD", "CALL", "PUT", "AT",
    "THE", "TO", "MOON", "CHART", "BREAKOUT", "ALERT", "SIGNAL", "TRADE",
    "ORDER", "PRICE", "LIMIT", "MARKET", "OPEN", "HIGH", "LOW", "NEWS",
    "IMO", "PUMP", "WATCH", "ADD", "SCALE", "BB", "MA", "EMA", "RSI",
    # action verbs that also match the ticker shape - without
    # these, "SOLD XYZ @ 12" parsed ticker=SOLD and the sell
    # no-oped against every position
    "SOLD", "DUMP", "GRAB", "LOAD", "OUT", "ALL",
}

BUY_RE = re.compile(
    r"\b(bought|buys?|buying|long|bull(?:ish)?|accumulate|entry|grab|load(?:ing)?)\b",
    re.I,
)
SELL_RE = re.compile(
    r"\b(sold|sells?|selling|short(?:ing)?|bear(?:ish)?|exit|close[sd]?|dump(?:ing)?|all\s+out)\b",
    re.I,
)
TICKER_CASH_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,4})")
TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
ENTRY_RE = re.compile(
    r"(?:entry|enter(?:ing)?|in\s*at|avg\.?|@)\s*[:\$]?\s*(\d*\.?\d+)",
    re.I,
)
STOP_RE = re.compile(
    r"(?:\bsl\b|stop\s*loss|stop)\s*[:@\$]?\s*(\d*\.?\d+)",
    re.I,
)
TP_RE = re.compile(
    r"(?:\btp\b|\bpt\b|targets?|take\s*profit(?:\s*at)?)\s*[:@\$]?\s*(\d*\.?\d+)",
    re.I,
)

GAIN_RE = re.compile(r"([+-]\s?\d{1,3}(?:\.\d+)?)\s*%")
# "for 120%", "at 120%", "up 120%" - gains stated without a sign
GAIN_WORD_RE = re.compile(
    r"\b(?:for|at|up|of)\s+(\d{1,3}(?:\.\d+)?)\s*%", re.I
)


def _gain(text: str) -> Optional[float]:
    m = GAIN_RE.search(text or "")
    if not m:
        m = GAIN_WORD_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", ""))
    except ValueError:
        return None


CORRECTION_RE = re.compile(
    r"\b(typos?\b|corrections?\b|correct(?:ed|ing|ion)?\b|"
    r"ignore\s+(?:that|last|previous|the|it)|disregard\b|"
    r"scratch\s+(?:that|it|last)|mistake\b|"
    r"meant\b|should\s+(?:be|have\s+been)|instead\s+of|"
    r"wrong\s+(?:strike|price|ticker|entry|level|alert|expiry|contract)|"
    r"edit:|fix:|cancel(?:ed|ling|s)?\b|not\s+\d)",
    re.I,
)


def is_correction(text: str) -> bool:
    if not text:
        return False
    return bool(CORRECTION_RE.search(text))


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_expiry(raw_expiry: Optional[str]) -> Optional[str]:
    # the et calendar day: the overnight session runs past
    # midnight et, and a non-et box's local date would resolve
    # 0DTE to the wrong expiry (the contract key would never
    # match the held position)
    try:
        from core.store import et_now
        today = et_now().date()
    except Exception:
        today = date.today()
    if not raw_expiry or raw_expiry.upper() == "0DTE":
        return today.isoformat()
    try:
        month, day = raw_expiry.split("/")
        exp = date(today.year, int(month), int(day))
    except ValueError:
        return None
    if exp < today:
        try:
            exp = date(today.year + 1, int(month), int(day))
        except ValueError:
            return None
    return exp.isoformat()


SCALE_LABELS = [
    (1.0, "all"),
    (0.75, "3/4"),
    (5 / 6, "5/6"),
    (2 / 3, "2/3"),
    (0.5, "1/2"),
    (1 / 3, "1/3"),
    (0.25, "1/4"),
]


def format_scale(scale):
    if scale is None:
        return None
    for value, label in SCALE_LABELS:
        if abs(scale - value) < 0.02:
            return label
    return round(scale, 2)


def _scale_from_verb(verb: str, frac: Optional[str]) -> Optional[float]:
    if verb.startswith("ALL"):
        return 1.0
    if "MOST" in verb:
        return 0.75
    if frac:
        num, den = frac.split("/")
        return float(num) / float(den)
    return 1.0


def _option_alert(match, action: str, raw: str, size: Optional[str]) -> Optional[Alert]:
    underlying = match.group("underlying").upper()
    expiry = _resolve_expiry(match.group("expiry"))
    if expiry is None:
        return None
    strike = _num(match.group("strike"))
    right = match.group("right").upper()
    premium = _num(match.group("premium"))
    scale = None
    if action == "SELL":
        scale = _scale_from_verb(match.group("verb"), match.group("frac"))
    return Alert(
        action=action,
        ticker=underlying,
        kind="option",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,
        premium=premium,
        scale=scale,
        gain_pct=_gain(raw),
        size=size,
        profits_only=bool(
            re.search(r"\bprofits?\s+only\b", raw or "", re.I)
        ),
        raw=raw,
    )


def parse_alert(text: str, custom_patterns: Optional[List[str]] = None) -> Optional[Alert]:
    if not text or not text.strip():
        return None

    for pattern in custom_patterns or []:
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        gd = m.groupdict()
        ticker = (gd.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        action = (gd.get("action") or "").strip().upper()
        if action not in ("BUY", "SELL"):
            action = "SELL" if SELL_RE.search(text) else "BUY"
        return Alert(
            action=action,
            ticker=ticker,
            entry=_num(gd.get("entry")),
            stop_loss=_num(gd.get("stop_loss")),
            take_profit=_num(gd.get("take_profit")),
            raw=text,
        )

    size_m = SIZE_RE.search(text)
    size = size_m.group("size").lower() if size_m else None
    if re.search(r"\blotto\b", text, re.I):
        size = "lotto"
    # "hero or zero" is the author's phrasing for a lotto trade:
    # a full loss or a moonshot, always 1 contract
    if re.search(r"\bhero\s+or\s+zero\b", text, re.I):
        size = "lotto"
    # "profits only": the buy is only allowed against what was
    # realized selling today
    profits_only = bool(re.search(r"\bprofits?\s+only\b", text, re.I))

    m = OPT_BUY_RE.search(text)
    if m:
        alert = _option_alert(m, "BUY", text, size)
        if alert:
            return alert

    m = OPT_SELL_RE.search(text)
    if m:
        alert = _option_alert(m, "SELL", text, size)
        if alert:
            return alert

    m = STOCK_SERVICE_RE.search(text)
    if m:
        verb = re.sub(r"\s+", " ", m.group("verb").upper())
        action = "SELL" if verb in ("SOLD", "ALL OUT") else "BUY"
        underlying = m.group("underlying").upper()
        price = _num(m.group("price"))
        return Alert(
            action=action,
            ticker=underlying,
            kind="stock",
            underlying=underlying,
            scale=_scale_from_verb(verb, None) if action == "SELL" else None,
            entry=price,
            premium=price,
            gain_pct=_gain(text),
            size=size,
            profits_only=profits_only,
            raw=text,
        )

    if not BUY_RE.search(text) and not SELL_RE.search(text):
        return None

    ticker_match = TICKER_CASH_RE.search(text)
    if ticker_match:
        ticker = ticker_match.group(1).upper()
    else:
        ticker = None
        for cand in TICKER_RE.findall(text):
            if cand not in STOPWORDS:
                ticker = cand
                break
    if not ticker:
        return None

    action = "BUY" if BUY_RE.search(text) else "SELL"

    entry_m = ENTRY_RE.search(text)
    stop_m = STOP_RE.search(text)
    tp_m = TP_RE.search(text)
    return Alert(
        action=action,
        ticker=ticker,
        size=size,
        profits_only=profits_only,
        entry=_num(entry_m.group(1)) if entry_m else None,
        stop_loss=_num(stop_m.group(1)) if stop_m else None,
        take_profit=_num(tp_m.group(1)) if tp_m else None,
        gain_pct=_gain(text),
        raw=text,
    )
