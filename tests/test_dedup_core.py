"""Дедупликация: иерархия, промпт/разбор ответа, план решений (без БД и без AI)."""
from datetime import date, datetime, timezone

import pytest

from deduplicator import engine, grouping
from deduplicator.engine import Live, plan_day
from deduplicator.grouping import BadGroupingOutput, Group
from deduplicator.priority import PriorityResolver
from deduplicator.records import KIND_EVENT, KIND_MESSAGE, Record, fingerprint

DAY = date(2026, 11, 8)


def msg(i, channel=1, text="Концерт группы Икс в клубе Y, 20:00", pos=0, hidden=False, ts=0):
    return Record(KIND_MESSAGE, i, DAY, f"ch:{channel}", text, hidden=hidden, position=pos,
                  created_at=datetime(2026, 11, 1, tzinfo=timezone.utc).replace(minute=ts))


def ev(i, source="kupikarte", text="Группа Икс. Клуб Y", hidden=False):
    return Record(KIND_EVENT, i, DAY, f"event:{source}", text, hidden=hidden)


def resolver(entries=(), names=None):
    return PriorityResolver(list(entries), names or {})


# ------------------------------------------------------------------ иерархия

def test_unlisted_messages_beat_events_then_longer_text_then_earlier():
    r = resolver()
    assert r.rank(msg(1)) < r.rank(ev(2))
    assert r.rank(msg(1, text="длиннее чем другой текст")) < r.rank(msg(2, channel=2, text="коротко"))
    assert r.rank(msg(1, ts=1)) > r.rank(msg(2, channel=2, ts=0))


def test_listed_sources_follow_list_order_mixing_messages_and_events():
    r = resolver(["event:kupikarte", "@chan_b", "ch:1"], {2: "chan_b", 1: "chan_a"})
    order = sorted([msg(1, 1), msg(2, 2), ev(3, "kupikarte"), ev(4, "other")], key=r.rank)
    assert [x.id for x in order] == [3, 2, 1, 4]       # событие kupikarte, @chan_b, ch:1, неуказанное


def test_priority_entry_forms_are_normalized():
    r = resolver(["chan_b", "@Chan_C", "id:5", "EVENT:Site"], {2: "Chan_B", 3: "chan_c"})
    assert r.entries == ["@chan_b", "@chan_c", "ch:5", "event:site"]
    assert r.position(msg(1, 3)) == 1 and r.position(msg(1, 5)) == 2 and r.position(ev(1, "SITE")) == 3


def test_unresolved_channel_names_are_reported():
    r = resolver(["@known", "@ghost", "event:x"], {1: "known"})
    assert r.unresolved() == ["@ghost"]


def test_label_prefers_username():
    r = resolver([], {7: "chan"})
    assert r.label(msg(1, 7)) == "@chan" and r.label(msg(1, 8)) == "ch:8" and r.label(ev(1)) == "event:kupikarte"


# ------------------------------------------------------------------ промпт и разбор

def test_prompt_marks_events_digests_and_sources():
    recs = [msg(1), ev(2), msg(3, channel=2, pos=2)]
    prompt, index = grouping.build_prompt(recs, {r.key: resolver().label(r) for r in recs})
    assert set(index) == {"r1", "r2", "r3"} and index["r2"] is recs[1]
    assert "событие с сайта" in prompt and "лёгкая строка: одно событие из подборки канала" in prompt and "2026-11-08" in prompt
    assert "Сомневаешься — не объединяй" in prompt


def test_prompt_truncates_long_text_and_flattens_lines():
    prompt, _ = grouping.build_prompt([msg(1, text="а\nб " * 400)], {}, max_chars=50)
    assert "…" in prompt and "а б а б" in prompt


def _index():
    recs = [msg(1), msg(2, channel=2), ev(3), ev(4, "other"), msg(5)]
    _, idx = grouping.build_prompt(recs, {})
    return recs, idx


def test_parse_groups_valid_and_reason_trimmed():
    recs, idx = _index()
    g = grouping.parse_groups('{"groups": [{"ids": ["r1", "r3"], "reason": "' + "я" * 500 + '"}]}', idx)
    assert [m.key for m in g[0].members] == [recs[0].key, recs[2].key] and len(g[0].reason) <= 201


