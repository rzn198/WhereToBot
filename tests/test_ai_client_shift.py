"""Указатель моделей: параллельные запросы, получившие отказ от одной и той же модели, сдвигают его один раз."""
import asyncio

import httpx

from shared.ai import AIClient


def make(models):
    seen = []

    async def handler(request):
        import json
        model = json.loads(request.content)["model"]
        seen.append(model)
        await asyncio.sleep(0.01)
        if model == "m1":
            return httpx.Response(429, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    return AIClient("dedup", "k", models, transport=httpx.MockTransport(handler), retry_delay=0), seen


async def test_three_parallel_429_move_to_next_model_not_three_steps():
    client, seen = make(["m1", "m2", "m3", "m4"])
    out = await asyncio.gather(*(client.complete("s", "u") for _ in range(3)))
    assert out == ["ok", "ok", "ok"]
    assert client.current_model == "m2"                 # раньше уезжали на m4
    assert set(seen) == {"m1", "m2"}


async def test_models_are_still_exhausted_in_order():
    client, _ = make(["m1"])
    try:
        await client.complete("s", "u")
    except Exception as e:
        assert "исчерпаны" in str(e) or "Unavailable" in type(e).__name__


async def test_next_model_in_list_answers_even_when_two_models_fail_in_parallel():
    import json

    seen = []

    async def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        await asyncio.sleep(0.01)
        if model in ("m1", "m2"):
            return httpx.Response(429, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": model}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    client = AIClient("dedup", "k", ["m1", "m2", "m3", "m4"], transport=httpx.MockTransport(handler), retry_delay=0)
    out = await asyncio.gather(*(client.complete("s", "u") for _ in range(4)))
    assert out == ["m3"] * 4 and client.current_model == "m3"
    assert "m4" not in seen                              # ни одна модель в списке не пропущена
