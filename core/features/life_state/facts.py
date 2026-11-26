import copy
from contextlib import contextmanager

from ...common import jsonio

_TRACKED_STORES = ("world", "things", "promises", "trainings", "abilities", "money")
_SNAPSHOT_FIELDS = (
    "_days",
    "_state",
    "_routine",
    "_data",
    "_revision",
    "_dirty_days",
    "_state_dirty",
    "_fact_errors",
    "_mtime",
    "_view_cache",
    "_weather_cache",
    "_sim_cache",
    "_fire_win_cache",
    "_cabin_curve_cache",
)


@contextmanager
def fact_batch(owner, *, preview=False):
    tracked = []
    for name in _TRACKED_STORES:
        store = getattr(owner, name, None)
        if store is None:
            continue
        lock = getattr(store, "_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            fields = {}
            for key in _SNAPSHOT_FIELDS:
                if hasattr(store, key):
                    fields[key] = copy.deepcopy(getattr(store, key))
            tracked.append((store, fields))
        finally:
            if lock is not None:
                lock.release()

    world = getattr(owner, "world", None)
    use_native_drop_buf = (
        world is not None
        and callable(getattr(world, "push_drop_buffer", None))
        and callable(getattr(world, "pop_drop_buffer", None))
    )
    fallback_orig_drop = None
    fallback_drop_events: list[tuple] = []
    fallback_hooked = False
    if use_native_drop_buf:
        world.push_drop_buffer()
    elif world is not None and hasattr(world, "on_drop"):
        fallback_orig_drop = getattr(world, "on_drop", None)
        if callable(fallback_orig_drop):
            def _buffered_drop(*args, **kwargs):
                fallback_drop_events.append((args, kwargs))
            world.on_drop = _buffered_drop
            fallback_hooked = True

    committed = False
    try:
        with jsonio.staged_writes(preview=preview):
            yield
        committed = not preview
    finally:
        if not committed:
            if use_native_drop_buf:
                world.pop_drop_buffer(commit=False)
            elif fallback_hooked:
                world.on_drop = fallback_orig_drop
            for store, fields in tracked:
                lock = getattr(store, "_lock", None)
                if lock is not None:
                    lock.acquire()
                try:
                    for key, value in fields.items():
                        setattr(store, key, value)
                finally:
                    if lock is not None:
                        lock.release()
            if hasattr(owner, "_run_cache"):
                owner._run_cache = {}
        else:
            if use_native_drop_buf:
                world.pop_drop_buffer(commit=True)
            elif fallback_hooked:
                world.on_drop = fallback_orig_drop
                if callable(fallback_orig_drop):
                    for args, kwargs in fallback_drop_events:
                        fallback_orig_drop(*args, **kwargs)