@pytest.mark.parametrize("raw", ['{"groups": []}', '```json\n{"groups": []}\n```', 'вот ответ: {"groups": []}'])
def test_parse_groups_empty_and_fenced(raw):
    assert grouping.parse_groups(raw, _index()[1]) == []


def test_parse_groups_drops_bad_groups_but_keeps_good_ones():
    _, idx = _index()
    raw = ('{"groups": [{"ids": ["r1", "r9"]}, {"ids": ["r1", "r5"]}, {"ids": ["r2", "r3"]}, '
           '{"ids": ["r2", "r4"]}, "мусор"]}')
    g = grouping.parse_groups(raw, idx)
    # r1+r9: чужой id → одна запись; r1+r5: один источник; r2+r3 — ок; r2+r4: r2 уже занят
    assert [[m.id for m in x.members] for x in g] == [[2, 3]]


@pytest.mark.parametrize("raw", ["", "нет json", '{"groups": "не список"}', '[1, 2]', '{"groups": [}'])
def test_parse_groups_rejects_broken_output(raw):
    with pytest.raises(BadGroupingOutput):
        grouping.parse_groups(raw, _index()[1])


def test_group_ids_are_case_and_space_tolerant():
    _, idx = _index()
    assert len(grouping.parse_groups('{"groups": [{"ids": [" R1 ", "r3"]}]}', idx)) == 1


# ------------------------------------------------------------------ отпечаток

def test_fingerprint_changes_on_text_hidden_membership_and_salt():
    a, b = msg(1), ev(2)
    base = fingerprint([a, b], "s")
    assert fingerprint([b, a], "s") == base
    assert fingerprint([a, b], "other") != base
    assert fingerprint([a], "s") != base
    assert fingerprint([a, ev(2, text="другое")], "s") != base
    assert fingerprint([a, ev(2, hidden=True)], "s") != base


# ------------------------------------------------------------------ план

def plan(records, groups, live=(), method="ai", res=None):
    recs = engine.candidates(records, list(live))
    return plan_day(DAY, recs, groups, list(live), res or resolver(), method)


def test_winner_by_hierarchy_loser_hidden():
    m, e = msg(1), ev(2)
    p = plan([m, e], [Group([m, e], "то же")])
    assert [(d.winner.key, d.loser.key) for d in p.insert] == [(m.key, e.key)] and p.hide == [e.key]


def test_priority_list_can_put_event_above_message():
    m, e = msg(1), ev(2)
    p = plan([m, e], [Group([m, e])], res=resolver(["event:kupikarte"]))
    assert p.insert[0].winner.key == e.key and p.hide == [m.key]


def test_same_source_records_are_not_hidden_by_their_sibling():
    a1, a2, b = msg(1, text="длинный текст поста а один"), msg(2, text="пост а два"), msg(3, channel=2, text="б")
    p = plan([a1, a2, b], [Group([a1, a2, b])])
    assert [d.loser.id for d in p.insert] == [3] and a2.key not in p.hide     # победитель a1, a2 того же источника


def test_digest_row_never_wins_but_can_lose():
    digest, post = msg(1, pos=2), msg(2, channel=2)
    for pr in (["ch:1"], ["ch:2"]):                                          # приоритет канала строки ничего не меняет
        p = plan([digest, post], [Group([digest, post])], res=resolver(pr))
        assert [(d.winner.id, d.loser.id) for d in p.insert] == [(2, 1)] and p.hide == [("m", 1)]


def test_digest_row_loses_to_event_card():
    digest, card = msg(1, pos=5, text="Суббота • Goatmare / Zappa Barka, 21:00"), ev(2, text="GOATMARE. Zappa Barka. Подробно")
    p = plan([digest, card], [Group([digest, card])])
    assert [(d.winner.key, d.loser.key) for d in p.insert] == [(card.key, digest.key)]


def test_two_digest_rows_in_one_group_have_no_winner():
    a, b = msg(1, pos=2, text="Суббота • Икс в клубе Y"), msg(2, channel=2, pos=3, text="Суббота • Икс в клубе Y!")
    p = plan([a, b], [Group([a, b])])
    assert p.insert == [] and p.group_winners == [None]


