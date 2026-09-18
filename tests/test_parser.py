import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.parser import parse_alert


def test_basic_buy():
    a = parse_alert("BUY TSLA @ 250, SL 240, TP 270")
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "TSLA"
    assert a.entry == 250
    assert a.stop_loss == 240
    assert a.take_profit == 270


def test_dollar_ticker():
    a = parse_alert("🚀🚀 LONG $AAPL entry 180.5 stop 175 target 195")
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "AAPL"
    assert a.entry == 180.5
    assert a.stop_loss == 175
    assert a.take_profit == 195


def test_sell_signal():
    a = parse_alert("SELL / EXIT NVDA now")
    assert a is not None
    assert a.action == "SELL"
    assert a.ticker == "NVDA"


def test_no_action_returns_none():
    assert parse_alert("market looking choppy today, stay safe") is None


def test_no_ticker_returns_none():
    assert parse_alert("BUY the dip everyone!") is None


def test_ticker_not_action_word():
    a = parse_alert("BUY SHOP @ 100 CAD")
    assert a.ticker == "SHOP"


def test_stop_loss_word_not_sell_action():
    a = parse_alert("BUY AMD @ 150, stop loss 145, TP 165")
    assert a.action == "BUY"
    assert a.stop_loss == 145


def test_takes_profit_label_not_sell():
    a = parse_alert("long META take profit 500")
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "META"
    assert a.take_profit == 500


def test_custom_pattern_wins():
    pattern = r"(?P<action>BUY|SELL)\s+(?P<ticker>[A-Z]+)\s+IN\s+(?P<entry>\d+)"
    a = parse_alert("BUY SPY IN 450", custom_patterns=[pattern])
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "SPY"
    assert a.entry == 450


def test_hype_text():
    a = parse_alert("🚀🚀🚀 TSLA CALLS TO THE MOON, BUY NOW 🚀")
    assert a is not None
    assert a.action == "BUY"
    assert a.ticker == "TSLA"
