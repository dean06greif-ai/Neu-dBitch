"""Regressionstests: Gebühren-Aufschlüsselung & globale Maker/Taker-Einstellung."""
import math

from core.utils import _enrich_trade
from services.bitunix_trade import effective_fee_percent


def make_trade(**over):
    t = {
        "id": "BTCUSDT-1", "symbol": "BTCUSDT", "side": "LONG", "mode": "paper",
        "status": "open", "entry": 100.0, "sl": 95.0, "initial_sl": 95.0,
        "tp1": 105.0, "tpf": 110.0, "qty": 1.0, "qty_remaining": 1.0,
        "leverage": 10, "max_capital": 10.0, "fee_percent": 0.06,
        "fees_paid": 0.06, "realized_pnl": -0.06,
        "opened_at": "2026-06-01T10:00:00+00:00",
    }
    t.update(over)
    return t


def test_entry_fee_is_fee_percent_of_notional():
    t = _enrich_trade(make_trade(), current_price=100.0)
    c = t["computed"]
    assert math.isclose(c["entry_fee"], 100.0 * 1.0 * 0.06 / 100)
    assert math.isclose(c["notional_usdt"], 100.0)


def test_open_trade_estimates_close_fee_and_total():
    t = _enrich_trade(make_trade(), current_price=102.0)
    c = t["computed"]
    assert math.isclose(c["est_close_fee"], 102.0 * 1.0 * 0.06 / 100)
    assert math.isclose(c["fees_total_est"], 0.06 + c["est_close_fee"])
    assert c["exit_fee"] is None


def test_closed_trade_splits_entry_and_exit_fee():
    t = make_trade(status="closed", exit_price=105.0, qty_remaining=0.0,
                   fees_paid=0.123, realized_pnl=4.877,
                   closed_at="2026-06-01T11:00:00+00:00", result="win")
    t = _enrich_trade(t)
    c = t["computed"]
    assert math.isclose(c["entry_fee"], 0.06)
    assert math.isclose(c["exit_fee"], 0.123 - 0.06)
    assert math.isclose(c["fees_total_est"], 0.123)


def test_price_move_pct_direction_adjusted():
    long_t = _enrich_trade(make_trade(), current_price=102.0)
    assert math.isclose(long_t["computed"]["price_move_pct"], 2.0)
    short_t = _enrich_trade(make_trade(side="SHORT"), current_price=98.0)
    assert math.isclose(short_t["computed"]["price_move_pct"], 2.0)


def test_pnl_pct_margin_includes_fees_upnl_excludes():
    # Kurs +1% auf 10x Hebel: uPnL = +10% auf Margin, PnL inkl. Fees darunter
    t = _enrich_trade(make_trade(), current_price=101.0)
    c = t["computed"]
    assert math.isclose(c["upnl_pct_margin"], 10.0, abs_tol=0.1)
    assert c["pnl_pct_margin"] < c["upnl_pct_margin"]


def test_effective_fee_uses_global_taker_setting(monkeypatch):
    from core import state
    monkeypatch.setitem(state.scanner.settings, "futures_taker_fee_pct", 0.045)
    # Default-Coin-Cfg (0.06) => globale Einstellung greift
    assert math.isclose(effective_fee_percent({"fee_percent": 0.06}), 0.045)
    # Explizit abweichende Coin-Einstellung hat Vorrang
    assert math.isclose(effective_fee_percent({"fee_percent": 0.1}), 0.1)


def test_effective_fee_default_without_setting(monkeypatch):
    from core import state
    monkeypatch.setitem(state.scanner.settings, "futures_taker_fee_pct", 0.06)
    assert math.isclose(effective_fee_percent({"fee_percent": 0.06}), 0.06)
