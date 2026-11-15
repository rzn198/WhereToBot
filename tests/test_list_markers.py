"""Определение перечня (дайджест, афиша недели): сильные и слабые слова, окна, времена, маркеры из config.yaml."""
import csv
from pathlib import Path

import pytest

from deduplicator import records
from deduplicator.records import KIND_MESSAGE, Record, build_list_markers, configure_list_markers, looks_like_list

FIXTURE = Path(__file__).parent / "fixtures" / "lists_20.csv"
TIMES = " ".join(f"{h}:00 событие" for h in range(10, 20))          # 10 времён


@pytest.fixture(autouse=True)
def default_markers():
    configure_list_markers(None)
    yield
    configure_list_markers(None)


def test_denis_labelled_twenty_posts_all_match():
    """20 постов, размеченных Denis (10 дайджестов и 10 нет): определение даёт ровно его разметку."""
    rows = list(csv.DictReader(open(FIXTURE, encoding="utf-8")))
    assert len(rows) == 20
    wrong = [r["id"] for r in rows if looks_like_list(r["text"]) != bool(int(r["is_digest"]))]
    assert wrong == []


@pytest.mark.parametrize("text", [
    "Дайджест событий на неделю", "Афиша недели 9–15.11", "Афиша мероприятий: пятница", "АФИША ВЫХОДНЫХ в Белграде",
    "Афиша на неделю", "Афиша  на   неделю",
])
def test_strong_word_in_first_80_chars_is_enough(text):
    assert looks_like_list(text)


def test_strong_word_after_80_chars_does_not_count():
    assert not looks_like_list("а" * 85 + " дайджест")


@pytest.mark.parametrize("word", ["подборка", "Куда пойти", "анонсы на неделю", "анонс недели", "на выходные и на следующую неделю"])
def test_weak_word_needs_eight_times(word):
    assert not looks_like_list(f"{word} в Белграде")                                   # слово без времён
    assert looks_like_list(f"{word} в Белграде. {TIMES}")                              # слово + 10 времён
    assert not looks_like_list(f"{word} в Белграде. " + " ".join(f"{h}:00 x" for h in range(10, 17)))   # 7 времён


def test_many_times_without_words_is_not_a_list():
    """French VR и фестивали с сеансами по часам больше не считаются перечнем."""
    assert not looks_like_list("Фестиваль. Программа: " + TIMES)


def test_weak_word_after_200_chars_does_not_count():
    assert not looks_like_list("а" * 210 + " подборка " + TIMES)


def test_is_list_only_for_whole_posts():
    text = "Дайджест недели. " + TIMES
    assert Record(KIND_MESSAGE, 1, None, "ch:1", text).is_list
    assert not Record(KIND_MESSAGE, 1, None, "ch:1", text, position=2).is_list      # строка дайджеста — своя логика
    assert Record(KIND_MESSAGE, 1, None, "ch:1", text, position=2).is_digest


def test_markers_come_from_config_and_change_signature():
    base = records.list_markers().signature
    m = configure_list_markers({"strong": ["гид по городу"], "weak": ["топ"], "strong_window": 30,
                                "weak_window": 50, "weak_min_times": 2})
    assert looks_like_list("Гид по городу на ноябрь")
    assert not looks_like_list("Дайджест недели")                                      # старые слова заменены
    assert looks_like_list("Топ событий 10:00 и 11:00")
    assert m.signature != base


def test_bad_regex_in_config_is_a_clear_error():
    with pytest.raises(ValueError, match="list_markers"):
        build_list_markers({"strong": ["(незакрыта"]})