def test_digest_row_loses_to_post_of_the_same_channel_when_allowed():
    """Вариант C: строка Zarko проигрывает обычному посту того же канала (position = 0), если ИИ признал их одним событием."""
    from deduplicator.priority import SameSourcePolicy
    line, post = msg(1, pos=4, text="Суббота • Avoidfest, Cormac"), msg(2, text="Avoidfest. Cormac. Клуб Y, 21:00, билеты по ссылке")
    groups = [Group([line, post])]
    policy = SameSourcePolicy(["channels"])
    p = plan_day(DAY, engine.candidates([line, post], []), groups, [], resolver(["ch:1"]), "ai", True, policy)
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(2, 1)]
    off = plan_day(DAY, engine.candidates([line, post], []), groups, [], resolver(["ch:1"]), "ai", True, None)
    assert off.insert == []                                                   # без исключения одного источника — как раньше


def test_shadow_proposes_but_changes_nothing():
    m, e = msg(1), ev(2)
    p = plan([m, e], [Group([m, e])], method="shadow")
    assert len(p.wanted) == 1 and p.insert == [] and p.hide == [] and p.unhide == [] and p.stale == []


def test_unchanged_group_keeps_existing_decision():
    m, e = msg(1), ev(2, hidden=True)
    live = [Live(10, e.key, m.key, "active")]
    p = plan([m, e], [Group([m, e])], live)
    assert p.kept == 1 and p.insert == [] and p.hide == [] and p.unhide == [] and p.stale == []


def test_group_not_repeated_by_ai_keeps_existing_pair():
    m, e = msg(1), ev(2, hidden=True)
    p = plan([m, e], [], [Live(10, e.key, m.key, "active")])
    assert p.unhide == [] and p.stale == [] and p.insert == [] and p.hide == []
    assert [d.id for d in p.sticky] == [10]


def test_pair_stays_when_ai_groups_loser_with_other_records_but_not_its_winner():
    m, e, x = msg(1), ev(2, hidden=True), ev(3, source="other")
    p = plan([m, e, x], [Group([m, x])], [Live(10, e.key, m.key, "active")])
    assert [d.id for d in p.sticky] == [10] and p.unhide == []              # e и m не названы в новой группе


def test_sticky_pair_released_when_loser_becomes_winner_of_new_group():
    m, e, x = msg(1, channel=5), ev(2, hidden=True), ev(3, source="other", text="Совсем другое")
    r = resolver(["event:kupikarte"])
    p = plan([m, e, x], [Group([e, x])], [Live(10, e.key, m.key, "active")], res=r)
    assert e.key in {d.winner.key for d in p.wanted} and p.sticky == []      # e теперь победитель, а не проигравший
    assert p.unhide == [e.key] and p.stale == [10]


def test_sticky_pair_follows_merge_instead_of_staying_under_a_loser_winner():
    a, b = msg(1, channel=1), msg(2, channel=2, hidden=True)
    d, c = msg(3, channel=3), msg(4, channel=4, hidden=True)
    live = [Live(10, b.key, a.key, "active"), Live(11, c.key, d.key, "active")]
    p = plan([a, b, c, d], [Group([a, d])], live)                              # AI свёл две группы: a (раньше) побеждает d
    assert {(x.winner.key, x.loser.key) for x in p.insert} >= {(a.key, d.key), (a.key, c.key)}
    assert [x.id for x in p.sticky] == [10]                                    # b остаётся под a; c переехал под a, не «липкий»


def test_sticky_pair_released_when_winner_is_hidden_by_its_own_decision():
    m, e = msg(1, hidden=True), ev(2, hidden=True)                              # победитель m сам скрыт нашим решением
    live = [Live(10, e.key, m.key, "active"), Live(11, m.key, ("m", 9), "active")]
    p = plan([m, e], [], live)
    assert 10 in p.stale and e.key in p.unhide


def test_sticky_pair_released_when_loser_turns_into_list():
    m = msg(1)
    e = msg(2, channel=2, text=DIGEST_TEXT, hidden=True)
    p = plan([m, e], [], [Live(10, e.key, m.key, "active")])
    assert p.sticky == [] and p.unhide == [e.key] and p.stale == [10]


