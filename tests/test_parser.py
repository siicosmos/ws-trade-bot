import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.trading.parser import parse_alert


def test_option_buy_0dte():
    a = parse_alert(
        "BOUGHT 0DTE SPX 7645c @ .65 @everyone ROLL UP LOTTO 🎲 tiny size"
    )
    assert a is not None
    assert a.kind == "option"
    assert a.action == "BUY"
    assert a.underlying == "SPX"
    assert a.strike == 7645
    assert a.right == "C"
    assert a.premium == 0.65
    assert a.expiry == date.today().isoformat()
    assert a.size == "lotto"


def test_option_sell_quarter():
    a = parse_alert(
        "SOLD 1/4 0DTE SPX 7650c @ 2.0 @everyone 2X MULTIPLIER 💥💸 runners FREE"
    )
    assert a is not None
    assert a.action == "SELL"
    assert a.underlying == "SPX"
    assert a.strike == 7650
    assert a.premium == 2.0
    assert a.scale == 0.25


def test_all_out():
    a = parse_alert(
        "ALL OUT 0DTE SPX 7650c @ 1.3 @everyone out runners here +30% 🏆"
    )
    assert a is not None
    assert a.action == "SELL"
    assert a.scale == 1.0


def test_option_buy_dated():
    a = parse_alert("BOUGHT 09/18 SPY 753c @ 2.9 @everyone small size will swing 🪃🪃🪃")
    assert a is not None
    assert a.action == "BUY"
    assert a.underlying == "SPY"
    assert a.strike == 753
    assert a.right == "C"
    assert a.premium == 2.9
    assert a.size == "small"
    assert a.expiry.endswith("-09-18")


def test_option_sell_with_date_and_frac():
    a = parse_alert("SOLD 1/4 09/18 SPY 753c @ 3.78 @everyone +30% 💰🔥")
    assert a is not None
    assert a.action == "SELL"
    assert a.underlying == "SPY"
    assert a.strike == 753
    assert a.scale == 0.25
    assert a.expiry.endswith("-09-18")


def test_sold_most():
    a = parse_alert("SOLD MOST 09/18 SPY 753c @ 10.05 @everyone 3.5X MULTIPLIER 💰🔥")
    assert a is not None
    assert a.action == "SELL"
    assert a.scale == 0.75


def test_sold_two_thirds():
    a = parse_alert("SOLD 2/3 0DTE SPX 7645c @ 1.0 @everyone +50% 💰🔥")
    assert a is not None
    assert a.scale == 2 / 3


def test_decimal_strike():
    a = parse_alert("BOUGHT 09/16 GOOGL 347.5c @ .48 @everyone lotto size 🎲")
    assert a is not None
    assert a.underlying == "GOOGL"
    assert a.strike == 347.5
    assert a.premium == 0.48
    assert a.size == "lotto"


def test_all_out_dated_put_styled():
    a = parse_alert("ALL OUT 09/16 INTC 102c @ 1.6 @everyone out rest BE 🏆")
    assert a is not None
    assert a.action == "SELL"
    assert a.underlying == "INTC"
    assert a.strike == 102
    assert a.scale == 1.0


def test_stock_shares_alert():
    a = parse_alert("BOUGHT BFLY shares @ 7.72 @everyone")
    assert a is not None
    assert a.kind == "stock"
    assert a.action == "BUY"
    assert a.ticker == "BFLY"
    assert a.entry == 7.72


def test_own_fill_confirmation_ignored():
    assert parse_alert("SPXplays — 08:41 executed 2.15 ^") is None


def test_noise_author_lines_ignored():
    assert parse_alert("📢 SPX Plays • Option Alert APP — 08:40") is None


def test_alert_with_ui_noise_prefix():
    a = parse_alert(
        "📢 SPX Plays • Option Alert APP — 06:58 "
        "BOUGHT 09/16 MSFT 505c @ 1.75 @everyone small size"
    )
    assert a is not None
    assert a.underlying == "MSFT"
    assert a.strike == 505
    assert a.premium == 1.75


