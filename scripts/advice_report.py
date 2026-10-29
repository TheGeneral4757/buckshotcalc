"""Report over the advice logs (decisions-pending item 16).

    .venv/bin/python scripts/advice_report.py [--resolve] [FILE ...]

Reads temp/advice/*.jsonl (or the files given) and prints calibration, deviations
and the preference fit that sets item 15(b)'s margin `m`. `--resolve` re-solves every
logged state on the current engine, and only when the reload table is already cached.
It never builds one. Keep the `__main__` guard: `Solver.reload_value` spawns workers.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rows(paths: list[Path]) -> list[dict]:
    """Every row of every file, in order, each tagged with its file under ``_file``."""
    rows = []
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                row["_file"] = p.name
                rows.append(row)
    return rows


def rounds(rows: list[dict]) -> list[tuple[float, bool]]:
    """(predicted chance to win at the round's first decision, did we win) per finished round."""
    out, cur = [], None
    for r in rows:
        if r.get("ev") == "load" and r.get("kind") == "new_round":
            cur = {"file": r["_file"], "pred": None}
        elif r.get("ev") == "decision" and cur and cur["pred"] is None:
            if r.get("rec_value") is not None and cur["file"] == r["_file"]:
                cur["pred"] = r["rec_value"]
        elif r.get("ev") == "round_end" and cur and cur["file"] == r["_file"]:
            if cur["pred"] is not None and r.get("winner") in ("player", "dealer"):
                out.append((cur["pred"], r["winner"] == "player"))
            cur = None
    return out


def calibration(rs: list[tuple[float, bool]]) -> tuple[list[tuple[int, int, float, float]], float]:
    """Rows (bin low %, count, mean predicted, actual win rate) and the Brier score."""
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for p, won in rs:
        bins[min(int(p * 10), 9)].append((p, won))
    table = [
        (b * 10, len(v), sum(p for p, _ in v) / len(v), sum(w for _, w in v) / len(v))
        for b, v in sorted(bins.items())
    ]
    brier = sum((p - won) ** 2 for p, won in rs) / len(rs) if rs else 0.0
    return table, brier


def _comparable(r: dict) -> bool:
    return (
        r.get("ev") == "decision" and not r.get("no_rec")
        and r.get("rec_value") is not None and r.get("taken_value") is not None
    )


def _groups(r: dict) -> list[str]:
    hp = r["summary"]["hp"][0]
    taken = r["taken"]
    kind = "shot" if taken.startswith("SHOOT") else "item"
    g = [f"hp={'3+' if hp >= 3 else hp}", f"kind={kind}",
         f"rec_self={'yes' if r['rec'] == 'SHOOT SELF' else 'no'}"]
    if kind == "item":
        g.append(f"item={taken}")
    return g


def deviations(rows: list[dict]) -> dict[str, tuple[int, int, float]]:
    """group -> (decisions, deviations, summed cost) where cost = best value - taken value."""
    out: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for r in filter(_comparable, rows):
        dev = r["taken"] != r["rec"]
        cost = r["rec_value"] - r["taken_value"] if dev else 0.0
        for g in ["all"] + _groups(r):
            out[g][0] += 1
            out[g][1] += dev
            out[g][2] += cost
    return {g: (n, d, c) for g, (n, d, c) in out.items()}


def preference(rows: list[dict]) -> dict[str, dict]:
    """At 1 and 2 HP: deviations, how many took a lower-"dies" move, and those moves' value gaps.

    `m` is the gap covering 80% of the safer deviations (nearest rank): the margin item 15(b)
    would need to reproduce them. None when there are none.
    """
    out = {k: {"dev": 0, "safer": 0, "gaps": []} for k in ("hp=1", "hp=2", "hp=1-2")}
    for r in filter(_comparable, rows):
        hp = r["summary"]["hp"][0]
        if hp > 2 or r["taken"] == r["rec"]:
            continue
        by = {c["action"]: c for c in r["children"]}
        if r["taken"] not in by or r["rec"] not in by:
            continue
        safer = by[r["taken"]]["dies"] < by[r["rec"]]["dies"]
        for k in (f"hp={hp}", "hp=1-2"):
            out[k]["dev"] += 1
            if safer:
                out[k]["safer"] += 1
                out[k]["gaps"].append(r["rec_value"] - r["taken_value"])
    for v in out.values():
        g = sorted(v["gaps"])
        v["m"] = g[max(math.ceil(0.8 * len(g)) - 1, 0)] if g else None
    return out


def resolve(rows: list[dict]) -> tuple[int, int] | None:
    """(re-solved, changed recommendation) on the current engine; None if no table is cached."""
    from buckshotcalc.engine import memo_key
    from buckshotcalc.engine.rules import default_rules
    from buckshotcalc.engine.solver import GameMode, Solver
    from buckshotcalc.engine.state import Player
    from buckshotcalc.tui import analysis

    rules = default_rules()
    solvers: dict[str, Solver] = {}
    done = changed = 0
    for r in filter(_comparable, rows):
        s = solvers.setdefault(r.get("mode", ""), Solver(rules, GameMode[r["mode"]]))
        if not s.has_cached_reload_table():
            return None
        res = s.evaluate(memo_key.decode(r["state"], rules))
        done += 1
        top = res.children[0].action if res.children else None
        best = analysis.action_label(top, Player.PLAYER) if top else None
        changed += best != r["rec"]
    return done, changed


def report(rows: list[dict], do_resolve: bool = False, engine_fp: str | None = None) -> str:
    L = []
    decisions = [r for r in rows if r.get("ev") == "decision"]
    blind = sum(1 for r in decisions if r.get("no_rec"))
    L.append(f"{len(decisions)} decisions, {blind} without a recommendation")
    if engine_fp is not None:
        stale = sum(1 for r in decisions if r.get("engine_fp") != engine_fp)
        if stale:
            L.append(f"WARNING: {stale} rows were logged by a different engine than this one")
    rs = rounds(rows)
    table, brier = calibration(rs)
    L += ["", f"Calibration ({len(rs)} finished rounds, predicted chance at the first decision)"]
    for lo, n, pred, act in table:
        L.append(f"  {lo:3d}-{lo + 10:<3d}%  n={n:4d}  predicted {pred * 100:5.1f}%"
                 f"  won {act * 100:5.1f}%")
    L.append(f"  Brier score {brier:.4f}" if rs else "  Brier score n/a (no finished rounds)")
    L += ["", "Deviations (cost = best value - taken value, in points)"]
    for g, (n, d, c) in sorted(deviations(rows).items()):
        mean = f"{c / d * 100:5.2f}" if d else "    -"
        L.append(f"  {g:<28} n={n:4d}  deviated {d:4d} ({d / n * 100:4.0f}%)"
                 f"  total {c * 100:6.2f}  mean {mean}")
    L += ["", "Preference fit (deviations at low HP)"]
    for k, v in preference(rows).items():
        if not v["dev"]:
            L.append(f"  {k}: no deviations")
            continue
        m = "n/a" if v["m"] is None else f"{v['m'] * 100:.2f} points"
        L.append(f"  {k}: {v['dev']} deviations, {v['safer']} took a lower death chance; "
                 f"gap covering 80% of those: {m}")
    m = preference(rows)["hp=1-2"]["m"]
    if m is not None:
        L.append(f"  suggested safety margin m = {m * 100:.2f} points (item 15(b))")
    if do_resolve:
        res = resolve(rows)
        L += ["", "Re-solve on the current engine"]
        if res is None:
            L.append("  no cached reload table: a build is needed (./start.sh --build); skipped")
        else:
            L.append(f"  {res[0]} states re-solved, {res[1]} recommendations differ from the log")
    return "\n".join(L)


def main() -> None:
    args = sys.argv[1:]
    do_resolve = "--resolve" in args
    paths = [Path(a) for a in args if a != "--resolve"] or sorted(
        (ROOT / "temp" / "advice").glob("*.jsonl")
    )
    if not paths:
        print("no advice logs found")
        return
    from buckshotcalc.engine.solver import _engine_fingerprint

    print(report(load_rows(paths), do_resolve, _engine_fingerprint()))


if __name__ == "__main__":
    main()