def test_sticky_pair_does_not_hold_after_admin_returned_the_loser():
    m, e = msg(1), ev(2)                                                      # проигравший снова виден
    p = plan([m, e], [], [Live(10, e.key, m.key, "active")])
    assert p.revert == [10] and p.sticky == [] and p.unhide == []


def test_sticky_pair_in_shadow_changes_nothing():
    m, e = msg(1), ev(2, hidden=True)
    p = plan([m, e], [], [Live(10, e.key, m.key, "active")], method="shadow")
    assert p.sticky == [] and p.unhide == [] and p.stale == []


def test_winner_removed_or_hidden_manually_resurrects_loser():
    e = ev(2, hidden=True)
    p = plan([e], [], [Live(10, e.key, ("m", 1), "active")])                  # победитель пропал (удалён / другая дата)
    assert p.unhide == [e.key] and p.stale == [10]
    m_hidden = msg(1, hidden=True)                                            # победитель скрыт вручную → не кандидат
    p2 = plan([m_hidden, e], [], [Live(10, e.key, m_hidden.key, "active")])
    assert p2.unhide == [e.key]


def test_next_in_hierarchy_becomes_winner_when_old_winner_leaves():
    a, b, c = msg(1, channel=1), msg(2, channel=2, hidden=True), msg(3, channel=3, hidden=True)
    live = [Live(10, b.key, a.key, "active"), Live(11, c.key, a.key, "active")]
    r = resolver(["ch:1", "ch:2", "ch:3"])
    p = plan([b, c], [Group([b, c])], live, res=r)                            # a удалён
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(2, 3)]
    assert p.unhide == [b.key] and sorted(p.stale) == [10, 11] and p.hide == []   # c остаётся скрытым (новое решение)


def test_incumbent_winner_is_sticky_when_hierarchy_changes():
    m, e = msg(1), ev(2, hidden=True)
    p = plan([m, e], [Group([m, e])], [Live(10, e.key, m.key, "active")], res=resolver(["event:kupikarte"]))
    assert p.kept == 1 and p.insert == [] and p.stale == [] and p.hide == [] and p.unhide == []


def test_new_member_loses_to_incumbent_even_with_higher_priority():
    a, b, c = msg(1, channel=2), msg(2, channel=3, hidden=True), msg(3, channel=1)      # c — новый и выше по приоритету
    live = [Live(10, b.key, a.key, "active")]
    p = plan([a, b, c], [Group([a, b, c])], live, res=resolver(["ch:1", "ch:2", "ch:3"]))
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(1, 3)] and p.kept == 1
    assert p.hide == [c.key] and p.stale == [] and p.group_winners == [a.key]


def test_new_group_picks_winner_by_priority():
    a, b = msg(1, channel=2), msg(2, channel=1)
    p = plan([a, b], [Group([a, b])], res=resolver(["ch:1", "ch:2"]))
    assert p.insert[0].winner.key == b.key


def test_merge_keeps_earlier_decision_and_moves_other_winner_with_its_losers():
    a, a2 = msg(1, channel=3), msg(2, channel=4, hidden=True)        # группа 1: победитель a (решение 10)
    b, b2 = msg(3, channel=1), msg(4, channel=2, hidden=True)        # группа 2: победитель b (решение 20), приоритет выше
    live = [Live(10, a2.key, a.key, "active"), Live(20, b2.key, b.key, "active")]
    p = plan([a, a2, b, b2], [Group([a, a2, b, b2])], live, res=resolver(["ch:1", "ch:2", "ch:3", "ch:4"]))
    assert p.group_winners == [a.key] and p.merged == {b.key: a.key}
    assert sorted((d.winner.id, d.loser.id) for d in p.insert) == [(1, 3), (1, 4)]
    assert p.stale == [20] and p.hide == [b.key] and p.kept == 1
    assert {d.loser.id: d.via for d in p.insert}[4] == "ai_group"


def test_merge_moves_losers_the_model_forgot_to_name():
    a, a2 = msg(1, channel=3), msg(2, channel=4, hidden=True)
    b, b2 = msg(3, channel=1), msg(4, channel=2, hidden=True)
    live = [Live(10, a2.key, a.key, "active"), Live(20, b2.key, b.key, "active")]
    p = plan([a, a2, b, b2], [Group([a, b])], live)                    # модель назвала только победителей
    assert {d.loser.id: d.via for d in p.insert} == {3: "ai_group", 4: "merge"}


