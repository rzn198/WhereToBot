"""Лёгкие строки Zarko, тай-брейк внутри источника, исключения «один источник», точные копии строк."""
from datetime import date, datetime, timezone

from deduplicator import engine, grouping
from deduplicator.engine import Live, plan_day
from deduplicator.grouping import Group
from deduplicator.priority import PriorityResolver, SameSourcePolicy
from deduplicator.records import KIND_EVENT, KIND_MESSAGE, Record

DAY = date(2026, 11, 8)
POST = "Avoidfest. Cormac. Клуб Y, 21:00, билеты по ссылке и подробности"


def msg(i, ch=1, text=POST, pos=0, ts=0, hidden=False):
    return Record(KIND_MESSAGE, i, DAY, f"ch:{ch}", text, hidden=hidden, position=pos,
                  created_at=datetime(2026, 11, 1, 10, ts, tzinfo=timezone.utc))


def ev(i, source="polako", text="Концерт Икс. Клуб Y", ts=0):
    return Record(KIND_EVENT, i, DAY, f"event:{source}", text, created_at=datetime(2026, 11, 1, 10, ts, tzinfo=timezone.utc))


def plan(recs, groups, res=None, policy=None, live=(), method="ai"):
    return plan_day(DAY, engine.candidates(recs, list(live)), groups, list(live), res or PriorityResolver(), method, True, policy)


CH = SameSourcePolicy(["channels", "event:polako"])


# ------------------------------------------------------------------ тай-брейк: «длиннее, затем раньше»

def test_same_source_longer_text_wins():
    short, long_ = msg(1, text="Звери. Розыгрыш билетов"), msg(2, text="Звери. Концерт 20:00, Клуб Y. Билеты и подробности программы")
    p = plan([short, long_], [Group([short, long_])], policy=CH)
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(2, 1)]


def test_same_source_equal_length_earlier_wins():
    a, b = msg(1, text="Одинаковая длина текста А", ts=5), msg(2, text="Одинаковая длина текста Б", ts=1)
    assert len(a.text) == len(b.text)
    p = plan([a, b], [Group([a, b])], policy=CH)
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(2, 1)]


def test_three_records_longest_wins_and_both_others_hidden():
    a, b, c = msg(1, text="коротко про событие"), msg(2, text="чуть подлиннее про событие икс"), msg(3, text="самый длинный подробный анонс события икс в клубе")
    p = plan([a, b, c], [Group([a, b, c])], policy=CH)
    assert {(d.winner.id, d.loser.id) for d in p.insert} == {(3, 1), (3, 2)}


def test_polako_cards_compare_but_ticket_sites_do_not():
    a, b = ev(1, text="Gusarijada. Короткая карточка"), ev(2, text="Gusarijada. Подробная карточка спектакля со временем и местом")
    assert [(d.winner.id, d.loser.id) for d in plan([a, b], [Group([a, b])], policy=CH).insert] == [(2, 1)]
    t1, t2 = ev(3, "ticketsrs", "Sarah Brightman. Lože"), ev(4, "ticketsrs", "Sarah Brightman. VIP, Meet & Greet, длиннее")
    assert plan([t1, t2], [Group([t1, t2])], policy=CH).insert == []                  # категории билетов — не дубли


def test_without_policy_same_source_is_never_compared():
    a, b = msg(1, text="короткий"), msg(2, text="намного более длинный текст анонса")
    assert plan([a, b], [Group([a, b])], policy=None).insert == []


def test_priority_is_config_only_not_by_record_type():
    post, card = msg(1, ch=5), ev(2, "biletrs")
    r_event = PriorityResolver(["event:biletrs", "ch:5"])
    r_post = PriorityResolver(["ch:5", "event:biletrs"])
    assert plan([post, card], [Group([post, card])], res=r_event).insert[0].winner.key == card.key
    assert plan([post, card], [Group([post, card])], res=r_post).insert[0].winner.key == post.key


def test_card_of_higher_config_source_beats_longer_post():
    """484269/27726: карточка polako короче поста sta_imas, но выше по config — побеждает карточка (вариант A)."""
    post = msg(1, ch=7, text="Мирославово Евангелие. " * 20)
    card = ev(2, "polako", "Мирославово Евангелие")
    res = PriorityResolver(["event:polako", "ch:7"])
    assert plan([post, card], [Group([post, card])], res=res, policy=CH).insert[0].winner.key == card.key


# ------------------------------------------------------------------ лёгкие строки

def test_light_line_does_not_win_even_with_top_priority():
    line, card = msg(1, ch=1, pos=3, text="Суббота • Avoidfest, Cormac"), ev(2, "ticrs", "Avoidfest. Cormac. Клуб Y")
    p = plan([line, card], [Group([line, card])], res=PriorityResolver(["ch:1", "event:ticrs"]))
    assert [(d.winner.key, d.loser.key) for d in p.insert] == [(card.key, line.key)]


def test_double_lines_after_position_shift_are_exact_copies():
    old = msg(1, pos=2, text="Суббота • Avoidfest, Cormac. Клуб Y 21:00", ts=0)
    twin = msg(2, pos=3, text="Суббота • Avoidfest, Cormac. Клуб Y 21:00", ts=7)
    other = msg(3, pos=4, text="Воскресенье • Совсем другое событие в другом месте")
    p = plan([old, twin, other], [])
    assert [(d.winner.id, d.loser.id, d.via) for d in p.insert] == [(1, 2, "same_source")] and p.hide == [("m", 2)]


