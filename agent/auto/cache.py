"""Disk cache for slow deterministic planning steps (block layouts, oil units). The key covers the call's
arguments and the source of the modules that compute it, so editing the solver invalidates old entries."""
import functools
import hashlib
import json
import pickle
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent.parent / "runtime" / "auto" / "cache"
SRC = Path(__file__).resolve().parent.parent


def _src_hash(files):
    h = hashlib.sha1()
    for f in files:
        h.update((SRC / f).read_bytes())
    return h.hexdigest()[:12]


def disk(name, files):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args):
            key = hashlib.sha1(json.dumps([_src_hash(files), args], sort_keys=True, default=str).encode()).hexdigest()[:20]
            f = DIR / f"{name}-{key}.pkl"
            if f.exists():
                return pickle.loads(f.read_bytes())
            out = fn(*args)
            DIR.mkdir(parents=True, exist_ok=True)
            f.write_bytes(pickle.dumps(out))
            return out
        return inner
    return wrap