def test_incumbent_vanished_or_hidden_recomputes_by_priority():
    a, b, c = msg(1, channel=3, hidden=True), msg(2, channel=2, hidden=True), msg(3, channel=1, hidden=True)
    live = [Live(10, b.key, a.key, "active"), Live(11, c.key, a.key, "active")]     # a пропал
    p = plan([b, c], [Group([b, c])], live, res=resolver(["ch:1", "ch:2", "ch:3"]))
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(3, 2)]


def test_proposed_decisions_make_incumbent_in_shadow():
    a, b, c = msg(1, channel=2), msg(2, channel=3), msg(3, channel=1)
    live = [Live(10, b.key, a.key, "proposed")]
    p = plan([a, b, c], [Group([a, b, c])], live, method="shadow", res=resolver(["ch:1", "ch:2", "ch:3"]))
    assert {(d.winner.id, d.loser.id) for d in p.wanted} == {(1, 2), (1, 3)}


def test_regrouping_cannot_demote_incumbent_to_loser_of_lower_one():
    a, b = msg(1, channel=3), msg(2, channel=1, hidden=True)
    live = [Live(10, b.key, a.key, "active")]
    p = plan([a, b], [Group([b, a])], live, res=resolver(["ch:1", "ch:3"]))        # порядок участников не важен
    assert p.kept == 1 and p.insert == []


def test_loser_returned_by_admin_is_pinned_and_never_hidden_again():
    m, e = msg(1), ev(2)                                                      # проигравший виден при active-решении
    p = plan([m, e], [Group([m, e])], [Live(10, e.key, m.key, "active")])
    assert p.revert == [10] and p.insert == [] and p.hide == []
    p2 = plan([m, e], [Group([m, e])], [Live(10, e.key, m.key, "reverted")])
    assert p2.insert == [] and p2.hide == [] and p2.revert == []


def test_admin_decision_is_not_recalculated_while_both_records_exist():
    m, e = msg(1), ev(2, hidden=True)
    live = [Live(10, e.key, m.key, "active", decided_by="admin")]
    p = plan([m, e], [], live)
    assert p.unhide == [] and p.stale == []                                   # AI не отменяет решение админа
    p2 = plan([e], [], live)                                                  # а если победителя больше нет — воскрешаем
    assert p2.unhide == [e.key] and p2.stale == [10]


def test_manually_hidden_records_do_not_take_part():
    m, e = msg(1), ev(2, hidden=True)                                         # e скрыта вручную (нет решения)
    assert engine.candidates([m, e], []) == [m]


def test_vanished_day_unhides_everything():
    p = plan([], [], [Live(10, ("e", 2), ("m", 1), "active")])
    assert p.unhide == [("e", 2)] and p.stale == [10]


# ------------------------------------------------------------------ перечни событий (дайджесты без разреза)

DIGEST_TEXT = ("Афиша Мероприятий в Белграде: 25 - 27 сентября. Дайджест. 25.09 Пятница 18:00 Йога 20:00 Японский клуб "
               "26.09 10:00 Занятия 12:00 Экскурсия 15:30 Лекция 16:00 Квиз")
MINI_TEXT = "Мини дайджест на выходные: Thrift Trampa 26.09.2026 Market 26.09.2026 Ночь летучих мышей 26.09.2026"
FESTIVAL = "Фестиваль. Программа: 12:00 открытие, 13:00 лекция, 14:30 концерт, 16:00 мастер-класс, 18:00 финал"


@pytest.mark.parametrize("text,expected", [
    (DIGEST_TEXT, True), (MINI_TEXT, True),
    ("Подборка бесплатных событий недели", False),                         # слабое слово без 8 времён — не перечень
    ("Дайджест: куда пойти в Белграде", True),                             # сильное слово в начале
    ("Японский разговорный клуб 25.09 в 20:00. Тема: природа", False), (FESTIVAL, False), ("", False),
])
def test_looks_like_list_heuristic(text, expected):
    from deduplicator.records import looks_like_list
    assert looks_like_list(text) is expected