def test_line_hidden_with_its_exact_copy_when_post_wins():
    post = msg(1, text="Avoidfest Cormac клуб Y подробный анонс события на субботу")
    line, line_copy = msg(2, pos=2, text="Avoidfest Cormac клуб Y"), msg(3, pos=5, text="Avoidfest Cormac клуб Y")
    p = plan([post, line, line_copy], [Group([post, line])], policy=CH)
    assert {(d.winner.id, d.loser.id) for d in p.insert} == {(1, 2), (1, 3)}


def test_legacy_decision_with_digest_winner_is_released():
    """Раньше строка дайджеста могла быть победителем. Теперь не может: решение снимается, проигравший возвращается."""
    line, loser = msg(1, ch=1, pos=2, text="Суббота • Икс в клубе Y"), msg(2, ch=2, text="Икс в клубе Y, подробно", hidden=True)
    live = [Live(id=1, loser=loser.key, winner=line.key, status="active")]
    p = plan([line, loser], [], live=live)
    assert p.stale == [1] and p.unhide == [loser.key] and p.sticky == []


def test_list_posts_never_take_part():
    big = msg(1, text="Афиша недели. " + " ".join(f"{h}:00 событие" for h in range(10, 20)))
    other = msg(2, ch=2, text="Концерт, клуб Y, подробный анонс")
    p = plan([big, other], [Group([big, other])])
    assert p.insert == [] and big.key in p.lists


# ------------------------------------------------------------------ политика «один источник»

def test_policy_entries_channels_ids_names_and_sites():
    pol = SameSourcePolicy(["channels", "event:polako"])
    assert pol.allows_source("ch:5") and pol.allows_source("event:polako") and pol.allows_source("event:POLAKO")
    assert not pol.allows_source("event:ticketsrs") and not pol.allows_source("event:ticrs")
    narrow = SameSourcePolicy(["ch:5", "@chan_b"], {6: "Chan_B"})
    assert narrow.allows_source("ch:5") and narrow.allows_source("ch:6") and not narrow.allows_source("ch:7")
    assert SameSourcePolicy().allows_source("ch:1") and SameSourcePolicy().allows_source("event:polako")
    assert not SameSourcePolicy([]).allows_source("ch:1")


def test_parse_groups_keeps_same_source_group_only_when_allowed():
    recs = [msg(1), msg(2, text="другой текст того же источника")]
    _, idx = grouping.build_prompt(recs, {})
    raw = '{"groups": [{"ids": ["r1", "r2"], "reason": "повтор"}]}'
    assert grouping.parse_groups(raw, idx) == []
    assert len(grouping.parse_groups(raw, idx, CH.allows)) == 1


def test_prompt_has_same_source_exception_light_line_and_festival_rule():
    recs = [msg(1), ev(2, "ticketsrs"), msg(3, ch=2, pos=2, text="Суббота • Икс")]
    prompt, _ = grouping.build_prompt(recs, {}, same_ok=CH.allows)
    assert grouping.SAME_SOURCE_TAG in prompt and "лёгкая строка" in prompt and "внутри фестиваля" in prompt
    lines = [ln for ln in prompt.splitlines() if ln.startswith("[r")]
    assert grouping.SAME_SOURCE_TAG in lines[0] and grouping.SAME_SOURCE_TAG not in lines[1]       # ticketsrs — нельзя
    assert "СПИСОК" not in prompt


# ------------------------------------------------------------------ липкий победитель и запись того же источника

def _sticky_case(new_text, ch=1):
    old = msg(1, ch=ch, text="Мужички. Спектакль 19:00, Vojvode Dobrnjca 48, регистрация по ссылке", ts=0)
    card = ev(3, "polako", "Мужички. Спектакль Милены Предич")
    fix = msg(2, ch=ch, text=new_text, ts=9)
    card_hidden = Record(card.kind, card.id, card.day, card.source, card.text, hidden=True, created_at=card.created_at)
    live = [Live(id=1, loser=card.key, winner=old.key, status="active")]
    res = PriorityResolver(["ch:1", "event:polako"])
    return old, fix, card_hidden, plan([old, fix, card_hidden], [Group([old, fix, card_hidden])], res=res, policy=CH, live=live)


def test_longer_corrected_post_of_same_source_replaces_sticky_winner():
    """467350/486062: исправленный анонс (20:00, длиннее) заменяет победителя со старым временем 19:00."""
    old, fix, card, p = _sticky_case("ВНИМАНИЕ! Время изменилось. Мужички. Спектакль 20:00, Vojvode Dobrnjca 48, регистрация по ссылке")
    assert {(d.winner.id, d.loser.key) for d in p.insert} == {(2, old.key), (2, card.key)}   # карточка переходит под нового
    assert p.merged == {old.key: fix.key} and old.key in p.hide


def test_shorter_repeat_of_same_source_does_not_displace_sticky_winner():
    old, fix, card, p = _sticky_case("Напоминание: Мужички, 19:00")
    assert [(d.winner.id, d.loser.id) for d in p.insert] == [(1, 2)] and p.merged == {}
    assert p.kept == 1                                                                    # карточка осталась под прежним победителем


def test_sticky_still_protects_winner_from_other_source_with_higher_priority():
    old = msg(1, ch=2, text="Концерт Икс в клубе Y, 20:00, подробный анонс события", ts=0)
    other = msg(2, ch=1, text="Икс", ts=1)                                                 # источник выше по config, но чужой
    live = [Live(id=1, loser=ev(3, "ticrs").key, winner=old.key, status="active")]
    card = Record(KIND_EVENT, 3, DAY, "event:ticrs", "Икс. Клуб Y", hidden=True)
    p = plan([old, other, card], [Group([old, other, card])], res=PriorityResolver(["ch:1", "ch:2"]), live=live)
    assert {(d.winner.id, d.loser.id) for d in p.insert} == {(1, 2)} and p.merged == {}  # прежняя логика не изменилась
