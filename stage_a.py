"""Stage A: deterministic, causal SHADOW signals. No broker or order interfaces.

Indicators are supplied by mxz6_monitor.analyze and are never changed here.
All timestamps identify CLOSED bars; H1 is joined by its close, not its open.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MSK = ZoneInfo("Europe/Moscow")
VERSION = "stage-a-1"
STATES = ("WATCH", "ARMED", "CONFIRMED", "COVER", "TAKE PROFIT")
RULES = {"atr_period": 14, "min_rsi": 30, "max_lips_atr": 1.5,
         "stop_atr_buffer": 0.25, "target_r": 2, "tick_size": 25,
         "point_value_rub": 1, "virtual_contracts": 1, "armed_bars": 6}
MAX_CANDLE_AGE_MIN = 31  # ISS 16 + 5m bucket + 10m operational allowance


def in_schedule(now):
    t = pd.Timestamp(now).tz_convert(MSK)
    return t.weekday() < 5 and 9 * 60 <= t.hour * 60 + t.minute <= 22 * 60 + 55


def data_health(last_begin, now, source="moex"):
    end = pd.Timestamp(last_begin) + pd.Timedelta(minutes=5)
    age = (pd.Timestamp(now) - end).total_seconds() / 60
    working = in_schedule(now)
    stale = age > MAX_CANDLE_AGE_MIN or age < 0
    status = "OFF_HOURS" if not working else "STALE" if stale else "DELAYED" if source == "moex" else "CURRENT"
    return {"status": status, "last_5m_end_msk": end.isoformat(),
            "candle_end_age_minutes": round(age, 2), "stale_after_minutes": MAX_CANDLE_AGE_MIN,
            "expected_iss_delay_minutes": 16 if source == "moex" else None,
            "new_signals_allowed": working and not stale,
            "note": "ISS задержан. Возраст свечи и возраст расчёта — разные величины; при STALE новые сигналы блокируются."}


def atr14(bars):
    prev = bars.close.shift(1)
    tr = pd.concat([bars.high - bars.low, (bars.high - prev).abs(), (bars.low - prev).abs()], axis=1).max(axis=1)
    out = pd.Series(np.nan, index=bars.index)
    if len(tr) >= 14:
        out.iloc[13] = tr.iloc[:14].mean()
        for i in range(14, len(tr)):
            out.iloc[i] = (out.iloc[i-1] * 13 + tr.iloc[i]) / 14
    return out


def initial_state():
    return {"version": VERSION, "state": "WATCH", "last_bar": None,
            "armed": None, "virtual_position": None}


def load_journal(path):
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [e["id"] for e in events]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate transition IDs in journal")
    return events


def load_state(path, events):
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else initial_state()
    if state.get("version") != VERSION or state.get("state") not in STATES:
        raise ValueError("Incompatible Stage A state; explicit migration required")
    # Every transition carries its state. Recover journal-before-checkpoint crashes.
    if events:
        event_state = events[-1]["state_after"]
        if state["last_bar"] is None or event_state["last_bar"] > state["last_bar"]:
            state = json.loads(json.dumps(event_state))
    return state


def conditions(bar, h1):
    atr = float(bar.atr14)
    if not math.isfinite(atr) or atr <= 0 or h1 is None:
        return False, False, "WARMUP"
    required = [bar.rsi14, bar.lips, h1.close, h1.teeth]
    if not all(math.isfinite(float(x)) for x in required):
        return False, False, "WARMUP"
    no_chase = float(bar.rsi14) > RULES["min_rsi"] and abs(float(bar.close - bar.lips)) < RULES["max_lips_atr"] * atr
    h1_context = float(h1.close) < float(h1.teeth) and not bool(h1.bullish)
    weak5 = float(bar.close) < float(bar.lips) and (bool(bar.new_down_break) or float(bar.close) < float(bar.previous_low))
    return h1_context and weak5, no_chase, "READY" if no_chase else "NO_CHASE"


def transition(state, to, bar_time, observed, reason, events, details=None):
    previous = state["state"]
    state["state"] = to
    state["last_bar"] = bar_time
    key = f"{VERSION}|MXZ6|{bar_time}|{previous}|{to}|{reason}"
    event = {"id": hashlib.sha256(key.encode()).hexdigest()[:24], "version": VERSION,
             "instrument": "MXZ6", "bar_begin_msk": bar_time,
             "observed_msk": pd.Timestamp(observed).isoformat(), "from": previous, "to": to,
             "reason": reason, "mode": "shadow", "real_order": False,
             "details": details or {}, "state_after": json.loads(json.dumps(state))}
    if event["id"] not in {e["id"] for e in events}:
        events.append(event)


def advance(state, bar, stamp, h1, now, events, allow_entry):
    """At most one transition per closed 5m bar. Confirmation cannot share ARMED bar."""
    stamp = pd.Timestamp(stamp)
    iso = stamp.isoformat()
    if state["last_bar"] and iso <= state["last_bar"]:
        return
    position = state["virtual_position"]
    if position:
        # Stop first if both touched: conservative unknown intrabar ordering.
        exit_price = None
        if float(bar.high) >= position["stop"]:
            exit_price, to, reason = max(float(bar.open), position["stop"]), "COVER", "VIRTUAL_STOP"
        elif float(bar.low) <= position["target"]:
            exit_price, to, reason = position["target"], "TAKE PROFIT", "VIRTUAL_2R"
        elif h1 is not None and bool(h1.bullish) and float(bar.close) > float(bar.lips):
            exit_price, to, reason = float(bar.close), "COVER", "VIRTUAL_TREND_EXIT"
        if exit_price is not None:
            details = dict(position, exit=exit_price,
                           pnl_rub=round((position["entry"] - exit_price) * RULES["point_value_rub"], 2))
            state["virtual_position"] = None
            state["armed"] = None
            transition(state, to, iso, now, reason, events, details)
    elif state["state"] in ("COVER", "TAKE PROFIT"):
        transition(state, "WATCH", iso, now, "NEXT_BAR_RESET", events)
    else:
        setup, no_chase, gate = conditions(bar, h1)
        if state["state"] == "WATCH":
            if allow_entry and setup and no_chase:
                state["armed"] = {"bar": iso, "trigger": float(bar.low),
                                  "structural_high": float(bar.structural_high), "waited": 0}
                transition(state, "ARMED", iso, now, "H1_CONTEXT_5M_WEAKNESS", events)
        elif state["state"] == "ARMED":
            armed = state["armed"]
            armed["waited"] += 1
            h1_ok = h1 is not None and float(h1.close) < float(h1.teeth) and not bool(h1.bullish)
            if not allow_entry or not no_chase or not h1_ok or armed["waited"] > RULES["armed_bars"] or float(bar.close) > armed["structural_high"]:
                state["armed"] = None
                transition(state, "WATCH", iso, now, "STALE_OR_INVALIDATED" if not allow_entry else gate if not no_chase else "SETUP_EXPIRED_OR_INVALIDATED", events)
            elif iso > armed["bar"] and float(bar.close) < armed["trigger"] and float(bar.close) < float(bar.lips):
                tick = RULES["tick_size"]
                entry = math.floor(float(bar.close) / tick) * tick
                stop = math.ceil((max(armed["structural_high"], float(bar.high)) + RULES["stop_atr_buffer"] * float(bar.atr14)) / tick) * tick
                risk = stop - entry
                if risk > 0:
                    position = {"entry": entry, "stop": stop, "risk_points": risk,
                                "risk_rub_1_contract": risk * RULES["point_value_rub"],
                                "target": entry - RULES["target_r"] * risk,
                                "contracts": 1, "confirmed_bar": iso,
                                "stop_buffer_points": round(RULES["stop_atr_buffer"] * float(bar.atr14), 4)}
                    state["virtual_position"] = position
                    state["armed"] = None
                    transition(state, "CONFIRMED", iso, now, "SEPARATE_CLOSED_5M_BREAK", events, position)
    state["last_bar"] = iso


def prepared_frames(dataset):
    bars = dataset["5m"].copy()
    bars["atr14"] = atr14(bars)
    bars["previous_low"] = bars.low.shift(1)
    bars["structural_high"] = bars.high.rolling(3, min_periods=3).max()
    return bars, dataset["H1"]


def run_engine(dataset, now, state, events, *, source="moex", backtest=False):
    bars, hours = prepared_frames(dataset)
    # CSV inputs are not trusted to contain only finished candles.
    bars = bars.loc[bars.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
    hours = hours.loc[hours.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)]
    if bars.empty:
        raise ValueError("No completed 5m bars for Stage A")
    health = data_health(bars.index[-1], now, source)
    if state["last_bar"] is None and not backtest:
        # Deployment must not invent historical virtual trades.
        transition(state, "WATCH", bars.index[-1].isoformat(), now, "BOOTSTRAP_NO_REPLAY", events)
    else:
        if state["last_bar"] and pd.Timestamp(state["last_bar"]) < bars.index[0] - pd.Timedelta(minutes=5):
            raise ValueError("Stage A history gap: cannot safely reconstruct missed bars")
        for stamp, bar in bars.iterrows():
            if state["last_bar"] and stamp <= pd.Timestamp(state["last_bar"]):
                continue
            closed = stamp + pd.Timedelta(minutes=5)
            known_h1 = hours.loc[hours.index + pd.Timedelta(hours=1) <= closed]
            h1 = known_h1.iloc[-1] if len(known_h1) else None
            h1_fresh = len(known_h1) and closed - (known_h1.index[-1] + pd.Timedelta(hours=1)) < pd.Timedelta(hours=2)
            age = (pd.Timestamp(now) - closed).total_seconds() / 60
            allowed = bool(h1_fresh) and (backtest or (health["new_signals_allowed"] and 0 <= age <= MAX_CANDLE_AGE_MIN))
            advance(state, bar, stamp, h1, now, events, allowed)
    _, no_chase, gate = conditions(bars.iloc[-1], hours.iloc[-1] if len(hours) else None)
    return {"version": VERSION, "mode": "shadow", "state": state["state"],
            "real_orders_enabled": False, "portfolio_mutations_enabled": False,
            "new_signals_allowed": health["new_signals_allowed"],
            "no_chase_passed": no_chase, "entry_gate": gate,
            "rules": RULES, "health": health, "armed": state["armed"],
            "virtual_position": state["virtual_position"],
            "last_processed_bar": state["last_bar"], "transition_count": len(events),
            "recent_transitions": [{k: v for k, v in e.items() if k != "state_after"} for e in events[-12:]],
            "execution_note": "Только виртуальная модель по задержанным закрытым свечам. Фактических заявок и изменений портфеля нет; риск без комиссий и проскальзывания."}


def persist(out, state, events):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    # Journal first; load_state can recover if the checkpoint write is interrupted.
    for filename, value in [("transitions.jsonl", "".join(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n" for e in events)),
                            ("stage_a_state.json", json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n")]:
        temp = out / (filename + ".tmp")
        temp.write_text(value, encoding="utf-8")
        temp.replace(out / filename)


def validate_shadow(signals):
    stage = signals["stage_a"]
    assert stage["version"] == VERSION and stage["mode"] == "shadow"
    assert stage["real_orders_enabled"] is False and stage["portfolio_mutations_enabled"] is False
    assert stage["state"] in STATES
    assert all(e["mode"] == "shadow" and e["real_order"] is False for e in stage["recent_transitions"])


def backtest(dataset, now):
    """Fixed-rule walk-forward replay, not parameter search or an execution claim."""
    state, events = initial_state(), []
    run_engine(dataset, now, state, events, backtest=True)
    exits = [e for e in events if e["to"] in ("COVER", "TAKE PROFIT")]
    return {"version": VERSION, "mode": "shadow", "fixed_rules": RULES,
            "start_msk": dataset["5m"].index[0].isoformat(),
            "end_msk": dataset["5m"].index[-1].isoformat(),
            "bars": len(dataset["5m"]), "events": len(events),
            "confirmed": sum(e["to"] == "CONFIRMED" for e in events),
            "closed_virtual_trades": len(exits),
            "virtual_pnl_rub_before_costs": sum(e["details"]["pnl_rub"] for e in exits),
            "open_virtual_position": state["virtual_position"],
            "limitations": "Пятидневное окно 5m с прогревом H1; без подгонки по датам. Вход по закрытию подтверждения, затем стоп/2R. При двойном касании стоп первый, гэп против шорта по open. Комиссии, ликвидность и проскальзывание не моделируются. Не доказательство доходности."}
