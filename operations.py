"""Bounded operational retries and honest failure publication. No broker writes."""
import json
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from mxz6_monitor import main as calculate, MSK


def run(out=Path("site"), state_dir=Path("data"), *, calculator=calculate,
        clock=lambda: datetime.now(MSK), pause=time.sleep):
    out, state_dir = Path(out), Path(state_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Preserve separately computed audit output; never run a backtest on the hot path.
    for name in ("stage_a_backtest.json", "stage_a_audit.json"):
        if (state_dir/name).exists():
            shutil.copy2(state_dir/name, out/name)
    failure = None
    attempts = 0
    for attempt in range(3):
        attempts += 1
        with tempfile.TemporaryDirectory(prefix="mxz6-attempt-") as temp:
            root = Path(temp)
            result = calculator(["--source", "moex", "--ticker", "MXZ6", "--out", str(root),
                                 "--state-dir", str(state_dir), "--no-backtest"])
            if result == 0:
                signals = json.loads((root/"signals.json").read_text())
                for path in root.iterdir():
                    if path.is_file() and path.name != "failure.json":
                        shutil.copy2(path, out/path.name)
                status = signals["stage_a"]["health"]["status"]
                runtime = {"status": status, "attempted_msk": clock().isoformat(),
                           "last_successful_calculation_msk": signals["generated_msk"],
                           "attempts": attempts, "failure": None}
                (out/"runtime_status.json").write_text(json.dumps(runtime, ensure_ascii=False, indent=2))
                return 0 if status != "STALE" else 1
            failure_file = root/"failure.json"
            failure = json.loads(failure_file.read_text()) if failure_file.exists() else {"phase":"unknown","error":"Calculation failed"}
        # Never hide state/calculation errors with a retry or reuse partial state.
        if failure["phase"] != "fetch":
            break
        if attempt < 2:
            pause(2 ** attempt)
    old = json.loads((state_dir/"signals.json").read_text())
    for name in ("report.md", "transitions.jsonl", "stage_a_state.json"):
        if (state_dir/name).exists():
            shutil.copy2(state_dir/name, out/name)
    stage = old["stage_a"]
    stage["health"]["status"] = "STALE"
    stage["health"]["new_signals_allowed"] = False
    stage["new_signals_allowed"] = False
    stage["entry_gate"] = "CALCULATION_FAILED"
    runtime = {"status":"STALE", "attempted_msk":clock().isoformat(),
               "last_successful_calculation_msk":old["generated_msk"],
               "attempts": attempts, "failure":failure}
    old["runtime"] = runtime
    # Keep generated_msk and checkpoint at the last successful calculation.
    (out/"signals.json").write_text(json.dumps(old, ensure_ascii=False, indent=2))
    (out/"runtime_status.json").write_text(json.dumps(runtime, ensure_ascii=False, indent=2))
    print("STALE: last successful calculation", old["generated_msk"], "failure", failure)
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
