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
        chart = (f'<a href="{key}"><img loading="lazy" src="{key}" alt="График {instrument} {esc(tf)}"/></a>'
                 if not signals.get('runtime',{}).get('failure') else '<p class="warning">График не обновлён из-за ошибки расчёта.</p>')
        cards.append(f'''<article class="card"><header><h2>{esc(LABELS.get(tf,tf))}</h2><span class="pill {color}">{state}</span></header>
        <p class="meta">Начало последней завершённой свечи: {candle} МСК</p>
        <table>{rows}</table><p class="warning">Пробой нижнего фрактала: <b>{esc(breakout)}</b></p>
        {chart}</article>''')
    generated=esc(signals['generated_msk'].replace('T',' ')[:19])
    source=esc(signals['source'])
    delay=esc(signals['delay'])
    stage = signals.get('stage_a', {})
    health = stage.get('health', {})
    position = stage.get('virtual_position')
    runtime = signals.get('runtime', {})
    runtime_failed = runtime.get('status') == 'STALE' and runtime.get('failure')
    failure_banner = ''
    if runtime_failed:
        failure_banner = '<div class="freshness stale"><b>STALE · ошибка обновления</b><p>Последний успешный расчёт: '+generated+' МСК</p><p>Последняя попытка: '+esc(runtime.get('attempted_msk'))+'</p><p>'+esc(runtime['failure'].get('phase'))+': '+esc(runtime['failure'].get('error'))+'</p></div>'
    risk = ''
    if position:
        risk = '<table>' + ''.join('<tr><td>'+label+'</td><td>'+esc(num(position[key]))+'</td></tr>' for label,key in [('Виртуальный вход','entry'),('Стоп','stop'),('Риск ₽ · 1 MIX','risk_rub_1_contract'),('Цель 2R','target')]) + '</table>'
    event_rows = ''.join('<tr><td>'+esc(e['bar_begin_msk'].replace('T',' ')[:16])+'</td><td>'+esc(e['to'])+'</td><td>'+esc(e['reason'])+'</td></tr>' for e in reversed(stage.get('recent_transitions', [])))
    legacy_note = ' Старые записи STALE_OR_INVALIDATED сохранены как история; их точные причины разобраны в аудите.' if any(e.get('reason') == 'STALE_OR_INVALIDATED' for e in stage.get('recent_transitions', [])) else ''
    audit_note = '<p class="meta">'+esc(stage.get('replay_note', ''))+' <a href="stage_a_audit.json">Исторический аудит 19–20.11</a>.'+esc(legacy_note)+'</p>'
    stage_card = f'<article class="card"><h2>Этап A · SHADOW · {esc(stage.get("state", "UNAVAILABLE"))}</h2><p>WATCH → ARMED → CONFIRMED · COVER / TAKE PROFIT</p><p class="warning">{esc(stage.get("execution_note", "Этап A недоступен"))}</p><p>Фильтр входа: {esc(stage.get("entry_gate", "—"))} · Переходов: {esc(stage.get("transition_count", 0))}</p>{risk}<div style="overflow-x:auto"><table>{event_rows}</table></div><p><a href="transitions.jsonl">Журнал переходов</a> · <a href="stage_a_backtest.json">Walk-forward: фиксированные правила</a></p></article>'
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
    .freshness{{margin:12px 0;padding:12px 14px;border-radius:9px;border:1px solid #64748b;font-weight:600;font-size:14px}}
    .freshness.ok{{background:#153e34;border-color:#33846d;color:#b9f1dd}}
    .freshness.stale{{background:#611f28;border:2px solid #ff626e;color:#fff}}
    .freshness.off{{background:#283443;border-color:#aab8c9;color:#d9e5f3}}
    .freshness.error{{background:#611f28;border:2px solid #ff626e;color:#fff}}
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
    {failure_banner}
    <div id="freshness" class="freshness" data-failed="{'true' if runtime_failed else 'false'}" data-generated="{esc(signals['generated_msk'])}" role="status" aria-live="polite">
      Проверяем время последнего расчёта...</div>
    <div id="source-health" class="freshness" data-candle-end="{esc(health.get('last_5m_end_msk', ''))}" data-limit="{esc(health.get('stale_after_minutes',31))}">Источник MOEX ISS: {esc(health.get('status','UNKNOWN'))}</div>
    <noscript><p class="freshness off">JavaScript выключен — автоматически оценить свежесть отчёта невозможно. Проверяйте время «Сформировано» вручную.</p></noscript>
    <script>
    (function() {{
      const box = document.getElementById('freshness');
      function updateFreshness() {{
        const generated = Date.parse(box.dataset.generated);
        if (!Number.isFinite(generated)) {{
          box.className = 'freshness error';
          box.textContent = '⚠ Не удалось определить время расчёта. Не используйте данные для торгового решения.';
          return;
        }}
        const sourceBox = document.getElementById('source-health');
        const candleAge = (Date.now() - Date.parse(sourceBox.dataset.candleEnd)) / 60000;
        const sourceStale = !Number.isFinite(candleAge) || candleAge > Number(sourceBox.dataset.limit) || candleAge < 0;
        sourceBox.className = sourceStale ? 'freshness stale' : 'freshness off';
        sourceBox.textContent = sourceStale
          ? 'STALE · Свечи источника устарели или недоступны. Новые сигналы не использовать. Возраст закрытия 5m: ' + (Number.isFinite(candleAge) ? Math.floor(candleAge) + ' мин.' : 'неизвестен')
          : 'MOEX ISS: задержанные свечи. Последняя 5m закрылась ' + Math.floor(candleAge) + ' мин назад; это не текущая цена.';
        const age = Math.max(0, Math.floor((Date.now() - generated) / 60000));
        const fmt = new Intl.DateTimeFormat('en-US', {{
          timeZone:'Europe/Moscow', weekday:'short', hour:'2-digit', minute:'2-digit', hour12:false
        }});
        const parts = Object.fromEntries(fmt.formatToParts(new Date())
          .filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
        const weekday = ['Mon','Tue','Wed','Thu','Fri'].includes(parts.weekday);
        const clock = (Number(parts.hour) % 24) * 60 + Number(parts.minute);
        const scheduled = weekday && clock >= 9*60 && clock <= 22*60+55;
        if (!scheduled && sourceStale) {{ sourceBox.className = 'freshness off'; sourceBox.textContent = 'Вне расписания: исторические свечи источника, новых сигналов нет.'; }}
        if (box.dataset.failed === 'true') {{
          box.className = 'freshness stale';
          box.textContent = 'STALE · обновление завершилось ошибкой. Последний успешный расчёт: {generated} МСК. Новых входов нет.';
        }} else if (age > 20) {{
          box.className = scheduled ? 'freshness stale' : 'freshness off';
          box.textContent = scheduled
            ? 'STALE · ДАННЫЕ УСТАРЕЛИ: последний успешный расчёт {generated} МСК (' + age + ' мин назад). Не используйте страницу для нового входа.'
            : 'Вне времени расчётов (будни 09:00–22:55 МСК): последнему обновлению ' + age + ' мин. Это исторические данные, не текущая котировка.';
        }} else {{
          box.className = 'freshness ok';
          box.textContent = '✓ Расчёт обновлён ' + age + ' мин назад. Котировки MOEX всё равно задержаны минимум на 15 минут.';
        }}
      }}
      updateFreshness();
      setInterval(updateFreshness, 60000);
      window.addEventListener('focus', updateFreshness);
    }})();
    </script>
    <div class="banner"><b>Данные задержанные, не для мгновенного входа.</b><p>{delay}</p>
    <p>Индикаторы не являются автоматической командой открыть хедж. Сверяйте брокерскую цену, H1/H4, объёмы, волны, геополитику и риск.</p></div>
    {stage_card}
    {audit_note}
    <p class="meta">Версия кода: {esc(signals.get('code_sha','unknown'))}</p>
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
    runtime_path = args.signals.parent/'runtime_status.json'
    if runtime_path.exists():
        data['runtime'] = json.loads(runtime_path.read_text(encoding='utf-8'))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(page(data),encoding='utf-8')
    print('Dashboard:',args.out)
if __name__=='__main__':main()
