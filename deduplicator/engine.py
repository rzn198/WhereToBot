"""План решений для одной даты: чистая логика без БД и AI (удобно тестировать).

Вход: записи даты, группы дублей от AI, действующие решения, иерархия.
Выход: что скрыть, что вернуть, какие решения устарели.

Правила:
  * победитель группы — лучшая по иерархии запись; скрываются только записи ДРУГИХ источников;
  * строки дайджестов (position > 0) могут побеждать, но сами не скрываются (в одной строке — целый
    день событий, скрыть её из-за одного дубля нельзя);
  * «воскрешение»: если группа распалась, победитель пропал, скрыт вручную или сменил дату —
    проигравший снова виден; следующий по иерархии становится победителем при пересчёте группы;
  * проигравший, которого админ вернул (он снова виден при действующем решении), закрепляется
    как reverted и больше не скрывается AI; решения админа (decided_by='admin') AI не пересчитывает.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from deduplicator.grouping import Group
from deduplicator.priority import PriorityResolver
from deduplicator.records import Record, norm_text

Key = tuple[str, int]


@dataclass(frozen=True)
class Live:
    """Действующее решение из dedup_decisions (status active или reverted)."""
    id: int
    loser: Key
    winner: Key
    status: str
    decided_by: str = 'ai'
    reason: str = ''


@dataclass
class NewDecision:
    loser: Record
    winner: Record
    reason: str = ''


@dataclass
class DayPlan:
    day: date
    method: str
    groups: list[Group] = field(default_factory=list)
    lists: set = field(default_factory=set)                   # перечни событий (AI + эвристика): не побеждают и не скрываются
    weak: set = field(default_factory=set)                    # вскользь упомянувшие событие: могут проиграть, не победят
    wanted: list[NewDecision] = field(default_factory=list)   # что решил AI (для proposed и для сверки)
    insert: list[NewDecision] = field(default_factory=list)   # новые active
    stale: list[int] = field(default_factory=list)            # устаревшие решения
    revert: list[int] = field(default_factory=list)           # админ вернул → reverted
    hide: list[Key] = field(default_factory=list)
    unhide: list[Key] = field(default_factory=list)
    kept: int = 0


def eligible(members: list[Record], lists: set) -> list[Record]:
    """Участники группы, которые могут победить или проиграть: без перечней и строк дайджеста."""
    return [m for m in members if not m.is_list and not m.is_digest and m.key not in lists]


def order_group(members: list[Record], lists: set, weak: set, resolver: PriorityResolver):
    """(победитель | None, остальные участники по иерархии). Слабые записи победить не могут."""
    ok = eligible(members, lists)
    strong = sorted((m for m in ok if m.key not in weak), key=resolver.rank)
    if not strong:
        return None, []
    return strong[0], sorted((m for m in ok if m.key != strong[0].key), key=resolver.rank)


def candidates(records: list[Record], live: list[Live]) -> list[Record]:
    """Записи, которые участвуют в сравнении: видимые + скрытые нашим активным решением.
    Скрытые вручную (без активного решения) не участвуют."""
    ours = {d.loser for d in live if d.status == 'active'}
    return [r for r in records if not r.hidden or r.key in ours]


def plan_day(day: date, records: list[Record], groups: list[Group], live: list[Live],
             resolver: PriorityResolver, method: str, hide_exact_copies: bool = True) -> DayPlan:
    """records — уже отфильтрованные candidates().
    hide_exact_copies: точные копии постов одного канала (тот же день, тот же текст) скрываются, остаётся лучшая
    по иерархии; работает и в днях без других источников. События с сайтов не трогаем: у двух сеансов
    одного события может быть одинаковое описание."""
    ai_lists = set(getattr(groups, 'lists', ()) or ())
    ai_weak = set(getattr(groups, 'weak', ()) or ())
    plan = DayPlan(day=day, method=method, groups=groups, weak=ai_weak,
                   lists=ai_lists | {r.key for r in records if r.is_list})
    recs = {r.key: r for r in records}

    revert_ids: list[int] = []
    pinned: set[Key] = {d.loser for d in live if d.status == 'reverted'}
    for d in live:
        loser = recs.get(d.loser)
        if d.status == 'active' and loser is not None and not loser.hidden:   # админ вернул проигравшего
            revert_ids.append(d.id)
            pinned.add(d.loser)
    active = {d.loser: d for d in live if d.status == 'active' and d.id not in revert_ids}
    locked = {k for k, d in active.items()
              if d.decided_by == 'admin' and k in recs and d.winner in recs}

    wanted: dict[Key, NewDecision] = {}
    winners: set[Key] = set()
    for g in groups:
        winner, rest = order_group([m for m in g.members if m.key in recs], plan.lists, plan.weak, resolver)
        if winner is None or not any(m.source != winner.source for m in rest):
            continue
        winners.add(winner.key)
        for m in rest:
            if m.source == winner.source or m.key in pinned or m.key in locked:
                continue
            wanted[m.key] = NewDecision(loser=m, winner=winner, reason=g.reason)

    # точные копии проигравшего (тот же источник, тот же текст) уходят вместе с ним, даже если AI их не назвал
    copies: dict[tuple, list[Record]] = {}
    for r in records:
        if not r.is_list and not r.is_digest and r.key not in plan.lists and len(norm_text(r.text)) >= 20:
            copies.setdefault((r.source, norm_text(r.text)), []).append(r)
    for nd in list(wanted.values()):
        for t in copies.get((nd.loser.source, norm_text(nd.loser.text)), []):
            if (t.key in wanted or t.key in winners or t.key in pinned or t.key in locked
                    or t.source == nd.winner.source):
                continue
            wanted[t.key] = NewDecision(loser=t, winner=nd.winner, reason=nd.reason or 'точная копия скрытой записи')

    if hide_exact_copies:
        same_source: dict[tuple, list[Record]] = {}
        for r in records:
            if (r.kind == 'm' and not r.is_list and not r.is_digest and r.key not in plan.lists
                    and len(norm_text(r.text)) >= 20):
                same_source.setdefault((r.source, norm_text(r.text)), []).append(r)
        for members in same_source.values():
            if len(members) < 2 or any(m.key in pinned or m.key in locked for m in members):
                continue
            keeper = next((m for m in members if m.key in winners), None) or min(members, key=resolver.rank)
            if keeper.key in wanted:                 # сам хранитель проиграл другому источнику: копии уже ушли с ним
                continue
            for t in members:
                if t.key != keeper.key and t.key not in wanted and t.key not in winners:
                    wanted[t.key] = NewDecision(loser=t, winner=keeper, reason='точная копия того же источника')
    plan.wanted = list(wanted.values())

    if method != 'ai':
        return plan

    plan.revert = revert_ids
    for key, nd in wanted.items():
        d = active.get(key)
        if d is not None and d.winner == nd.winner.key:
            plan.kept += 1
            continue
        if d is not None:
            plan.stale.append(d.id)
        plan.insert.append(nd)
        if not nd.loser.hidden:
            plan.hide.append(key)
    for key, d in active.items():
        if key in wanted or key in locked:
            continue
        plan.stale.append(d.id)
        plan.unhide.append(key)          # запись могла пропасть или уехать на другую дату — тогда это no-op
    return plan
