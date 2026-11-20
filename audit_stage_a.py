"""Reproducible fixed-rule audit. Separate from operational updates and journal."""
import argparse
import base64
import copy
import gzip
import hashlib
import io
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from mxz6_monitor import analyze, assert_ohlcv
from stage_a import RULES, conditions, initial_state, prepared_frames, run_engine


def compact(event):
    return {k:v for k,v in event.items() if k != 'state_after'}


def audit(frames, now, live, first_date='2026-11-19'):
    data={key:analyze(value) for key,value in frames.items()}
    bars,hours=prepared_frames(data)
    state,events=initial_state(),[]
    run_engine(data,now,state,events,backtest=True)
    selected=[e for e in events if e['bar_begin_msk'][:10] >= first_date]
    setups=[]
    for index,event in enumerate(selected):
        if event['to'] != 'ARMED':
            continue
        outcome=next((e for e in selected[index+1:] if e['from']=='ARMED'),None)
        armed=event['state_after']['armed']
        stop=pd.Timestamp(outcome['bar_begin_msk']) if outcome else bars.index[-1]
        evidence=[]
        for stamp,bar in bars.loc[(bars.index>pd.Timestamp(armed['bar'])) & (bars.index<=stop)].iterrows():
            known=hours.loc[hours.index+pd.Timedelta(hours=1)<=stamp+pd.Timedelta(minutes=5)]
            setup,no_chase,gate=conditions(bar,known.iloc[-1] if len(known) else None)
            evidence.append({'bar':stamp.isoformat(),'close':float(bar.close),
                             'break_below_trigger':bool(bar.close<armed['trigger']),
                             'below_lips':bool(bar.close<bar.lips),'rsi14':round(float(bar.rsi14),4),
                             'lips_distance_atr':round(abs(float(bar.close-bar.lips))/float(bar.atr14),4),
                             'no_chase_passed':no_chase})
        setups.append({'armed_bar':armed['bar'],'trigger':armed['trigger'],
                       'outcome':compact(outcome) if outcome else 'PENDING_AT_AUDIT_END',
                       'subsequent_closed_bars':evidence})
    boot=next((e for e in live if e['reason']=='BOOTSTRAP_NO_REPLAY'),None)
    deployment=[]
    if boot:
        ds=copy.deepcopy(boot['state_after']);deployment=[copy.deepcopy(boot)]
        run_engine(data,now,ds,deployment)
    original=[]
    for index,event in enumerate(live):
        if event['to'] != 'ARMED':
            continue
        actual=next((e for e in live[index+1:] if e['from']=='ARMED'),None)
        ds=copy.deepcopy(event['state_after']);de=[]
        run_engine(data,now,ds,de)
        corrected=next((e for e in de if e['from']=='ARMED'),None)
        original.append({'armed_bar':event['bar_begin_msk'],
                         'recorded_outcome':compact(actual) if actual else None,
                         'recorded_candle_age_at_observation_minutes':round((pd.Timestamp(actual['observed_msk'])-pd.Timestamp(actual['bar_begin_msk'])-pd.Timedelta(minutes=5)).total_seconds()/60,3) if actual else None,
                         'causal_outcome':compact(corrected) if corrected else None})
    filters=Counter()
    for stamp,bar in bars.loc[bars.index.strftime('%Y-%m-%d')>=first_date].iterrows():
        known=hours.loc[hours.index+pd.Timedelta(hours=1)<=stamp+pd.Timedelta(minutes=5)]
        setup,no_chase,gate=conditions(bar,known.iloc[-1] if len(known) else None)
        if setup:
            filters['h1_and_5m_candidates']+=1
            if no_chase: filters['passed_no_chase']+=1
            else:
                if float(bar.rsi14)<=RULES['min_rsi']:filters['blocked_rsi']+=1
                if abs(float(bar.close-bar.lips))>=RULES['max_lips_atr']*float(bar.atr14):filters['blocked_lips_distance']+=1
    snapshot={}
    for key,frame in frames.items():
        raw=frame.to_csv(index_label='begin').encode()
        snapshot[key]={'sha256':hashlib.sha256(raw).hexdigest(),
                       'gzip_base64_csv':base64.b64encode(gzip.compress(raw,mtime=0)).decode()}
    return {'as_of_msk':pd.Timestamp(now).isoformat(),'first_date':first_date,'mode':'shadow',
            'fixed_rules':RULES,'strategy_thresholds_changed':False,
            'walk_forward_counts':dict(Counter(e['to'] for e in selected)),
            'walk_forward_armed_cases':setups,'no_chase_candidate_statistics':dict(filters),
            'since_deployment_causal_events':[compact(e) for e in deployment],
            'original_live_armed_cases':original,'dataset_snapshot':snapshot,
            'original_live_journal':live,
            'limitations':'Reconstruction from the captured completed candles, not a live execution result. Original failing ISS responses were not archived. Intrabar order, fees and slippage are not known. The live journal and checkpoints are never rewritten by this audit.'}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--csv-dir',type=Path,default=Path('market'))
    p.add_argument('--snapshot',type=Path)
    p.add_argument('--as-of')
    p.add_argument('--journal',type=Path,default=Path('data/transitions.jsonl'))
    p.add_argument('--out',type=Path,default=Path('data/stage_a_audit.json'))
    args=p.parse_args()
    if args.snapshot:
        saved=json.loads(args.snapshot.read_text())
        frames={}
        for key,item in saved['dataset_snapshot'].items():
            raw=gzip.decompress(base64.b64decode(item['gzip_base64_csv']))
            assert hashlib.sha256(raw).hexdigest()==item['sha256']
            frames[key]=assert_ohlcv(pd.read_csv(io.BytesIO(raw)))
        now=pd.Timestamp(saved['as_of_msk']);live=saved['original_live_journal']
    else:
        frames={key:assert_ohlcv(pd.read_csv(args.csv_dir/(key+'.csv'))) for key in ('5m','H1','H4')}
        now=pd.Timestamp(args.as_of or (args.csv_dir/'fetched_msk.txt').read_text())
        live=[json.loads(line) for line in args.journal.read_text().splitlines() if line.strip()]
    result=audit(frames,now,live)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps({k:result[k] for k in ('as_of_msk','walk_forward_counts','no_chase_candidate_statistics')},ensure_ascii=False))


if __name__=='__main__':
    main()