def test_is_list_applies_only_to_whole_messages():
    assert msg(1, text=DIGEST_TEXT).is_list
    assert not msg(1, text=DIGEST_TEXT, pos=3).is_list          # строка разрезанного дайджеста — своя логика
    assert not ev(1, text=DIGEST_TEXT).is_list


def test_unsplit_digest_never_wins_and_original_post_is_not_hidden():
    digest, club = msg(1, channel=1, text=DIGEST_TEXT * 3), msg(2, channel=2, text="Японский клуб 25.09 в 20:00")
    p = plan([digest, club], [Group([digest, club], "в перечне есть клуб")])
    assert p.insert == [] and p.hide == []
    assert digest.key in p.lists


def test_unsplit_digest_does_not_block_real_duplicates_in_same_group():
    digest = msg(1, channel=1, text=DIGEST_TEXT * 3)
    a, b = msg(2, channel=2, text="Японский клуб 25.09 в 20:00 подробности"), msg(3, channel=3, text="Японский клуб")
    p = plan([digest, a, b], [Group([digest, a, b])])
    assert [(d.winner.key, d.loser.key) for d in p.insert] == [(a.key, b.key)]
    assert p.hide == [b.key]


def test_ai_marked_list_is_also_excluded():
    a, b = msg(1, channel=1, text="Мини-афиша без слов-маркеров"), msg(2, channel=2, text="Событие Х")
    groups = grouping.Groups([Group([a, b])], lists={a.key})
    recs = engine.candidates([a, b], [])
    p = plan_day(DAY, recs, groups, [], resolver(), "ai")
    assert p.insert == [] and p.lists == {a.key}


def test_parse_groups_reads_lists_and_ignores_unknown_ids():
    recs = [msg(1), msg(2, channel=2), msg(3, channel=3)]
    _, idx = grouping.build_prompt(recs, {})
    g = grouping.parse_groups('{"groups": [{"ids": ["r1", "r2"]}], "lists": ["r3", "r99", " R1 "]}', idx)
    assert g.lists == {recs[2].key, recs[0].key} and len(g) == 1
    assert grouping.parse_groups('{"groups": []}', idx).lists == set()
    assert grouping.parse_groups('{"groups": [], "lists": "r1"}', idx).lists == set()


def test_prompt_has_lists_rule_and_tags_unsplit_digest():
    prompt, _ = grouping.build_prompt([msg(1, text=DIGEST_TEXT), msg(2, channel=2)], {})
    assert "lists" in prompt and "ПЕРЕЧЕНЬ нескольких событий" in prompt
    assert grouping.PROMPT_VERSION == 'v4' and "weak" in prompt


# ------------------------------------------------------------------ пакет 1d: слабые записи, копии, терпимый разбор

def test_weak_record_cannot_win_but_can_lose():
    long_note = msg(1, channel=1, text="Я в отпуске, но советую сходить на экскурсию 4 июля с Марией. " * 3)
    real = msg(2, channel=2, text="4 июля, суббота, 13:00 подземный Белград")
    groups = grouping.Groups([Group([long_note, real])], weak={long_note.key})
    p = plan_day(DAY, engine.candidates([long_note, real], []), groups, [], resolver(), "ai")
    assert [(d.winner.key, d.loser.key) for d in p.insert] == [(real.key, long_note.key)]


def test_group_of_only_weak_records_is_skipped():
    a, b = msg(1, channel=1), msg(2, channel=2)
    groups = grouping.Groups([Group([a, b])], weak={a.key, b.key})
    p = plan_day(DAY, engine.candidates([a, b], []), groups, [], resolver(), "ai")
    assert p.insert == [] and p.hide == []


COPY = "Кулинарный мастер-класс «Сентябрьский ужин» 6 сентября 18:00 приглашаем всех желающих"


