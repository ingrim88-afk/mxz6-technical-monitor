#!/usr/bin/env python3
"""Read-only MXZ6 technical monitor: MOEX ISS (delayed), T-Invest (authorized), or CSV.

Not a trading bot. No placing orders, positions or API write methods.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
MOEX_URL = (
    "https://iss.moex.com/iss/engines/futures/markets/forts/"
    "boards/rfud/securities/{ticker}/candles.json"
)
TBANK_URL = (
    "https://invest-public-api.tbank.ru/rest/"
    "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles"
)
INTERVALS = {"5m": "CANDLE_INTERVAL_5_MIN", "H1": "CANDLE_INTERVAL_HOUR", "H4": "CANDLE_INTERVAL_4_HOUR"}
REQUIRED_COLS = ["open", "high", "low", "close", "volume"]


def assert_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a price table with timezone-aware MSK candle BEGIN timestamps."""
    df = df.copy()
    if "begin" in df:
        df["begin"] = pd.to_datetime(df.pop("begin"), errors="coerce")
        df = df.set_index("begin")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Нет колонки begin или временного индекса")
    if df.index.tz is None:
        df.index = df.index.tz_localize(MSK)
    else:
        df.index = df.index.tz_convert(MSK)
    for c in REQUIRED_COLS:
        if c not in df.columns:
            raise ValueError(f"Отсутствует колонка {c}")
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_index()
    df = df.loc[~df.index.duplicated(keep="last")]
    df["volume"] = df["volume"].fillna(0)
    if len(df) and ((df.high < df.low) | (df.high < df[["open", "close"]].max(axis=1)) | (df.low > df[["open", "close"]].min(axis=1))).any():
        raise ValueError("Некорректные OHLC: high/low не покрывают open/close")
    return df[REQUIRED_COLS]


