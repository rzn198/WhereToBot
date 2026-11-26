"""Параметры AI киноафиши (секция cinema_ai в config.yaml, см. shared.config._cinema_ai_section)."""
from __future__ import annotations

from dataclasses import dataclass

METHODS = ('off', 'shadow', 'on')


@dataclass(frozen=True)
class CinemaAISettings:
    method: str = 'off'
    max_films_per_run: int = 40       # потолок фильмов на каждый этап за один прогон; остальное — в следующий
    titles_batch: int = 20            # фильмов в одном вызове этапа A
    input_chars: int = 2500           # сколько символов описания отправляем на перевод (в данных максимум ~2000)
    concurrency: int = 3
    retranslate_ratio: float = 1.5    # перевод заново, только если исходное описание выросло во столько раз
    max_fails: int = 3                # столько неудач подряд — фильм пропускаем
    candidate_similarity: float = 0.72  # порог похожести названий для вопроса «это один фильм?»
    max_candidates: int = 3           # сколько похожих известных фильмов показать модели

    @property
    def enabled(self) -> bool:
        return self.method in ('shadow', 'on')


def cinema_ai_settings(cfg: dict) -> CinemaAISettings:
    c = cfg or {}
    d = CinemaAISettings()
    method = str(c.get('method') or 'off').strip().lower()
    return CinemaAISettings(
        method=method if method in METHODS else 'off',
        max_films_per_run=max(1, int(c.get('max_films_per_run', d.max_films_per_run))),
        titles_batch=max(1, int(c.get('titles_batch', d.titles_batch))),
        input_chars=max(200, int(c.get('input_chars', d.input_chars))),
        concurrency=max(1, int(c.get('concurrency', d.concurrency))),
        retranslate_ratio=max(1.0, float(c.get('retranslate_ratio', d.retranslate_ratio))),
        max_fails=max(1, int(c.get('max_fails', d.max_fails))),
        candidate_similarity=min(1.0, max(0.3, float(c.get('candidate_similarity', d.candidate_similarity)))),
        max_candidates=max(0, int(c.get('max_candidates', d.max_candidates))),
    )
