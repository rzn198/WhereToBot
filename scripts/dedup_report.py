"""Отчёт по журналу решений дедупликатора: кто кого скрыл и почему, для ручной проверки.

Источник — logs/dedup_audit.jsonl и ротированные архивы dedup_audit.jsonl.N.gz (пишет сервис deduplicator).

    python scripts/dedup_report.py --run 12                      # решения одного прогона
    python scripts/dedup_report.py --from 2026-11-01 --to 2026-11-30
    python scripts/dedup_report.py --day 2026-11-14 --model deepseek-v4.1-flash
    python scripts/dedup_report.py --current                     # что дедуп считает дублями прямо сейчас (из БД)
    python scripts/dedup_report.py --log /путь/к/dedup_audit.jsonl --format csv

На сервере (CSV, без дополнительных пакетов):
    docker compose exec deduplicator python scripts/dedup_report.py --run 12 --format csv --out-dir /app/logs/reports
    docker compose cp deduplicator:/app/logs/reports ./reports

Excel строится, если установлен openpyxl (pip install openpyxl; в образе его нет — удобнее строить локально
по скачанному dedup_audit.jsonl: docker compose cp deduplicator:/app/logs/dedup_audit.jsonl .).

Листы/файлы: Группы (победитель первым, потом проигравшие и оставшиеся, полный текст), Решения, Оставлены,
Изменения, Прогоны, Ошибки; с --raw ещё Ответы ИИ.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

DEFAULT_LOG = HERE.parent / 'logs' / 'dedup_audit.jsonl'

VIA = {'ai_group': 'ИИ (группа)', 'exact_copy': 'точная копия проигравшего', 'same_source': 'копия того же источника',
       'merge': 'слияние групп (перешёл под нового победителя)'}
KEPT = {'digest': 'строка дайджеста', 'list': 'перечень событий', 'weak': 'слабое упоминание',
        'same_source': 'тот же источник, что у победителя', 'admin_locked': 'решение админа',
        'reverted_by_admin': 'админ вернул', 'no_winner': 'нет подходящего победителя', 'other': 'другое'}


# ------------------------------------------------------------------ чтение журнала

def log_files(path: Path) -> list[Path]:
    """Основной файл и ротированные архивы, от старых к новым."""
    base = str(path)
    rotated = sorted(glob.glob(base + '.*.gz'), key=lambda p: -int(p.rsplit('.', 2)[-2]) if p.rsplit('.', 2)[-2].isdigit() else 0)
    out = [Path(p) for p in rotated]
    if path.exists():
        out.append(path)
    return out


def iter_events(path: Path):
    for f in log_files(path):
        opener = gzip.open if f.suffix == '.gz' else open
        with opener(f, 'rt', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue                         # оборванная строка в конце файла


def select(events, *, run=None, day_from=None, day_to=None, model=None):
    for e in events:
        if run is not None and e.get('run') != run:
            continue
        d = e.get('day')
        if d and day_from and d < day_from:
            continue
        if d and day_to and d > day_to:
            continue
        if model:
            em = e.get('model') or e.get('models') or []
            if e['event'] in ('day', 'group', 'decision', 'ai_raw', 'bad_output') and model not in (
                    em if isinstance(em, list) else [em]):
                continue
        yield e


# ------------------------------------------------------------------ таблицы

def _src(r: dict) -> str:
    return r.get('name') or r.get('src') or ''


def build_tables(events: list[dict], with_raw: bool = False) -> dict[str, tuple[list[str], list[list]]]:
    t: dict[str, tuple[list[str], list[list]]] = {
        'Группы': (['Дата', 'Группа', 'Роль', 'Происхождение / причина', 'Тип', 'id', 'Источник', 'Ссылка', 'Текст',
                    'Причина (ИИ)', 'Модель', 'Прогон'], []),
        'Решения': (['Дата', 'Прогон', 'Группа', 'Происхождение', 'Статус', 'Новое', 'Модель',
                     'Победитель: источник', 'Победитель: id', 'Победитель: ссылка', 'Победитель: текст',
                     'Проигравший: источник', 'Проигравший: id', 'Проигравший: ссылка', 'Проигравший: текст',
                     'Причина'], []),
        'Оставлены': (['Дата', 'Прогон', 'Группа', 'Причина', 'Тип', 'id', 'Источник', 'Ссылка', 'Текст'], []),
        'Изменения': (['Дата', 'Прогон', 'Что изменилось', 'Прежний победитель', 'Проигравший (id)', 'Проигравший: текст',
                       'Новый победитель: источник', 'Новый победитель: текст', 'Прежняя модель', 'Прежняя причина'], []),
        'Прогоны': (['Прогон', 'Метод', 'Начало', 'Конец', 'Статус', 'Модели (дат на модель)', 'Дат всего',
                     'Дат обработано', 'Запросов', 'Токенов in', 'Токенов out', 'Групп', 'Скрыто', 'Раскрыто',
                     'Плохих дат', 'Секунд', 'Ошибка'], []),
        'Ошибки': (['Время', 'Прогон', 'Дата', 'Тип', 'Модель', 'Ошибка', 'Сырой ответ'], []),
    }
    if with_raw:
        t['Ответы ИИ'] = (['Дата', 'Прогон', 'Модели', 'Сырые ответы'], [])
    runs: dict = {}
    for e in events:
        ev = e['event']
        if ev == 'group':
            gid = e['group']
            for m in e['members']:
                what = (VIA.get(m.get('via'), m.get('via')) if m['role'] == 'loser' else
                        KEPT.get(m.get('reason'), m.get('reason')) if m['role'] == 'kept' else '')
                t['Группы'][1].append([e['day'], gid, {'winner': 'победитель', 'loser': 'скрыт', 'kept': 'оставлен'}[m['role']],
                                       what, 'пост' if m['kind'] == 'm' else 'событие', m['id'], _src(m), m.get('link') or '',
                                       m['text'], e.get('reason', ''), e.get('model', ''), e.get('run')])
        elif ev == 'decision':
            w, l = e['winner'], e['loser']
            t['Решения'][1].append([e['day'], e.get('run'), e.get('group') or '', VIA.get(e['via'], e['via']), e['status'],
                                    'да' if e.get('is_new') else 'нет', e.get('model', ''),
                                    _src(w), w['id'], w.get('link') or '', w['text'],
                                    _src(l), l['id'], l.get('link') or '', l['text'], e.get('reason', '')])
        elif ev == 'kept':
            r = e['record']
            t['Оставлены'][1].append([e['day'], e.get('run'), e.get('group') or '', KEPT.get(e['reason'], e['reason']),
                                      'пост' if r['kind'] == 'm' else 'событие', r['id'], _src(r), r.get('link') or '', r['text']])
        elif ev == 'change':
            p, l, nw = e['previous'], e.get('loser') or {}, e.get('new_winner') or {}
            t['Изменения'][1].append([e['day'], e.get('run'), 'победитель сменился' if e['change'] == 'winner_changed' else 'решение снято',
                                      f"{p['winner']['kind']}{p['winner']['id']}", f"{p['loser']['kind']}{p['loser']['id']}",
                                      l.get('text', ''), _src(nw) if nw else '', nw.get('text', ''), p.get('model') or '',
                                      p.get('reason') or ''])
        elif ev in ('bad_output', 'day_error', 'apply_error'):
            t['Ошибки'][1].append([e['ts'], e.get('run'), e.get('day'), ev, e.get('model', ''), e.get('error', ''),
                                   e.get('raw', '')])
        elif ev == 'ai_raw' and with_raw:
            t['Ответы ИИ'][1].append([e['day'], e.get('run'), ', '.join(e.get('models') or []), '\n---\n'.join(e.get('chunks') or [])])
        elif ev == 'run_start':
            runs.setdefault(e['run'], {}).update(start=e)
        elif ev == 'run_end':
            runs.setdefault(e['run'], {}).update(end=e)
    for rid in sorted(k for k in runs if k is not None):
        s, en = runs[rid].get('start') or {}, runs[rid].get('end') or {}
        st = en.get('stats') or {}
        used = ', '.join(f"{k}: {v}" for k, v in (en.get('models_used') or {}).items())
        t['Прогоны'][1].append([rid, s.get('method') or en.get('method', ''), s.get('ts', ''), en.get('ts', ''),
                                en.get('status', 'нет записи о завершении'), used, st.get('dates_total', s.get('dates_total', '')),
                                st.get('dates_processed', ''), st.get('requests', ''), st.get('tokens_in', ''),
                                st.get('tokens_out', ''), st.get('groups_found', ''), st.get('hidden', ''),
                                st.get('unhidden', ''), st.get('bad_days', ''), en.get('seconds', ''), en.get('error') or ''])
    return t


# ------------------------------------------------------------------ текущее состояние из БД

async def current_tables(day_from: str | None) -> dict:
    os.environ.setdefault('APP_ENV', 'local')
    from datetime import date
    from db.connection import close_db_pool, init_db_pool
    from db.tables import Tables
    pool = await init_db_pool()
    try:
        names = {r['channel_id']: r['username'] for r in await pool.fetch(f"SELECT channel_id, username FROM {Tables.CHANNELS}")}
        start = date.fromisoformat(day_from) if day_from else date.today()
        decs = await pool.fetch(
            f"SELECT day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason "
            f"FROM {Tables.DEDUP_DECISIONS} WHERE status IN ('proposed','active','reverted') AND day >= $1 "
            f"ORDER BY day, winner_kind, winner_id, loser_id", start)
        mids = {r['winner_id'] for r in decs if r['winner_kind'] == 'm'} | {r['loser_id'] for r in decs if r['loser_kind'] == 'm'}
        eids = {r['winner_id'] for r in decs if r['winner_kind'] == 'e'} | {r['loser_id'] for r in decs if r['loser_kind'] == 'e'}
        msgs = {r['surrogate_id']: r for r in await pool.fetch(
            f"SELECT surrogate_id, id, channel_id, text FROM {Tables.MESSAGES} WHERE surrogate_id = ANY($1::bigint[])", list(mids))}
        evs = {r['id']: r for r in await pool.fetch(
            f"SELECT id, source, source_url, title, venue, description FROM {Tables.EVENTS} WHERE id = ANY($1::bigint[])", list(eids))}
    finally:
        await close_db_pool(pool)

    def info(kind, rid):
        if kind == 'm':
            r = msgs.get(rid)
            if not r:
                return ('(пост удалён)', '', '')
            n = names.get(r['channel_id'])
            link = f"https://t.me/{n.lstrip('@')}/{r['id']}" if n else f"https://t.me/c/{r['channel_id']}/{r['id']}"
            return (('@' + n.lstrip('@')) if n else f"ch:{r['channel_id']}", link, r['text'])
        r = evs.get(rid)
        if not r:
            return ('(событие удалено)', '', '')
        return (f"event:{r['source']}", r['source_url'] or '', '. '.join(x for x in (r['title'], r['venue'], r['description']) if x))

    rows = []
    for d in decs:
        ws, wl, wt = info(d['winner_kind'], d['winner_id'])
        ls, ll, lt = info(d['loser_kind'], d['loser_id'])
        rows.append([d['day'].isoformat(), d['status'], d['decided_by'], d['model'] or '', ws, d['winner_id'], wl, wt,
                     ls, d['loser_id'], ll, lt, d['reason'] or ''])
    head = ['Дата', 'Статус', 'Кто решил', 'Модель', 'Победитель: источник', 'Победитель: id', 'Победитель: ссылка',
            'Победитель: текст', 'Проигравший: источник', 'Проигравший: id', 'Проигравший: ссылка', 'Проигравший: текст',
            'Причина']
    return {'Сейчас': (head, rows)}


# ------------------------------------------------------------------ вывод

def write_csv(tables: dict, base: Path) -> list[Path]:
    out = []
    for name, (head, rows) in tables.items():
        if not rows:
            continue
        p = base.parent / f"{base.name}_{name.replace(' ', '_')}.csv"
        with open(p, 'w', encoding='utf-8-sig', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(head)
            w.writerows(rows)
        out.append(p)
    return out


def write_xlsx(tables: dict, path: Path) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False
    wb = Workbook()
    wb.remove(wb.active)
    fill = PatternFill('solid', fgColor='DDDDDD')
    for name, (head, rows) in tables.items():
        ws = wb.create_sheet(name[:31])
        ws.append(head)
        for r in rows:
            ws.append([("'" + v if isinstance(v, str) and v[:1] in '=+-@' else v) for v in r])
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), fill
        ws.freeze_panes = 'A2'
        for i, h in enumerate(head, 1):
            wide = 'текст' in h.lower() or 'ответ' in h.lower() or 'ошибка' in h.lower() or 'причина' in h.lower()
            ws.column_dimensions[get_column_letter(i)].width = 70 if wide else 16
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical='top')
        if name == 'Группы':                          # визуально отделяем группы друг от друга
            prev, shade = None, False
            light = PatternFill('solid', fgColor='F3F6FA')
            for row in ws.iter_rows(min_row=2):
                g = row[1].value
                if g != prev:
                    shade, prev = not shade, g
                if shade:
                    for c in row:
                        c.fill = light
    wb.save(path)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Отчёт по журналу решений дедупликатора')
    ap.add_argument('--log', default=str(DEFAULT_LOG), help='путь к dedup_audit.jsonl (рядом ищутся .N.gz)')
    ap.add_argument('--run', type=int, help='номер прогона (dedup_runs.id)')
    ap.add_argument('--from', dest='day_from', help='дата YYYY-MM-DD: с (по дате события)')
    ap.add_argument('--to', dest='day_to', help='дата YYYY-MM-DD: по')
    ap.add_argument('--day', help='одна дата')
    ap.add_argument('--model', help='только решения этой модели')
    ap.add_argument('--raw', action='store_true', help='добавить сырые ответы модели')
    ap.add_argument('--current', action='store_true', help='текущее состояние решений из БД (журнал не нужен)')
    ap.add_argument('--format', choices=('csv', 'xlsx', 'both'), default='both')
    ap.add_argument('--out-dir', default='dedup_reports')
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if args.current:
        tables = asyncio.run(current_tables(args.day or args.day_from))
        base = out_dir / f"dedup_current_{stamp}"
    else:
        path = Path(args.log)
        if not log_files(path):
            print(f"Журнал не найден: {path}. Сервис пишет его в logs/dedup_audit.jsonl при deduplication.audit.enabled=true.")
            return 1
        d_from, d_to = (args.day, args.day) if args.day else (args.day_from, args.day_to)
        events = list(select(iter_events(path), run=args.run, day_from=d_from, day_to=d_to, model=args.model))
        if not events:
            print("По этим условиям в журнале ничего нет.")
            return 1
        tables = build_tables(events, with_raw=args.raw)
        tag = f"run{args.run}" if args.run is not None else (args.day or f"{d_from or 'start'}_{d_to or 'end'}")
        base = out_dir / f"dedup_report_{tag}_{stamp}"

    for name, (head, rows) in tables.items():
        print(f"  {name}: {len(rows)} строк")
    if args.format in ('csv', 'both'):
        for p in write_csv(tables, base):
            print(f"  → {p}")
    if args.format in ('xlsx', 'both'):
        p = base.with_suffix('.xlsx')
        if write_xlsx(tables, p):
            print(f"  → {p}")
        else:
            print("  openpyxl не установлен — Excel пропущен (pip install openpyxl); CSV записаны.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
