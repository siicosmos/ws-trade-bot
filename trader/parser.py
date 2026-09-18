import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Alert:
    action: str
    ticker: str
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    raw: str = ""

    def to_dict(self):
        return {
            "action": self.action,
            "ticker": self.ticker,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "raw": self.raw[:500],
        }


STOPWORDS = {
    "BUY", "SELL", "SL", "TP", "PT", "LONG", "SHORT", "EXIT", "CLOSE",
    "ENTRY", "TARGET", "STOP", "NOW", "USD", "CAD", "CALL", "PUT", "AT",
    "THE", "TO", "MOON", "CHART", "BREAKOUT", "ALERT", "SIGNAL", "TRADE",
    "ORDER", "PRICE", "LIMIT", "MARKET", "OPEN", "HIGH", "LOW", "NEWS",
    "IMO", "PUMP", "WATCH", "ADD", "SCALE", "BB", "MA", "EMA", "RSI",
}

BUY_RE = re.compile(
    r"\b(buys?|buying|long|bull(?:ish)?|accumulate|entry|grab|load(?:ing)?)\b",
    re.I,
)
SELL_RE = re.compile(
    r"\b(sells?|selling|short(?:ing)?|bear(?:ish)?|exit|close[sd]?|dump(?:ing)?)\b",
    re.I,
)
TICKER_CASH_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,4})")
TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
ENTRY_RE = re.compile(
    r"(?:entry|enter(?:ing)?|in\s*at|avg\.?|@)\s*[:\$]?\s*(\d+(?:\.\d+)?)",
    re.I,
)
STOP_RE = re.compile(
    r"(?:\bsl\b|stop\s*loss|stop)\s*[:@\$]?\s*(\d+(?:\.\d+)?)",
    re.I,
)
TP_RE = re.compile(
    r"(?:\btp\b|\bpt\b|targets?|take\s*profit(?:\s*at)?)\s*[:@\$]?\s*(\d+(?:\.\d+)?)",
    re.I,
)


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_ticker(text: str) -> Optional[str]:
    m = TICKER_CASH_RE.search(text)
    if m:
        return m.group(1).upper()
    for cand in TICKER_RE.findall(text):
        if cand not in STOPWORDS:
            return cand
    return None


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

    if not BUY_RE.search(text) and not SELL_RE.search(text):
        return None

    ticker = _extract_ticker(text)
    if not ticker:
        return None

    action = "BUY" if BUY_RE.search(text) else "SELL"

    return Alert(
        action=action,
        ticker=ticker,
        entry=_num(ENTRY_RE.search(text).group(1)) if ENTRY_RE.search(text) else None,
        stop_loss=_num(STOP_RE.search(text).group(1)) if STOP_RE.search(text) else None,
        take_profit=_num(TP_RE.search(text).group(1)) if TP_RE.search(text) else None,
        raw=text,
    )
