"""Install the reload tables shipped in `buckshotcalc/tables/` into the user cache.

The files are named exactly like the solver's cache files, which embed a hash of
`rules.toml` and of the engine code. A shipped table therefore only matches (and
is only ever loaded) while both are unchanged; otherwise it is ignored and the
table is rebuilt. Kept out of `engine/` so it never changes the engine hash.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from buckshotcalc.engine.solver import _cache_dir


def install() -> int:
    """Copy shipped tables missing from the cache. Returns how many were copied."""
    src = Path(__file__).parent / "tables"
    dst = _cache_dir()
    n = 0
    for f in sorted(src.glob("reload_*.json")):
        if not (dst / f.name).exists():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst / f.name)
            n += 1
    return n
