#!/usr/bin/env python3
"""Offline tests. All test values are SYNTHETIC, not market quotes."""
from datetime import datetime,timedelta
import unittest
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo
import tempfile
from pathlib import Path

from mxz6_monitor import (
    smma,rsi_wilder,analyze,parse_iss_block,parse_tbank,
    ohlcv_resample,assert_ohlcv,remove_unfinished,snapshot,plot_chart,report_md
)
MSK=ZoneInfo("Europe/Moscow")


def fake_prices(n=100):
    idx=pd.date_range("2026-10-20 09:00",periods=n,freq="5min",tz=MSK)
    close=np.array([100+0.11*i+2*np.sin(i/4) for i in range(n)])
    return pd.DataFrame({"open":close-0.3,"high":close+1,"low":close-1,"close":close,"volume":100},index=idx)


class TechTests(unittest.TestCase):
    def test_smma_flat(self):
        x=pd.Series([100.0]*30)
        y=smma(x,13)
        self.assertTrue(y.iloc[:12].isna().all())
        self.assertTrue(np.allclose(y.iloc[12:],100))

    def test_wilder_rsi_flat_and_up(self):
        x=pd.Series([100.0]*50)
        self.assertAlmostEqual(rsi_wilder(x).iloc[-1],50)
        y=pd.Series(np.arange(50.0))
        self.assertAlmostEqual(rsi_wilder(y).iloc[-1],100)

    def test_shift_uses_history_only(self):
        base=fake_prices(120)
        a=analyze(base)
        changed=base.copy()
        changed.loc[changed.index[-1],"high"]+=70
        changed.loc[changed.index[-1],"close"]+=50
        b=analyze(changed)
        # Today's plot values are based on t-3/5/8, not t.
        for line in ("jaw","teeth","lips"):
            self.assertEqual(float(a.iloc[-1][line]),float(b.iloc[-1][line]))
        # Earlier confirmed fractals can't depend on later changes
        pd.testing.assert_series_equal(a.fractal_down_confirmed.iloc[:-2],b.fractal_down_confirmed.iloc[:-2])

    def test_fractal_confirmation_delayed_two_bars(self):
        n=65
        base=fake_prices(n)
        # Make small oscillations disappear near index 52, isolated pivot at index 55.
        for i in range(48,65):
            base.iloc[i,base.columns.get_loc("high")]=200
            base.iloc[i,base.columns.get_loc("low")]=190
            base.iloc[i,base.columns.get_loc("open")]=195
            base.iloc[i,base.columns.get_loc("close")]=195
        base.iloc[55,base.columns.get_loc("high")]=205
        a=analyze(base)
        self.assertTrue(np.isnan(a.iloc[55].fractal_up_confirmed))
        self.assertTrue(np.isnan(a.iloc[56].fractal_up_confirmed))
        self.assertEqual(float(a.iloc[57].fractal_up_confirmed),205)

    def test_iss_parse_column_order(self):
        j={"candles":{"columns":["begin","close","high","low","open","volume"],"data":[["2026-10-20 15:00:00",100.2,100.4,99.9,100.0,50]]}}
        records=parse_iss_block(j)
        a=assert_ohlcv(pd.DataFrame(records))
        self.assertEqual(a.iloc[0].close,100.2)
        self.assertEqual(a.index[0].hour,15)

    def test_resample_and_cutoff(self):
        df=fake_prices(20)
        out=ohlcv_resample(df,"1h")
        self.assertAlmostEqual(out.iloc[0].high,max(df.high.iloc[:12]))
        cutoff=datetime(2026,10,20,10,3,tzinfo=MSK)
        finished=remove_unfinished(out,timedelta(hours=1),cutoff)
        self.assertEqual(len(finished),1)

    def test_tbank_parse_quotations(self):
        j={"candles":[{"open":{"units":"245","nano":125000000},"high":{"units":"246","nano":0},"low":{"units":"244","nano":0},"close":{"units":"245","nano":500000000},"volume":90,"time":"2026-10-20T12:00:00Z","isComplete":True}, {"open":{},"high":{},"low":{},"close":{},"volume":0,"time":"2026-10-20T12:05:00Z","isComplete":False}]}
        d=parse_tbank(j)
        self.assertEqual(len(d),1)
        self.assertEqual(d.index[0].hour,15)
        self.assertAlmostEqual(float(d.iloc[0].open),245.125)

    def test_end_to_end_offline(self):
        df=analyze(fake_prices(115))
        ss=snapshot("5m",df,datetime(2026,10,21,18,tzinfo=MSK))
        self.assertEqual(ss["timeframe"],"5m")
        self.assertIn("lower_fractal_level",ss)
        md=report_md("TEST", "SYNTHETIC", "DO NOT TRADE", [ss], datetime(2026,10,21,18,tzinfo=MSK))
        self.assertIn("TEST",md)
        with tempfile.TemporaryDirectory() as d:
            img=Path(d)/"chart.png"
            plot_chart(df,"5m",img,"TEST","SYNTHETIC",90)
            self.assertGreater(img.stat().st_size,20_000)

if __name__=="__main__":
    unittest.main(verbosity=2)
