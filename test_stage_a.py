"""Behavior and failure tests; all prices synthetic, never execution instructions."""
import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from stage_a import (MSK, RULES, advance, initial_state, run_engine, conditions,
                     data_health, persist, load_journal, load_state, backtest, validate_shadow)
from mxz6_monitor import analyze
from tests import fake_prices
from dashboard import page


def bar(**changes):
    b = dict(open=100250, high=100300, low=100150, close=100200, lips=100400,
             teeth=100500, jaw=100600, atr14=250, rsi14=45,
             previous_low=100250, structural_high=100600, new_down_break=False)
    b.update(changes)
    return pd.Series(b)


def h1(**changes):
    b = dict(close=100300, teeth=100500, bullish=False)
    b.update(changes)
    return pd.Series(b)


T = pd.Timestamp('2026-11-18T12:00:00', tz=MSK)


class StageATests(unittest.TestCase):
    def setUp(self):
        self.state, self.events = initial_state(), []

    def step(self, n, b=None, hour=None, allowed=True):
        advance(self.state, bar() if b is None else b, T+pd.Timedelta(minutes=5*n),
                h1() if hour is None else hour, T+pd.Timedelta(minutes=40), self.events, allowed)

    def confirm(self):
        self.step(0)
        self.step(1,bar(close=100100,low=100050,high=100225))
        self.assertEqual(self.state['state'],'CONFIRMED')

    def test_separate_candle_and_repeat_dedup(self):
        self.step(0)
        self.assertEqual(self.state['state'],'ARMED')
        self.step(0,bar(close=100000))
        self.assertEqual(self.state['state'],'ARMED')
        self.assertEqual(len(self.events),1)
        self.step(1,bar(close=100100,low=100050))
        self.assertEqual(self.state['state'],'CONFIRMED')
        self.assertEqual(len(self.events),2)

    def test_risk_buffer_tick_and_2r(self):
        self.confirm()
        p=self.state['virtual_position']
        self.assertEqual(p['stop'],100675)
        self.assertEqual(p['risk_rub_1_contract'],575)
        self.assertEqual(p['entry']-p['target'],2*p['risk_points'])
        self.assertEqual(p['stop']%25,0)

    def test_no_chase_boundaries(self):
        for b in [bar(rsi14=30),bar(close=100025,lips=100400,atr14=250),bar(rsi14=10)]:
            self.assertFalse(conditions(b,h1())[1])
        self.step(0,bar(rsi14=30))
        self.assertEqual(self.state['state'],'WATCH')

    def test_stale_and_off_hours_block_entry(self):
        self.step(0,allowed=False)
        self.assertEqual(len(self.events),0)
        self.step(1)
        self.step(2,allowed=False)
        self.assertEqual(self.state['state'],'WATCH')
        self.assertIsNone(self.state['armed'])

    def test_stop_first_and_gap(self):
        self.confirm();p=self.state['virtual_position'].copy()
        self.step(2,bar(high=p['stop']+500,low=p['target']-500,open=p['stop']+100))
        self.assertEqual(self.state['state'],'COVER')
        self.assertEqual(self.events[-1]['details']['exit'],p['stop']+100)
        self.assertIsNone(self.state['virtual_position'])

    def test_take_profit_then_reset_on_next_bar(self):
        self.confirm();p=self.state['virtual_position'].copy()
        self.step(2,bar(high=p['entry'],low=p['target']-25))
        self.assertEqual(self.state['state'],'TAKE PROFIT')
        self.assertEqual(self.events[-1]['details']['pnl_rub'],2*p['risk_points'])
        self.step(3)
        self.assertEqual(self.state['state'],'WATCH')

    def test_h1_invalidation(self):
        self.step(0)
        self.step(1,bar(close=100100),h1(bullish=True))
        self.assertEqual(self.state['state'],'WATCH')

    def test_armed_expires(self):
        self.step(0)
        for n in range(1,8):self.step(n)
        self.assertEqual(self.state['state'],'WATCH')

    def test_persist_restart_and_journal_crash_recovery(self):
        self.confirm()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);persist(root,self.state,self.events)
            loaded=load_journal(root/'transitions.jsonl')
            self.assertEqual(load_state(root/'stage_a_state.json',loaded),self.state)
            (root/'stage_a_state.json').unlink()
            recovered=load_state(root/'stage_a_state.json',loaded)
            recovered['virtual_position']['stop']=999
            self.assertNotEqual(loaded[-1]['state_after']['virtual_position']['stop'],999)
            recovered=load_state(root/'stage_a_state.json',loaded)
            before=len(loaded)
            advance(recovered,bar(),T+pd.Timedelta(minutes=5),h1(),T,loaded,True)
            self.assertEqual(len(loaded),before)

    def test_corrupt_journal_fails_closed(self):
        self.step(0)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'transitions.jsonl'
            p.write_text(json.dumps(self.events[0])+'\n'+json.dumps(self.events[0])+'\n')
            with self.assertRaises(ValueError):load_journal(p)

    def dataset(self):
        a=analyze(fake_prices(120))
        hour=analyze(fake_prices(120).set_axis(pd.date_range('2026-10-19 00:00',periods=120,freq='h',tz=MSK)))
        return {'5m':a,'H1':hour,'H4':hour}

    def test_bootstrap_no_retroactive_trades(self):
        d=self.dataset();now=d['5m'].index[-1]+pd.Timedelta(minutes=21)
        stage=run_engine(d,now,self.state,self.events)
        self.assertEqual(stage['state'],'WATCH')
        self.assertEqual(len(self.events),1)
        self.assertEqual(self.events[0]['reason'],'BOOTSTRAP_NO_REPLAY')
        run_engine(d,now,self.state,self.events)
        self.assertEqual(len(self.events),1)

    def test_future_h1_not_used_and_prefix_invariance(self):
        d=self.dataset();cut=d['5m'].index[75]+pd.Timedelta(minutes=5)
        s1,e1=initial_state(),[]
        run_engine(d,cut,s1,e1,backtest=True)
        d2={k:v.copy() for k,v in d.items()}
        for key in ('H1','5m'):
            width=pd.Timedelta(hours=1) if key=='H1' else pd.Timedelta(minutes=5)
            mask=d2[key].index+width>cut
            d2[key].loc[mask,'close']=999999
            d2[key].loc[mask,'bullish']=True
        s2,e2=initial_state(),[]
        run_engine(d2,cut,s2,e2,backtest=True)
        self.assertEqual(s1,s2);self.assertEqual(e1,e2)

    def causal_setup(self):
        idx=pd.date_range(T-pd.Timedelta(minutes=150),T+pd.Timedelta(minutes=5),freq='5min')
        bars=pd.DataFrame([dict(bar(open=100300,close=100300,low=100250,high=100600)) for _ in idx],index=idx)
        bars.loc[T,['close','low']]=[100200,100150]
        bars.loc[T+pd.Timedelta(minutes=5),['close','low']]=[100100,100050]
        hours=pd.DataFrame([dict(h1()),dict(h1()),dict(h1(close=200000,bullish=True))],index=[T-pd.Timedelta(hours=2),T-pd.Timedelta(hours=1),T])
        return {'5m':bars,'H1':hours,'H4':hours}

    def test_unclosed_h1_cannot_veto_real_setup(self):
        data=self.causal_setup();now=T+pd.Timedelta(minutes=10)
        run_engine(data,now,self.state,self.events,backtest=True)
        self.assertEqual([e['to'] for e in self.events],['ARMED','CONFIRMED'])
        self.assertEqual(self.events[-1]['bar_begin_msk'],(T+pd.Timedelta(minutes=5)).isoformat())

    def test_batch_vs_incremental_replay(self):
        data=self.causal_setup();s2,e2=initial_state(),[]
        run_engine(data,T+pd.Timedelta(minutes=10),self.state,self.events,backtest=True)
        for minutes in range(-145,11,5):
            run_engine(data,T+pd.Timedelta(minutes=minutes),s2,e2,backtest=True)
        self.assertEqual(self.state,s2)
        self.assertEqual([(e['id'],e['to']) for e in self.events],[(e['id'],e['to']) for e in e2])

    def test_history_gap_fails_closed(self):
        d=self.dataset();self.state['last_bar']='2026-01-01T00:00:00+03:00'
        with self.assertRaisesRegex(ValueError,'history gap'):
            run_engine(d,T,self.state,self.events)

    def test_source_age_is_candle_end_not_generation(self):
        now=pd.Timestamp('2026-11-18T12:30:00',tz=MSK)
        health=data_health(now-pd.Timedelta(minutes=25),now)
        self.assertEqual(health['status'],'DELAYED')
        self.assertEqual(health['candle_end_age_minutes'],20)
        self.assertEqual(data_health(now-pd.Timedelta(minutes=45),now)['status'],'STALE')
        self.assertEqual(data_health(now,now)['status'],'STALE')
        self.assertEqual(data_health(now,now+pd.Timedelta(days=3))['status'],'OFF_HOURS')

    def test_backtest_rules_fixed_no_date_overrides(self):
        d=self.dataset();out=backtest(d,T)
        self.assertEqual(out['fixed_rules'],RULES)
        self.assertEqual(out['mode'],'shadow')
        self.assertIn('closed_virtual_trades',out)

    def test_safety_contract_and_no_network_in_engine(self):
        d=self.dataset();stage=run_engine(d,T,self.state,self.events)
        validate_shadow({'stage_a':stage})
        stage['real_orders_enabled']=True
        with self.assertRaises(AssertionError):validate_shadow({'stage_a':stage})
        tree=ast.parse(Path('stage_a.py').read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]+[a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
        self.assertFalse(set(imports)&{'requests','httpx','socket','subprocess','tinkoff'})

    def test_dashboard_has_shadow_source_and_journal(self):
        signals=json.loads(Path('data/signals.json').read_text())
        signals['stage_a']=run_engine(self.dataset(),T,self.state,self.events)
        rendered=page(signals)
        for text in ('Этап A · SHADOW','transitions.jsonl','source-health','Свечи источника устарели','ARMED','CONFIRMED','TAKE PROFIT'):
            self.assertIn(text,rendered)


if __name__=='__main__':unittest.main(verbosity=2)
