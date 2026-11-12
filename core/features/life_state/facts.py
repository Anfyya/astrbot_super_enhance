import copy
from contextlib import contextmanager

from ...common import jsonio


@contextmanager
def fact_batch(owner, *, preview=False):
    tracked = []
    for name in ("world", "things", "promises", "trainings", "abilities"):
        store = getattr(owner, name, None)
        if store is None:
            continue
        fields = {}
        for key in ("_days", "_state", "_routine", "_data", "_revision", "_dirty_days",
                    "_view_cache", "_weather_cache", "_sim_cache", "_projection_errors", "_fact_errors"):
            if hasattr(store, key):
                fields[key] = copy.deepcopy(getattr(store, key))
        tracked.append((store, fields))
    committed = False
    try:
        with jsonio.staged_writes(preview=preview):
            yield
        committed = not preview
    finally:
        if not committed:
            for store, fields in tracked:
                for key, value in fields.items():
                    setattr(store, key, value)
            if hasattr(owner, "_run_cache"):
                owner._run_cache = {}
