#!/usr/bin/env python3
"""Render static mobile-readable dashboard from historical finished-candle indicator snapshots.
No JS dependencies; displays no financial account information. Not a trade signal.
"""
import argparse
import html
import json
from pathlib import Path

LABELS = {'5m':'5 минут', 'H1':'1 час', 'H4':'4 часа'}

def esc(value):
    return html.escape(str(value))

def num(value):
    return '—' if value is None else f'{value:,.2f}'.replace(',', ' ')

def page(signals: dict) -> str:
    cards=[]
    instrument=esc(signals['instrument'])
    for item in signals['timeframes']:
        tf=item['timeframe']
        state='Медвежий порядок' if item['allig_bearish'] else 'Бычий порядок' if item['allig_bullish'] else 'Нет полного порядка'
        color='bear' if item['allig_bearish'] else 'bull' if item['allig_bullish'] else 'neutral'
        candle=esc(item['last_finished_candle_start_msk'].replace('T',' ')[:16])
        key=f'{esc(instrument)}_{esc(tf)}.png'
        rows=''.join('<tr><td>'+nm+'</td><td>'+esc(num(item[k]))+'</td></tr>' for nm,k in [
            ('Закрытие','close'),('RSI 14','rsi14'),('Jaw (синяя)','jaw_blue'),
            ('Teeth (красная)','teeth_red'),('Lips (зелёная)','lips_green'),
            ('Нижний фрактал','lower_fractal_level'),('Верхний фрактал','upper_fractal_level'),
        ])
        breakout='Да — пробой на последней закрытой свече' if item['down_fractal_broken_this_candle'] else 'Нет'
        cards.append(f'''<article class="card"><header><h2>{esc(LABELS.get(tf,tf))}</h2><span class="pill {color}">{state}</span></header>
        <p class="meta">Начало последней завершённой свечи: {candle} МСК</p>
        <table>{rows}</table><p class="warning">Пробой нижнего фрактала: <b>{esc(breakout)}</b></p>
        <a href="{key}"><img loading="lazy" src="{key}" alt="График {instrument} {esc(tf)}"/></a></article>''')
    generated=esc(signals['generated_msk'].replace('T',' ')[:19])
    source=esc(signals['source'])
    delay=esc(signals['delay'])
    return f'''<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <meta http-equiv="refresh" content="300"/>
    <meta name="robots" content="noindex,nofollow"/>
    <title>MXZ6 — Alligator, Fractals и RSI</title>
    <style>
    :root{{color-scheme:dark; font-family:system-ui,Roboto,-apple-system,sans-serif;background:#101823;color:#edf3fb}}
    *{{box-sizing:border-box}}body{{margin:0 auto;max-width:1000px;padding:16px 12px 48px}}
    h1{{font-size:clamp(22px,5vw,32px);margin:14px 0 3px}}h2{{font-size:19px;margin:0}}
    .lead,.meta,.footer{{color:#b2c2d3;font-size:13px;line-height:1.45}}
    .banner{{background:#222e40;border-left:4px solid #eda94f;border-radius:9px;padding:10px 13px;margin:15px 0}}
    .card{{background:#1a2535;border:1px solid #35455b;border-radius:12px;padding:14px;margin:12px 0;overflow:hidden}}
    header{{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}}
    .pill{{font-size:11px;border-radius:99px;padding:5px 9px;background:#515d70}}
    .bear{{background:#72363a;color:#fff}}.bull{{background:#205d4e;color:#fff}}.neutral{{background:#49526b}}
    table{{border-collapse:collapse;width:100%;margin-top:10px}}td{{font-size:14px;padding:6px 4px;border-bottom:1px solid #344258}}
    td:last-child{{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
    img{{width:100%;display:block;max-width:100%;margin-top:10px;border-radius:6px;background:#fff}}
    a{{color:#9ccaff}}.warning{{font-size:13px;color:#dfbd86}}
    </style></head><body><h1>{instrument} · индикаторный монитор</h1>
    <p class="lead">Сформировано: {generated} МСК · Источник: {source}</p>
    <div class="banner"><b>Данные задержанные, не для мгновенного входа.</b><p>{delay}</p>
    <p>Индикаторы не являются автоматической командой открыть хедж. Сверяйте брокерскую цену, H1/H4, объёмы, волны, геополитику и риск.</p></div>
    {''.join(cards)}
    <p class="footer">Подробные данные: <a href="signals.json">signals.json</a> · <a href="report.md">report.md</a>.
    Автообновление страницы каждые 5 минут. Расчёт запускается каждые 5 минут; GitHub Actions иногда стартует с задержкой. Время последнего отчёта указано выше.</p>
    </body></html>'''

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--signals',type=Path,default=Path('site/signals.json'))
    parser.add_argument('--out',type=Path,default=Path('site/index.html'))
    args=parser.parse_args()
    data=json.loads(args.signals.read_text(encoding='utf-8'))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(page(data),encoding='utf-8')
    print('Dashboard:',args.out)
if __name__=='__main__':main()