def test_exact_copies_of_a_loser_from_the_same_source_leave_with_it():
    win = msg(1, channel=1, text=COPY)
    lose, twin1, twin2 = msg(2, channel=2, text=COPY), msg(3, channel=2, text=COPY.upper() + "!"), msg(4, channel=2, text=COPY)
    other = msg(5, channel=2, text="Совсем другой пост про джаз и ещё немного слов для длины")
    p = plan([win, lose, twin1, twin2, other], [Group([win, lose])])
    assert {d.loser.id for d in p.insert} == {2, 3, 4} and all(d.winner.id == 1 for d in p.insert)
    assert set(p.hide) == {("m", 2), ("m", 3), ("m", 4)} and ("m", 5) not in p.hide


def test_copies_never_touch_winner_pinned_or_short_texts():
    win = msg(1, channel=1, text=COPY)
    lose = msg(2, channel=2, text=COPY)
    winner_twin = msg(3, channel=1, text=COPY, ts=9)               # точная копия победителя: уходит под флагом hide_exact_copies
    digest_twin = msg(4, channel=2, text=COPY, pos=3)
    p = plan([win, lose, winner_twin, digest_twin], [Group([win, lose])])
    assert {d.loser.id for d in p.insert} == {2, 3, 4}          # копия проигравшего в виде строки дайджеста уходит вместе с ним
    off = plan_day(DAY, engine.candidates([win, lose, winner_twin, digest_twin], []), [Group([win, lose])], [], resolver(), "ai",
                   hide_exact_copies=False)
    assert [d.loser.id for d in off.insert] == [2, 4]      # копии проигравшего уходят с ним; копия победителя (3) без флага остаётся
    short_w, short_l, short_t = msg(5, channel=1, text="Да"), msg(6, channel=2, text="Да"), msg(7, channel=2, text="Да")
    p2 = plan([short_w, short_l, short_t], [Group([short_w, short_l])])
    assert [d.loser.id for d in p2.insert] == [6]


def test_copy_of_pinned_loser_is_left_alone():
    win, lose, twin = msg(1, channel=1, text=COPY), msg(2, channel=2, text=COPY), msg(3, channel=2, text=COPY)
    live = [Live(id=9, loser=twin.key, winner=win.key, status="reverted")]
    p = plan([win, lose, twin], [Group([win, lose])], live=live)
    assert [d.loser.id for d in p.insert] == [2]


def test_parse_groups_reads_weak_ids():
    recs = [msg(1), msg(2, channel=2), msg(3, channel=3)]
    _, idx = grouping.build_prompt(recs, {})
    g = grouping.parse_groups('{"groups": [{"ids": ["r1", "r2"]}], "lists": [], "weak": ["r1", "r77"]}', idx)
    assert g.weak == {recs[0].key}


def test_broken_reason_json_is_salvaged_but_truncated_answer_is_not():
    recs = [msg(1), msg(2, channel=2), msg(3, channel=3), msg(4, channel=4)]
    _, idx = grouping.build_prompt(recs, {})
    broken = '{"groups": [{"ids": ["r2", "r1"], "reason": "забыл кавычку"]}, {"ids": ["r3", "r4"], "reason": "ок"}], "lists": ["r4"], "weak": []}'
    g = grouping.parse_groups(broken, idx)
    assert [[m.id for m in x.members] for x in g] == [[2, 1], [3, 4]] and g.lists == {recs[3].key}
    with pytest.raises(BadGroupingOutput):
        grouping.parse_groups('{"groups": [{"ids": ["r1", "r2"], "reason": "обор', idx)


# ------------------------------------------------------------------ точные копии внутри одного источника

SAME = "Японский разговорный клуб 25.09 в 20:00. Тема встречи: времена года"


def test_same_source_exact_copies_hidden_even_without_ai_groups():
    a, b, c = msg(1, channel=1, text=SAME, ts=0), msg(2, channel=1, text=SAME, ts=5), msg(3, channel=1, text="другой текст поста про джаз")
    p = plan([a, b, c], [])
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(1, 2)] and p.hide == [("m", 2)]
    assert p.insert[0].reason == "точная копия того же источника"


def test_same_source_copy_follows_winner_when_group_exists():
    other = msg(9, channel=2, text="Японский клуб 25.09 в 20:00 и подробное описание встречи в баре")
    a, b = msg(1, channel=1, text=SAME, ts=0), msg(2, channel=1, text=SAME, ts=5)
    p = plan([a, b, other], [Group([a, other])], res=resolver(["ch:1"]))
    assert {(d.winner.id, d.loser.id) for d in p.insert} == {(1, 9), (1, 2)}


