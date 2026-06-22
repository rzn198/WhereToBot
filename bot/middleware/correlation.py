import contextvars

_update_id_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    'update_id', default=0
)


def set_current_update_id(update_id: int) -> None:
    _update_id_ctx.set(update_id)


def get_current_update_id() -> int:
    return _update_id_ctx.get()
