"""Regression tests for ingestion, late runs, honest failure publication."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import mxz6_monitor as monitor
from operations import run
from watchdog import decision
from stage_a import initial_state, run_engine
import test_stage_a as fixtures
from test_stage_a import T, bar, h1


class WatchdogTests(unittest.TestCase):
    def test_recover_after_four_minutes_and_skip_active_main(self):
        now=datetime(2026,11,20,16,0,tzinfo=monitor.MSK)
        signals={'generated_msk':(now-timedelta(minutes=5)).isoformat()}
        self.assertEqual(decision(now,signals,[]),'DISPATCH')
        self.assertEqual(decision(now,signals,[{'head_branch':'main','status':'in_progress'}]),'ACTIVE')
        self.assertEqual(decision(now,{'generated_msk':now.isoformat()},[]),'FRESH')

    def test_no_dispatch_off_hours_and_recover_bad_timestamp(self):
        now=datetime(2026,11,20,16,0,tzinfo=monitor.MSK)
        self.assertEqual(decision(now,{},[]),'DISPATCH')
        self.assertEqual(decision(now+timedelta(hours=8),{},[]),'OFF_HOURS')


class IngestionTests(unittest.TestCase):
    def response(self, stamp):
        return {'candles':{'columns':['begin','open','high','low','close','volume'],
                'data':[['2026-11-20 15:00:00',100,110,90,105,10],
                        [stamp,100,95,90,105,10]]}}

    def test_unavailable_mutable_candle_is_cut_before_validation(self):
        end=datetime(2026,11,20,15,10,tzinfo=monitor.MSK)
        with patch.object(monitor,'http_json',side_effect=[self.response('2026-11-20 15:10:00'),{'candles':{'columns':[],'data':[]}}]):
            result=monitor.load_moex_interval('MXZ6',end-timedelta(days=1),end,1,object())
        self.assertEqual(len(result),1)

    def test_invalid_completed_candle_is_never_dropped_or_repaired(self):
        end=datetime(2026,11,20,15,10,tzinfo=monitor.MSK)
        with patch.object(monitor,'http_json',side_effect=[self.response('2026-11-20 15:05:00'),{'candles':{'columns':[],'data':[]}}]):
            with self.assertRaisesRegex(ValueError,'2026-11-20'):
                monitor.load_moex_interval('MXZ6',end-timedelta(days=1),end,1,object())


class PublicationTests(unittest.TestCase):
    def fixture(self, root):
        state=root/'data';out=root/'site';state.mkdir()
        signals=json.loads(Path('data/signals.json').read_text())
        (state/'signals.json').write_text(json.dumps(signals))
        (state/'transitions.jsonl').write_text('legacy-journal\n')
        (state/'stage_a_state.json').write_text('{"state":"WATCH"}')
        return state,out,signals

    def test_fetch_failure_retries_preserves_success_time_and_blocks_entries(self):
        with tempfile.TemporaryDirectory() as d:
            state,out,old=self.fixture(Path(d));calls=[]
            def fail(argv):
                calls.append(argv);root=Path(argv[argv.index('--out')+1])
                (root/'failure.json').write_text(json.dumps({'phase':'fetch','error':'Invalid OHLC'}))
                return 1
            code=run(out,state,calculator=fail,pause=lambda _:None)
            self.assertEqual(code,1);self.assertEqual(len(calls),3)
            published=json.loads((out/'signals.json').read_text())
            self.assertEqual(published['generated_msk'],old['generated_msk'])
            self.assertEqual(published['stage_a']['health']['status'],'STALE')
            self.assertFalse(published['stage_a']['new_signals_allowed'])
            self.assertEqual((out/'transitions.jsonl').read_text(),'legacy-journal\n')
            self.assertEqual(json.loads((state/'signals.json').read_text()),old)

    def test_state_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            state,out,old=self.fixture(Path(d));calls=[]
            def fail(argv):
                calls.append(argv);root=Path(argv[argv.index('--out')+1])
                (root/'failure.json').write_text('{"phase":"stage_a","error":"Corrupt journal"}')
                return 1
            self.assertEqual(run(out,state,calculator=fail,pause=lambda _:None),1)
            self.assertEqual(len(calls),1)

    def test_partial_attempt_is_not_reused_after_retry(self):
        with tempfile.TemporaryDirectory() as d:
            state,out,old=self.fixture(Path(d));calls=[]
            def calculate(argv):
                calls.append(argv);root=Path(argv[argv.index('--out')+1])
                if len(calls)==1:
                    (root/'leaked-partial.json').write_text('bad')
                    (root/'failure.json').write_text('{"phase":"fetch","error":"Timeout"}')
                    return 1
                old['stage_a']['health']['status']='DELAYED'
                (root/'signals.json').write_text(json.dumps(old))
                return 0
            self.assertEqual(run(out,state,calculator=calculate,pause=lambda _:None),0)
            self.assertFalse((out/'leaked-partial.json').exists())
            self.assertIn('--no-backtest',calls[-1])


class LateExecutionTests(unittest.TestCase):
    def test_late_run_replays_armed_and_confirmation_without_wall_clock_reset(self):
        data=fixtures.StageATests().causal_setup()
        first=initial_state();first['last_bar']=(T-pd.Timedelta(minutes=5)).isoformat()
        late=copy.deepcopy(first);a=[];b=[]
        run_engine(data,T+pd.Timedelta(minutes=30),first,a)
        result=run_engine(data,T+pd.Timedelta(minutes=100),late,b)
        self.assertEqual(first,late)
        self.assertEqual([e['to'] for e in b],['ARMED','CONFIRMED'])
        self.assertEqual([e['id'] for e in a],[e['id'] for e in b])
        self.assertFalse(result['new_signals_allowed'])
        self.assertEqual(result['entry_gate'],'STALE_SOURCE')
        count=len(b)
        run_engine(data,T+pd.Timedelta(minutes=101),late,b)
        self.assertEqual(len(b),count)

    def test_rsi_and_distance_invalidations_have_distinct_reasons(self):
        for candle,reason in [(bar(rsi14=30),'NO_CHASE_RSI'),
                              (bar(close=100000,lips=100400,atr14=250),'NO_CHASE_LIPS_DISTANCE')]:
            fixture=fixtures.StageATests();fixture.setUp();fixture.step(0);fixture.step(1,candle)
            self.assertEqual(fixture.events[-1]['reason'],reason)
            self.assertIn('break_below_trigger',fixture.events[-1]['details'])


if __name__=='__main__':
    unittest.main()