def test_iren_dated():
    a = parse_alert("BOUGHT 09/25 IREN 47c @ 1.52 @everyone small size")
    assert a is not None
    assert a.underlying == "IREN"
    assert a.strike == 47
    assert a.expiry.endswith("-09-25")


def test_dedupe_keys_differ_by_premium():
    a1 = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.0 @everyone")
    a2 = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.32 @everyone")
    assert a1.dedupe_key() != a2.dedupe_key()


def test_generic_fallback_still_works():
    a = parse_alert("BUY TSLA @ 250, SL 240, TP 270")
    assert a is not None
    assert a.kind == "stock"
    assert a.action == "BUY"
    assert a.ticker == "TSLA"
    assert a.entry == 250
    assert a.stop_loss == 240
    assert a.take_profit == 270


def test_generic_dollar_ticker():
    a = parse_alert("🚀🚀 LONG $AAPL entry 180.5 stop 175 target 195")
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "AAPL"


def test_no_action_returns_none():
    assert parse_alert("market looking choppy today, stay safe") is None


def test_is_correction_positive():
    from trader.trading.parser import is_correction
    samples = [
        "Typo on the last alert, it was 760c not 7645c",
        "CORRECTION: entry should have been 759c",
        "ignore last alert",
        "Disregard previous entry",
        "Edit: stop loss should be 245",
    ]
    for s in samples:
        assert is_correction(s), s


def test_is_correction_negative():
    from trader.trading.parser import is_correction
    samples = [
        "BOUGHT 0DTE SPX 7585c @ .7 @everyone HERO or ZERO",
        "SOLD 1/4 0DTE SPX 7585c @ 2.0 runners FREE",
        "executed 2.15 ^",
        "market looking choppy today",
    ]
    for s in samples:
        assert not is_correction(s), s


def test_lotto_word_wins_over_tiny_size():
    a = parse_alert("BOUGHT 0DTE SPX 7645c @ .65 ROLL UP LOTTO tiny size")
    assert a.size == "lotto"


def test_format_scale():
    from trader.trading.parser import format_scale
    assert format_scale(None) is None
    assert format_scale(2 / 3) == "2/3"
    assert format_scale(1 / 3) == "1/3"
    assert format_scale(0.25) == "1/4"
    assert format_scale(0.5) == "1/2"
    assert format_scale(0.75) == "3/4"
    assert format_scale(1.0) == "all"
    assert format_scale(0.4) == 0.4


def test_gain_pct_parsed():
    a = parse_alert("SOLD 1/3 08/26 AVGO 352.5c @ 1.55 +20% SCALED")
    assert a.gain_pct == 20.0
    b = parse_alert("SOLD 1/4 0DTE SPY 759c @ 2.0 runners FREE")
    assert b.gain_pct is None
    c = parse_alert("SOLD 1/2 0DTE SPY 759c @ 1.2 -10% cut")
    assert c.gain_pct == -10.0
    d = parse_alert("BOUGHT 0DTE SPX 7645c @ .65 tiny size")
    assert d.gain_pct is None


def test_gain_from_unsigned_percentages():
    from trader.trading.parser import parse_alert

    a = parse_alert(
        "ALL OUT 09/18 SPY 753c @ 6.22 exiting swing runners here for 120%"
    )
    assert a.gain_pct == 120
    b = parse_alert("SOLD half XYZ at 45% - trimming here")
    assert b.gain_pct == 45
    # a tickerless sell no longer parses (SOLD used to be
    # grabbed as the ticker itself)
    assert parse_alert("SOLD half at 45%") is None
    # size percentages ("2% budget") and other numbers stay ignored
    c = parse_alert("BOUGHT 09/25 ARM 300c @ 1.65 small size")
    assert c.gain_pct is None