def http_json(session: requests.Session, url: str, *, params=None, headers=None, payload=None):
    for attempt in range(4):
        try:
            if payload is None:
                resp = session.get(url, params=params, headers=headers, timeout=16)
            else:
                resp = session.post(url, json=payload, headers=headers, timeout=16)
            if resp.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(0.5 * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 3:
                raise
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError("Не удалось получить ответ API")


def parse_iss_block(result: dict) -> list[dict]:
    block = result.get("candles")
    if not block or not isinstance(block, dict):
        raise ValueError("В ответе MOEX отсутствует блок candles; проверьте тикер или доступ")
    cols = block.get("columns", [])
    return [dict(zip(cols, row)) for row in block.get("data", [])]


def load_moex_interval(ticker: str, start: datetime, end: datetime, interval: int, session=None) -> pd.DataFrame:
    session = session or requests.Session()
    url = MOEX_URL.format(ticker=ticker.upper())
    offset = 0
    allrows = []
    page_marker = None
    for page_no in range(100):
        obj = http_json(session, url, params={
            "from": start.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S"),
            "till": end.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S"),
            "interval": interval,
            "start": offset,
            "iss.meta": "off",
        })
        rows = parse_iss_block(obj)
        if not rows:
            break
        new_marker = (rows[0].get("begin"), rows[-1].get("begin"))
        if new_marker == page_marker:
            raise RuntimeError("MOEX не переключил страницу истории — загрузка остановлена")
        page_marker = new_marker
        allrows.extend(rows)
        offset += len(rows)
    else:
        raise RuntimeError("MOEX: превышен лимит страниц, уменьшите окно истории")
    if not allrows:
        raise RuntimeError(f"MOEX не вернул свечи для {ticker}, interval={interval}")
    return assert_ohlcv(pd.DataFrame(allrows))


def ohlcv_resample(df: pd.DataFrame, rule: str, *, offset: str = "0h") -> pd.DataFrame:
    """Resample trade candles to a session-clock bucket; do not invent empty candles."""
    b = df.resample(rule, closed="left", label="left", origin="start_day", offset=offset)
    result = b.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return assert_ohlcv(result.dropna(subset=["open"]))


def remove_unfinished(df: pd.DataFrame, width: timedelta, cutoff: datetime) -> pd.DataFrame:
    """Do not evaluate buckets whose theoretical end is after verified-data cutoff."""
    return df.loc[df.index + width <= cutoff.astimezone(MSK)].copy()


def load_moex(ticker: str, now: datetime, delay_minutes: int = 16, h4_anchor: int = 1) -> dict[str, pd.DataFrame]:
    cutoff = now.astimezone(MSK) - timedelta(minutes=delay_minutes)
    with requests.Session() as session:
        minute = load_moex_interval(ticker, now - timedelta(days=5), now, 1, session)
        hour = load_moex_interval(ticker, now - timedelta(days=55), now, 60, session)
    bars5 = remove_unfinished(ohlcv_resample(minute, "5min"), timedelta(minutes=5), cutoff)
    bars1 = remove_unfinished(hour, timedelta(hours=1), cutoff)
    bars4 = remove_unfinished(ohlcv_resample(bars1, "4h", offset=f"{h4_anchor}h"), timedelta(hours=4), cutoff)
    return {"5m": bars5, "H1": bars1, "H4": bars4}


def quotation_to_float(q: dict) -> float:
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


def parse_tbank(result: dict) -> pd.DataFrame:
    rows = []
    for x in result.get("candles", []):
        if not x.get("isComplete", False):
            continue
        stamp = pd.to_datetime(x["time"], utc=True).tz_convert(MSK)
        rows.append({"begin": stamp, **{k: quotation_to_float(x[k]) for k in ("open", "high", "low", "close")}, "volume": x.get("volume", 0)})
    if not rows:
        return assert_ohlcv(pd.DataFrame(columns=["begin"] + REQUIRED_COLS))
    return assert_ohlcv(pd.DataFrame(rows))


def load_tbank(instrument: str, now: datetime, token: str) -> dict[str, pd.DataFrame]:
    if not token or len(token) < 16:
        raise RuntimeError("Токен T-Invest отсутствует: используйте INVEST_TOKEN в переменных окружения")
    with requests.Session() as session:
        output = {}
        for tf, interval in INTERVALS.items():
            total_days = 5 if tf == "5m" else 55
            chunk_days = 1 if tf == "5m" else 50
            start = now - timedelta(days=total_days)
            allbars = []
            while start < now:
                stop = min(now, start + timedelta(days=chunk_days))
                payload = {
                    "from": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "to": stop.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "interval": interval,
                    "instrumentId": instrument,
                    "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
                    "limit": 2400,
                }
                result = http_json(session, TBANK_URL, payload=payload, headers={"Authorization": "Bearer " + token})
                piece = parse_tbank(result)
                if len(piece):
                    allbars.append(piece)
                start = stop
            if not allbars:
                raise RuntimeError(f"T-Invest не вернул завершённых свечей {tf} для {instrument}")
            output[tf] = assert_ohlcv(pd.concat(allbars))
        return output


def load_csv_dir(folder: Path) -> dict[str, pd.DataFrame]:
    return {tf: assert_ohlcv(pd.read_csv(folder / (tf + ".csv"))) for tf in INTERVALS}


def smma(price: pd.Series, period: int) -> pd.Series:
    """Williams SMMA: initial SMA, then recursive smoothing."""
    a = price.astype(float).to_numpy()
    r = np.full(len(a), np.nan)
    if len(a) >= period:
        if np.isfinite(a[:period]).all():
            r[period - 1] = a[:period].mean()
        for i in range(period, len(a)):
            if np.isfinite(a[i]) and np.isfinite(r[i - 1]):
                r[i] = (r[i - 1] * (period - 1) + a[i]) / period
    return pd.Series(r, index=price.index)


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0).to_numpy(float)
    losses = (-delta.clip(upper=0)).to_numpy(float)
    rg = np.full(len(close), np.nan)
    rl = np.full(len(close), np.nan)
    if len(close) > period:
        rg[period] = np.nanmean(gains[1:period + 1])
        rl[period] = np.nanmean(losses[1:period + 1])
        for i in range(period + 1, len(close)):
            rg[i] = (rg[i - 1] * (period - 1) + gains[i]) / period
            rl[i] = (rl[i - 1] * (period - 1) + losses[i]) / period
    result = np.full(len(close), np.nan)
    good = np.isfinite(rg) & np.isfinite(rl)
    result[good] = 100 * rg[good] / (rg[good] + rl[good] + 1e-30)
    result[good & (rg == 0) & (rl == 0)] = 50
    return pd.Series(result, index=close.index)


