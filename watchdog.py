"""Bridge delayed cron starts with a bounded five-minute runner heartbeat."""
import json
import os
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo('Europe/Moscow')


def within_session(now):
    local = now.astimezone(MSK)
    return local.weekday() < 5 and 540 <= local.hour*60+local.minute <= 1375


def decision(now, signals, runs):
    if not within_session(now):
        return 'OFF_HOURS'
    if any(r.get('head_branch') == 'main' and r.get('status') != 'completed' for r in runs):
        return 'ACTIVE'
    try:
        age = (now-datetime.fromisoformat(signals['generated_msk'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 'DISPATCH'
    if age < -60 or age >= 240 or signals.get('stage_a',{}).get('health',{}).get('status') == 'STALE':
        return 'DISPATCH'
    return 'FRESH'


def gh(*args):
    return subprocess.check_output(['gh',*args],text=True,timeout=60)


def run(max_seconds=7200):
    repo = os.environ['GH_REPO']
    deadline = time.monotonic()+max_seconds
    while time.monotonic() < deadline:
        now = datetime.now(MSK)
        if not within_session(now):
            print(json.dumps({'time':now.isoformat(),'decision':'OFF_HOURS'}),flush=True)
            return
        try:
            runs = json.loads(gh('api',f'repos/{repo}/actions/workflows/mxz6.yml/runs?per_page=15'))['workflow_runs']
            raw = gh('api',f'repos/{repo}/contents/data/signals.json?ref=main','-H','Accept: application/vnd.github.raw+json')
            signals = json.loads(raw)
            result = decision(now,signals,runs)
            if result == 'DISPATCH':
                gh('workflow','run','mxz6.yml','--repo',repo,'--ref','main')
            print(json.dumps({'time':now.isoformat(),'decision':result,'last_success':signals.get('generated_msk')}),flush=True)
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            print(json.dumps({'time':now.isoformat(),'decision':'WATCHDOG_API_ERROR','error':str(error)}),flush=True)
        # Align to wall-clock five-minute boundaries. A fresh main checkout is
        # performed by each child; this worker never writes state or the journal.
        next_tick = (int(time.time())//300+1)*300
        while time.time() < next_tick and time.monotonic() < deadline:
            time.sleep(min(60,max(0,next_tick-time.time())))


if __name__ == '__main__':
    run()