def test_same_source_copies_follow_loser_not_keeper_when_cross_source_loses():
    win = msg(1, channel=2, text=SAME)
    a, b = msg(2, channel=1, text=SAME, ts=0), msg(3, channel=1, text=SAME, ts=5)
    p = plan([win, a, b], [Group([win, a])], res=resolver(["ch:2"]))
    assert {(d.winner.id, d.loser.id) for d in p.insert} == {(1, 2), (1, 3)}


def test_same_source_copies_flag_off_and_events_and_digests_and_pinned_are_untouched():
    a, b = msg(1, channel=1, text=SAME), msg(2, channel=1, text=SAME, ts=3)
    assert plan_day(DAY, engine.candidates([a, b], []), [], [], resolver(), "ai", hide_exact_copies=False).insert == []
    e1, e2 = ev(3, text="Одинаковое описание сеанса " * 3), ev(4, text="Одинаковое описание сеанса " * 3)
    assert plan([e1, e2], []).insert == []                                      # два сеанса одного события
    live = [Live(id=1, loser=b.key, winner=a.key, status="reverted")]
    assert plan([a, b], [], live=live).insert == []                             # админ вернул — не скрываем


def test_same_source_copies_in_shadow_are_only_proposed():
    a, b = msg(1, channel=1, text=SAME), msg(2, channel=1, text=SAME, ts=3)
    p = plan([a, b], [], method="shadow")
    assert len(p.wanted) == 1 and p.insert == [] and p.hide == []


# ------------------------------------------------------------------ 2d: нарезка по парам частей и объединение групп

def _many(n):
    return [msg(i, channel=i) for i in range(1, n + 1)]


def test_small_day_is_one_request():
    recs = _many(80)
    assert grouping.plan_requests(recs, 80) == [recs]


def test_big_day_every_pair_of_records_shares_a_request():
    recs = _many(129)
    reqs = grouping.plan_requests(recs, 80)
    assert len(reqs) == 6 and all(len(r) <= 80 for r in reqs)
    seen = [set(x.key for x in r) for r in reqs]
    for a in range(0, 129, 7):
        for b in range(a + 1, 129, 5):
            assert any(recs[a].key in s and recs[b].key in s for s in seen), (a, b)


def test_pair_that_old_chunking_separated_is_now_compared():
    recs = _many(129)
    first, last = recs[1], recs[84]                           # раньше: позиция 2 и позиция 85 — разные запросы
    old = grouping.chunk_records(recs, 80)
    assert not any(first in c and last in c for c in old)
    assert any(first in r and last in r for r in grouping.plan_requests(recs, 80))


def test_too_big_day_falls_back_to_overlapping_chunks():
    recs = _many(400)
    assert grouping.plan_requests(recs, 80, max_requests=21) == grouping.chunk_records(recs, 80)


def test_merge_groups_joins_groups_sharing_a_record():
    recs = _many(6)
    a, b, c, d, e, f = recs
    out = grouping.merge_groups([Group([a, b], "r1"), Group([c, a], ""), Group([e, f], "r3"), Group([b, c], "")], recs)
    assert [[m.id for m in g.members] for g in out] == [[1, 2, 3], [5, 6]]
    assert [g.reason for g in out] == ["r1", "r3"]


def test_merge_groups_keeps_disjoint_groups_untouched():
    recs = _many(4)
    out = grouping.merge_groups([Group([recs[2], recs[3]], "x"), Group([recs[0], recs[1]], "y")], recs)
    assert [[m.id for m in g.members] for g in out] == [[1, 2], [3, 4]]


def test_incumbent_stays_winner_even_if_model_marks_it_weak_now():
    a, b = msg(1, channel=2), msg(2, channel=1, hidden=True)
    g = Group([a, b], "то же")
    g.members = [a, b]
    groups = grouping.Groups([g], weak={a.key})
    live = [Live(10, b.key, a.key, "active")]
    p = plan_day(DAY, engine.candidates([a, b], live), groups, live, resolver(["ch:1", "ch:2"]), "ai")
    assert p.kept == 1 and p.insert == [] and p.group_winners == [a.key]