def analyze(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 45:
        raise ValueError("Для устойчивого расчёта нужно минимум 45 завершённых свечей")
    df = assert_ohlcv(df)
    med = (df.high + df.low) / 2
    # Shift moves SMMA to the RIGHT on the plotted chart; values at t
    # belong to t-shift, which is known before candle t. No look-ahead.
    df["jaw"] = smma(med, 13).shift(8)
    df["teeth"] = smma(med, 8).shift(5)
    df["lips"] = smma(med, 5).shift(3)
    df["rsi14"] = rsi_wilder(df.close)
    hi = df.high
    lo = df.low
    top_pivot = (hi > hi.shift(1)) & (hi > hi.shift(2)) & (hi > hi.shift(-1)) & (hi > hi.shift(-2))
    low_pivot = (lo < lo.shift(1)) & (lo < lo.shift(2)) & (lo < lo.shift(-1)) & (lo < lo.shift(-2))
    # Mark a confirmed fractal only 2 FULL bars after its center.
    df["fractal_up_confirmed"] = hi.shift(2).where(top_pivot.shift(2, fill_value=False))
    df["fractal_down_confirmed"] = lo.shift(2).where(low_pivot.shift(2, fill_value=False))
    df["fractal_up_level"] = df.fractal_up_confirmed.ffill()
    df["fractal_down_level"] = df.fractal_down_confirmed.ffill()
    prior_lo = df.fractal_down_level.shift(1)
    prior_hi = df.fractal_up_level.shift(1)
    df["new_down_break"] = (df.close < prior_lo) & (df.close.shift(1) >= prior_lo)
    df["new_up_break"] = (df.close > prior_hi) & (df.close.shift(1) <= prior_hi)
    df["bearish"] = (df.lips < df.teeth) & (df.teeth < df.jaw) & (df.close < df.lips)
    df["bullish"] = (df.lips > df.teeth) & (df.teeth > df.jaw) & (df.close > df.lips)
    return df


def snapshot(tf: str, df: pd.DataFrame, now: datetime) -> dict:
    r = df.iloc[-1]
    last = df.index[-1]
    def val(k):
        x = r[k]
        return round(float(x), 2) if pd.notna(x) else None
    return {
        "timeframe": tf,
        "last_finished_candle_start_msk": last.isoformat(),
        "age_minutes": round((now.astimezone(MSK) - last).total_seconds() / 60),
        "close": val("close"), "volume": val("volume"), "rsi14": val("rsi14"),
        "jaw_blue": val("jaw"), "teeth_red": val("teeth"), "lips_green": val("lips"),
        "allig_bearish": bool(r.bearish), "allig_bullish": bool(r.bullish),
        "upper_fractal_level": val("fractal_up_level"),
        "lower_fractal_level": val("fractal_down_level"),
        "down_fractal_broken_this_candle": bool(r.new_down_break),
        "up_fractal_broken_this_candle": bool(r.new_up_break),
    }


def report_md(ticker: str, source: str, delay: str, snapshots: list[dict], now: datetime) -> str:
    lines = [f"# {ticker}: технический монитор", "", f"Дата формирования: {now.astimezone(MSK):%Y-%m-%d %H:%M:%S} МСК", f"Источник: {source}", f"Задержка/ограничение: {delay}", "", "| ТФ | Последняя закрытая свеча (МСК) | Close | RSI14 | Jaw | Teeth | Lips | Alligator | Верхний фрактал | Нижний фрактал | Пробой вверх | Пробой вниз |", "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|---|" ]
    for s in snapshots:
        st = "BEAR" if s["allig_bearish"] else "BULL" if s["allig_bullish"] else "NEUTRAL"
        fmt = lambda v: "—" if v is None else f"{v:.2f}"
        lines.append(f"| {s['timeframe']} | {s['last_finished_candle_start_msk'][11:16]} {s['last_finished_candle_start_msk'][:10]} | {fmt(s['close'])} | {fmt(s['rsi14'])} | {fmt(s['jaw_blue'])} | {fmt(s['teeth_red'])} | {fmt(s['lips_green'])} | {st} | {fmt(s['upper_fractal_level'])} | {fmt(s['lower_fractal_level'])} | {'ДА' if s['up_fractal_broken_this_candle'] else 'нет'} | {'ДА' if s['down_fractal_broken_this_candle'] else 'нет'} |")
    lines += ["", "## Дисциплина сигнала", "", "- Нет автоматического торгового приказа: фрактал сам по себе не команда на сделку.", "- Заходной импульс Эллиотта и качественный ABC-отскок требуют отдельной проверки структуры.", "- Цена, Alligator и фракталы оцениваются только по **завершённым** свечам.", "- Не путать уровни линий Alligator *с учётом сдвига при отображении* с несмещёнными SMMA.", "- Проверить ликвидность, реальную цену в брокере, новости, MOEX FUTOI и риск/прибыль перед решением о хедже.", "- История/смещение H4 может немного отличаться от настроек терминала; требует сверки.", "", "**Действие по умолчанию: новых сделок нет, решение принимает пользователь.**"]
    return "\n".join(lines) + "\n"


def plot_chart(df: pd.DataFrame, label: str, out: Path, ticker: str, source: str, limit: int = 110) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    d = df.tail(limit)
    x = np.arange(len(d))
    fig, (ax, vol, rsi_ax) = plt.subplots(3, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [4, 1, 1.3]}, sharex=True)
    for i, (t, r) in enumerate(d.iterrows()):
        c = "#2b997c" if r.close >= r.open else "#db5e65"
        ax.vlines(i, r.low, r.high, color=c, linewidth=1)
        body_low = min(r.open, r.close)
        ax.add_patch(Rectangle((i-0.34,body_low),0.68,max(0.01,abs(r.close-r.open)),facecolor=c,edgecolor=c))
        vol.bar(i,r.volume,color=c,width=0.8)
    for key,color in [("jaw","#4b94cb"),("teeth","#cc5b63"),("lips","#289e7b")]:
        ax.plot(x,d[key].values,label=key.upper(),color=color,linewidth=1.5)
    peaks=d.fractal_up_confirmed.dropna()
    troughs=d.fractal_down_confirmed.dropna()
    for s,mark,color in [(peaks,"v","#b47b29"),(troughs,"^","#b47b29")]:
        if len(s):
            # Pivot is two bars BACK relative to confirmation time.
            inds=np.array([d.index.get_loc(k)-2 for k in s.index],dtype=int)
            keep=(inds>=0)
            ax.scatter(inds[keep],s.to_numpy()[keep],s=30,marker=mark,c=color,zorder=10)
    ax.set_title(f"{ticker} · {label} · {source} · завершённые свечи, МСК",loc="left")
    ax.grid(alpha=0.15)
    ax.legend(loc="upper left",ncol=3,fontsize=9)
    vol.grid(alpha=0.12)
    sel=np.linspace(0,len(d)-1,min(9,len(d))).round().astype(int)
    rsi_ax.plot(x, d["rsi14"].values, color="#8b5cf6", linewidth=1.4)
    rsi_ax.axhline(70, color="#db5e65", linestyle="--", linewidth=0.8)
    rsi_ax.axhline(30, color="#2b997c", linestyle="--", linewidth=0.8)
    rsi_ax.set_ylim(0, 100)
    rsi_ax.set_ylabel("RSI 14")
    rsi_ax.grid(alpha=0.15)
    rsi_ax.set_xticks(sel)
    rsi_ax.set_xticklabels([d.index[i].strftime("%d.%m %H:%M") for i in sel],rotation=30,ha="right",fontsize=8)
    vol.set_ylabel("Volume")
    fig.tight_layout()
    fig.savefig(out,dpi=155)
    plt.close(fig)


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description="MXZ6 monitor: read-only candle analysis")
    p.add_argument("--source",choices=["moex","tbank","csv"],default="moex")
    p.add_argument("--ticker",default="MXZ6")
    p.add_argument("--instrument",default="MXZ6_SPBFUT",help="T-Invest: instrument ID or ticker_CLASS_CODE")
    p.add_argument("--csv-dir",type=Path,default=Path("."))
    p.add_argument("--out",type=Path,default=Path("report"))
    p.add_argument("--h4-anchor",choices=[0,1,2,3],type=int,default=1,help="MOEX H4: Moscow hours modulo 4, default 1 (09/13/17/21)")
    args=p.parse_args(argv)
    args.out.mkdir(parents=True,exist_ok=True)
    now=datetime.now(MSK)
    try:
        if args.source=="moex":
            dataset=load_moex(args.ticker,now,h4_anchor=args.h4_anchor)
            delay="Бесплатный ISS: 15+ минут. Отсекаем последние 16 минут и незакрытые свечи."
        elif args.source=="tbank":
            dataset=load_tbank(args.instrument,now,os.getenv("INVEST_TOKEN",""))
            delay="T-Invest API, только завершённые биржевые свечи; доступ зависит от тарифа/поставщика."
        else:
            dataset=load_csv_dir(args.csv_dir)
            delay="CSV из локального файла; актуальность зависит от времени экспорта."
        snapshots=[]
        for tf in INTERVALS:
            df=analyze(dataset[tf])
            if tf == "5m" and (now - df.index[-1].to_pydatetime() > timedelta(hours=12)):
                print("ВНИМАНИЕ: Последняя 5m свеча старше 12 часов. Данные устарели!",file=sys.stderr)
            snapshots.append(snapshot(tf,df,now))
            plot_chart(df,tf,args.out/f"{args.ticker}_{tf}.png",args.ticker,args.source)
        obj={"instrument":args.ticker,"source":args.source,"generated_msk":now.isoformat(),"delay":delay,"timeframes":snapshots}
        (args.out/"signals.json").write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")
        text=report_md(args.ticker,args.source,delay,snapshots,now)
        (args.out/"report.md").write_text(text,encoding="utf-8")
        print(text)
        print(f"Готово. Файлы в {args.out.resolve()}")
        return 0
    except Exception as e:
        print(f"НЕ УДАЛОСЬ ЗАГРУЗИТЬ/ОБРАБОТАТЬ ДАННЫЕ: {e}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
