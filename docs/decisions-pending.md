# Pending engineering decisions

**Internal engineering record — not published with the public repo.**

Open decisions that are *ours*, not the game's. Game-rule uncertainty belongs in
[`SPEC.md`](../SPEC.md) §15 and is resolved by editing `rules.toml`; this file is
for engineering choices that implementation surfaced, that have a real tradeoff,
and that nobody has yet made. Each entry records what was measured, what the
options cost, and what is blocked until it is settled.

Add to this file rather than deciding in code. Remove an entry when it is
settled, and say where it was settled.

---

## 1. The reload precompute tuning dial — cost against accuracy

**Status: provisionally 64 (shipped). Operator, 2026-10-24: "start with
shipped for now", then come back with a recommendation for rebuilds and what
they buy once the M8.5 follow-up speed-ups have landed. The §12.1 run no
longer waits on this — it runs against the 64-sample table (item 3).**

`solver.reload_item_draw_samples` sets both how long the fixed point takes and
how accurate its table is, and the two cannot be set independently:

| samples | per sweep | table standard error |
|---|---|---|
| 64 (shipped) | ~19 min | ±0.0067 |
| ~113 | ~34 min | ±0.005 |

SPEC §12.1 accepts `|predicted − empirical| < 0.01`. At the shipped 64 samples
the reload table alone consumes two thirds of that budget before the search
contributes any error at all. Sweeps to convergence: **unknown — no full-scale
run has ever completed.** See SPEC §7.7 for the full measurement table.

This is a one-time cost whose result lands in the fingerprint-keyed disk cache,
so hours may well be acceptable. Nobody has decided.

**The table's error is a fixed offset, not noise — measured this session.**
This sharpens what the ±0.0067 figure above actually means, and it is the
part most likely to be misread. `Solver._draw_samples` seeds the item-draw
sample from `rules.fingerprint` alone, so for a given `rules.toml` the table
has *one* realization of its error. That error is identical for every
position and every Monte Carlo seed in a run, and it therefore does **not**
average away across a validation run the way independent noise would — a
100-position × 100k-trial acceptance run is not 100 independent looks at the
table's accuracy, it is 100 looks through one fixed lens. It is also
re-rolled by *any* edit to `rules.toml`, however unrelated to the solver.

Measured at `SMALL_OVERRIDES` scale, varying only the sample count (which
re-rolls the draw and shrinks its error together), against the empty-rack
control arm of `measure_reload_carryover_bias`:

| samples | control bias |
|---|---|
| 4 | −0.0344 |
| 8 | +0.0276 |
| 16 | −0.0145 |
| 32 | +0.0013 |
| 64 | +0.0079 |

This is what makes decision 1 a real decision rather than a formality: for a
§12.1 run with a ±0.01 budget, the question is not "is the table's error
small on average" but "did this particular rules file draw a good sample".
Raising the sample count is the only lever that shrinks it; re-running with a
different Monte Carlo seed does not.

It also caused a live test failure — `test_reload_carryover_bias_...`'s
control assertion was passing on the hash of the rules file and started
failing the moment an unrelated dealer patch changed it. Fixed by pinning
that test to 32 samples; see its docstring.

**2026-10-26: keep 64 — operator approved the audit's recommendation.** DoN builds in ~26 min at 64; a
sweep scales about linearly with samples, so 128 is ~50 min for ±0.0047 and 256
~100 min for ±0.0034. That buys at most ~0.2-0.3 points. The modelling errors
are an order of magnitude larger: the Inverter leak just fixed (mean +3.0, max
+33 points over Inverter positions) and §15.13's carried-item blindness
(+0.03 to +0.09, settled below). Spend build time on §15.13 before samples.

## 2. Bounding the fixed point's memo — measured, not applied

**Status: SETTLED — superseded, no action needed.**

SPEC §7.7 item 1 (the parallel sweep) shipped in M8.5, and it bounds peak
memory the same way this entry's per-key clearing would: each worker holds
only its own key's memo. This entry predicted exactly that outcome
("composes with SPEC §7.7's item 1 … and would make this redundant"), and
that is what happened — a full 29-key sweep now completes. The +20% time
cost this entry would have paid is not paid.

The original entry:

The constraint on the precompute is memory, not CPU. Measured: 869k memo
entries for one table key; 2.09M for four keys sharing one memo; ~15M
extrapolated across all 29. Clearing the scratch memo per table key caps peak at
~869k for **+20% time, −58% peak memory**.

This is what turns "cannot complete" into "completes" and is close to
unambiguous, but it is still a deliberate tradeoff on committed code, so it is
recorded here rather than applied unilaterally. It composes with SPEC §7.7's
item 1 (parallelizing across keys), which bounds memory the same way as a side
effect and would make this redundant.

## 3. SPEC §12.1's acceptance criterion has never actually been run

**Status: PASSED in both modes, 2026-10-25** (64-sample tables, after the
2026-10-25 rules corrections). Numbers in "Measured runs" below. The rest of
this entry is the original reasoning.

What changed: the M8.5 performance work (SPEC §7.7 items 1 and 2 — the
parallel sweep and the bit-packed memo key) landed, and a full 29-key sweep
now completes rather than dying on memory. The acceptance run is a decision
to spend hours, not an engineering blocker.

M5's harness is built and its sensitivity is characterised, but the headline
check — 100 positions × 100k trials against shipped `rules.toml` — has not been
executed, because it blocks on the precompute above. Everything measured so far
is at `SMALL_OVERRIDES` scale.

Consequence to keep in view: **a zero-discrepancy result at reduced scale is not
the same as a passing acceptance run.** At 3000 trials/position the harness
detects errors of roughly ±0.03; at the mandated 100k it would detect ±0.005.
Errors between those two figures are currently invisible to us.

## 4. §15.9's Dealer-phone bias is unmeasured, not measured-as-zero

**Status: open.**

M5 measured the §15.9 bias at `SMALL_OVERRIDES` scale and found it
statistically indistinguishable from zero. That is the expected result whether
or not a real effect exists: the effect only fires when the Dealer's phone
triggers early count-closure, which is rare at small shell counts. It is
evidence of nothing yet, and SPEC §15.9 records that the bias runs *upward*.

Re-measure at production scale once decision 1 is settled. Do not let the
reduced-scale null propagate into anyone's confidence.

## 5. Whether the TUI should auto-start the reload-table build in the background

**Status: SETTLED — re-decided by the operator, 2026-10-24: an opt-in
ctrl-chord build, still no auto-build at startup.** The operator picked the
opt-in build when asked again: a ctrl-chord starts the table build in the
background, the status line shows its progress, and recommendations appear
when it finishes. Startup behaviour is unchanged, so the machine is still
never committed to a build nobody asked for. This supersedes the
"separate act only" half of the call below. Implementation follows the
build speed-ups (item 13).

The earlier call (Sep 2026): the TUI keeps the explicit yellow "RELOAD TABLE NOT BUILT" status and never
commits the machine to a build the operator did not ask for. Building the
table is now a deliberate, separate act (`sim/reload_bench.py`, or any
solver call in a guarded harness), and its result is cached to disk, so the
out-of-the-box gap this entry worried about closes the first time anyone
runs the precompute rather than on every session's startup.

The original entry, for the reasoning that was weighed:

The TUI (`buckshotcalc/tui/app.py`) checks `Solver.has_cached_reload_table()`
before ever calling `evaluate`/`value`, and never triggers `_compute_reload_table`
itself: with no cache on disk it shows a persistent yellow "RELOAD TABLE NOT
BUILT" status and stays there for the whole session rather than attempting
(and hanging on, per decision 1's numbers) a build.

The alternative — kick off `solver.reload_value(...)` in a background thread
worker at startup, non-blocking by construction (`asyncio.to_thread`) — was
considered and declined for this milestone. It would not violate the "never
block the UI" constraint (SPEC.md §7.5/§7.7: it is not on the render or event
path either way), but it silently commits the machine to burning a full core
for the length of the ~19-minute-per-sweep, never-yet-completed build, every
session, with no operator opt-in. That is a real cost/UX tradeoff — CPU/battery
against "recommendations become available if you leave it running long enough"
— and SPEC.md §0.1 is explicit that this kind of dial belongs here, not in a
silent default buried in `app.py`.

Nobody has decided whether the auto-build default is worth it, or whether it
should instead be an explicit operator action (which would need a new keybind,
since §9's table is otherwise full — also unresolved, see SPEC.md §15.15's
sibling problem of extending the input model). Blocks: recommendations are
unavailable out of the box against the shipped `rules.toml` until either this
is decided or decision 1 is settled and a table is committed/cached ahead of
time.

## 6. Our belief cannot represent a mixture, and two Dealer actions need one

**Status: SUPERSEDED 2026-10-26 for the Inverter.** The mixture turned out to
need one bit, not a set of states: `ChamberState.inverted0` (SPEC.md §5.2)
represents an unseen flip exactly, so `Session` no longer degrades and the
recommendation stays on. `Session.degraded` is kept as a channel but nothing
sets it now. The Phone half below is unchanged.

**Previous status: SETTLED as option (a) — implemented. Operator's call, this session.**

`Session` now refuses the unknowable entry and marks the chamber degraded
until an observation collapses it (`Session.degraded`, `DEGRADED_INVERTER`).
The TUI replaces the whole recommendation block with a `BELIEF DEGRADED`
notice rather than annotating a number, because the operator cannot tell a
wrong `P(win)` is wrong by looking at it. The flag clears automatically on
the next real observation of slot 0 — a fired shell, a Glass, a Beer — and
`undo` restores it, so a mis-key does not strand the session in a degraded
state. Tests: `tests/test_session.py` (six cases) and
`tests/test_tui.py::test_degraded_belief_suppresses_the_win_probability`.

Scope note, decided while implementing rather than inherited from the entry:
the degradation applies to **either seat's** Inverter on an unresolved shell,
not just the Dealer's. The entry and SPEC.md §15.16(b) single out the
Dealer's, but the epistemics are identical — no one learns a shell by
inverting it, so the operator cannot answer "what was it before?" for their
own Inverter either, and M7 was prompting for it in both cases. Narrowing it
back to the Dealer's would mean deliberately keeping a prompt we know invites
a guess.

Option (b) — a weighted set of candidate states — remains the correct model
and remains unbuilt; (a) is a containment, not a solution. The Burner Phone
half of this entry (§15.16(a)) is untouched and still takes an operator's
guess.

The original entry, for the options that were weighed:

`GameState` holds one chamber, so our belief is always a single point. That is
correct for everything we observe, and for the Dealer's Glass (private value,
*known* slot). It is wrong for the Dealer's Inverter on an unresolved shell:
that is a chance node we do not observe, so our true posterior is a mixture
over both branches. M7 asks the operator for the pre-flip value they cannot
know, which fabricates knowledge rather than merely misplacing it.

Options, unmeasured: (a) forbid the entry and mark the chamber degraded until
the next observation collapses it; (b) let `Session` hold a weighted set of
candidate `GameState`s and have the TUI report an averaged `P(win)`; (c) accept
the over-commitment and document it. (b) is the correct model and the most
invasive — it changes what "the state" means everywhere above the engine.

Same shape, lower stakes: the Dealer's Burner Phone (§15.16(a)) picks a slot we
cannot see, so `dealer_knows` got an operator's guess. **Settled this session
on the operator's instruction** — they cannot see it, so they are not asked.
The prompt is gone; `Session.record_dealer_phone` uses
`engine.dealer.dealer_phone_target`, which is item 10 below and still open.
The guess did not disappear, it moved from the keyboard into a documented
model the search shares.

## 7. M8's item-value sheet: what "common positions" means, chosen not measured

**Status: settled for v1's generated sheet; open to revisiting the family later.**

SPEC.md §14 asks for a "generated cheat sheet for common positions" and
defines "common" nowhere, and the M8 brief is explicit that inventing
a definition silently, or padding the sheet to look comprehensive, is worse
than a narrow sheet that says what it covers. `buckshotcalc/sim/tables.py`
picked this family, stated on the sheet's own header, not just in code:

- A **fresh, unresolved load** (no chamber slot resolved yet) — the moment an
  operator most plausibly wants a static reference, before any observation
  has narrowed the position past what the sheet already encodes.
- **Both players at full HP** (`hp = hp = max_hp`), for every `max_hp` in
  `rules.health.possible_max_hp` (2, 3, 4 at shipped defaults) — the start of
  a round/reload, not a fixed HP the player will not have.
- The **3 smallest legal shell totals** (`_POSITION_TOTAL_COUNT`, currently
  2/3/4 at shipped `shells.min_total=2`) and every legal split of each —
  early-round shell counts are what "common" plausibly means; the full legal
  range runs to `shells.max_total=8`, more than triples the position count,
  and was not included.
- The player holds **exactly one item at a time** (from
  `rules.items.base_items` only — `GameMode.BASE`, not Double or Nothing or
  multiplayer), compared against a no-item baseline at the same position; the
  Dealer holds nothing.

What this deliberately excludes, so nobody mistakes the sheet for more than
it is: mid-round positions with a resolved chamber slot or reduced HP, item
*combinations* (saw+cuffs synergy, Adrenaline's steal target — there is never
a second item to steal), and Double or Nothing / multiplayer's item lists.
None of these were measured against alternatives; this is a scope choice, not
a result. At shipped `rules.toml` this produces 12 positions x 5 items = 60
valuation rows plus 12 baselines, generated in well under a second (the
reload boundary always uses the injected health-ratio stand-in table, never
the real, uncompleted `V_reload` precompute — see items 1-3 above and
SPEC.md §7.7). If the covered range turns out to be the wrong one in
practice, widening `_POSITION_TOTAL_COUNT` or adding a resolved-slot /
partial-HP family are the two obvious next moves, each a straightforward
extension of `enumerate_positions`.

## 8. `sim/` now depends on `tui/`, and the layering says it should not

**Status: open, small. Introduced in M8.**

`sim/tables.py` imports `action_label` and `item_valuations` from
`buckshotcalc/tui/analysis.py`. This is legal today — `analysis.py` is
stdlib-only by design — and it avoids duplicating two small derivations. But
it points the wrong way: `sim/` is an offline harness and `tui/` is the UI,
and SPEC.md §0.1 (3) names `tui/` as the one directory where third-party deps are
allowed to live. A future `import rich` in `analysis.py` would make `sim`
un-importable headless.

`tests/test_layering.py` now pins `tui/session.py` and `tui/analysis.py` as
stdlib-only, so the hazard is guarded rather than latent, and the guard is
mutation-tested. The open question is whether to leave it guarded or remove
the edge — by moving the shared derivations somewhere both layers can sit
above, which means adding a module to the layout SPEC.md §11 fixes, or by
letting `tables.py` derive the two values itself. Neither is urgent; the
guard is what made it safe.

## 9. Modelling the Dealer's item-rack order — cost against what the fixed-list approximation buys

**Status: open. Introduced by the correctness patch that pinned down the
Dealer's real item-selection order.**

Observation settles SPEC.md §3.4's "priority order" as a misnomer: the
Dealer walks its own rack in the order items were **dealt** to it, and fires
the first item whose own condition holds. There is no global priority; `[cigarettes, glass]` uses
the cigarettes, `[glass, cigarettes]` uses the glass, with both conditions
identical. `rules.toml`'s fixed `item_priority` list is a global order and
cannot express this by construction, no matter how the list is tuned.

The operator has decided, for v1: keep the fixed-list approximation, and
document it as a known, deliberate approximation rather than an open
question (SPEC.md §3.4/§15.2 now say so).

What modelling it exactly would cost, roughly assessed rather than measured
(no prototype built): item acquisition order would need to become part of
Dealer state — e.g. `items: tuple[Item, ...]` (ordered) instead of
`ItemCounts` (SPEC.md §5.3 is explicit that item order is *deliberately*
unordered "because treating it as ordered would explode the state space",
which is exactly the cost here) for the Dealer's rack specifically, doubling
as a second representation alongside the player's (still-unordered)
`ItemCounts`. Every dealt item, adrenaline steal, and item-use would need to
thread rack-order bookkeeping. The memo key would grow by up to `8!` orderings
of an 8-slot rack in the worst case (in practice far fewer, since most racks
hold fewer than 8 distinct items and ties among same-type items are
order-irrelevant, but the combinatorics are real for any rack with 4+ distinct
held types).

What the approximation buys: a `GameState` that stays a plain count vector,
the existing memo table's size, and `dealer.item_priority` staying a single
config-driven list a reviewer can read in one glance rather than a derived
property of dealt-item history. The correctness cost is bounded to positions
where the Dealer holds 2+ items whose conditions would both currently fire in
the *wrong* relative order versus its real rack — unmeasured, but SPEC.md
§15.2 has carried this as "moderate impact" since M1 without anyone
discovering it changes that estimate.

Revisit for v2, or sooner if M6's agreement corpus or M5's Monte Carlo
cross-check ever surfaces a real-play line where the fixed order visibly
diverges from a rack the operator can see.

**Audit examples (decompile re-read, `DealerIntelligence.gd:150-205`):** the
loop tests one rack slot per iteration and `break`s on the first slot whose own
item condition holds. So with the Dealer below max HP and the shell unknown,
rack `[beer, cigarettes]` ejects a shell blind before healing, where our fixed
list heals first. More consequentially rack `[beer, magnifying_glass]` beers
first (`knownShell != "live"` holds while the shell is unknown), possibly
ejecting a live shell it would have seen with the glass, whereas our order
glasses first and then keeps a known live shell. The reverse rack
`[magnifying_glass, beer]` matches ours. Both directions are position-dependent;
still unmeasured.

## 10. The Dealer's own Burner Phone target index — a fixed guess, not the true draw

**Status: open — and now load-bearing in two places, not one.**

Introduced by the dealer-knowledge fix (`dealer.py`'s `dealer_phone_target`,
made public this session). It used to govern only what the *search* assumed
about future Dealer phone uses; since §15.16(a) was settled it also governs
what the *live session* records when the operator watches the Dealer use one.
That is a strict improvement in consistency — the two could previously
disagree — but it raises the value of measuring the fixed choice, because a
single wrong assumption now shows up in both halves of the tool rather than
being averaged against an operator's (equally unfounded) guesses.

`docs/dealer-knowledge.md`/SPEC.md §15.9 settle *that* the Dealer's own
Burner Phone must mark an index in `dealer_knows` without resolving
`state.chamber`, and that no chance node may open at the moment the Dealer
*acquires* the value (only when its policy later *consults* slot 0). Both are
implemented. What neither document settles is *which* index `1..n-1` gets
marked, given the real mechanic draws uniformly over all of them (including
already-resolved ones, which waste the draw) and we never observe the draw at
all — not even which slot it hit, per SPEC.md §15.9's addendum ("we still
never see which slot was drawn").

Branching `dealer_step` over that uniform draw would brand the search with
knowledge the player cannot use to distinguish branches, i.e. it would
reopen exactly the strategy-fusion hole `docs/dealer-knowledge.md` exists to
close, since the branches would differ only in a fact never disclosed to us.
So `dealer_phone_target` picks a single, fixed index deterministically:
index 1, the slot nearest the chamber. This is an engineering guess, not a
modelled distribution — same shape as item 6's Inverter/mixture problem
(`Session` cannot represent a belief either), one level more understated.

Unmeasured, and now worth more than it was: how much the fixed choice of
"nearest slot" (rather than, say,
farthest, or excluding already-resolved slots by construction) moves
`P(win)` versus the true uniform draw. SPEC.md §15.9 already accepts *some*
understatement here as bounded (Double or Nothing only, Dealer holds a
phone, matters only when count-closure would otherwise have fired early);
this decision is about which *direction* the fixed approximation leans
within that accepted budget, and nobody has measured it. The alternatives —
model a belief over the Dealer's private draw instead of a point (as item 6
proposes for the Inverter/TUI case), or measure several fixed choices against
Monte Carlo and pick the closest — are both real options, neither attempted.


**Audit confirmation (decompile re-read):** the game's Dealer phone really is
uniform over `1..len-1` (`DealerIntelligence.gd:187-190`:
`randi_range(1, len - 1)`, with only `8 -> 7` clamped, which a chamber of at
most 8 shells can never reach). Unlike the *player's* phone
(`BurnerPhone.gd:14-16`, which also shifts 7 -> 6 and is now modelled in
`actions._use_burner_phone` via `burner_phone.player_index_decrement_from`),
there is no non-uniformity to model here. The fixed index-1 choice is
therefore still purely our approximation of a uniform private draw; this
decision stays open and unchanged.

## 14. The item-use cap: a false warning, and a memo key it fragments

**Status: measured 2026-10-25, not applied. Waiting on the operator.**

The operator saw "SEARCH CAP REACHED" mid-round, plus a long think. The cap is
`[solver] max_items_per_turn = 6` (ASSUMED, ours; SPEC.md §7.5, §15.5). Items
are finite (each use consumes one), so the search terminates without it.

Measured on 24 random Double or Nothing positions (stub reload table, single
process each, cap 6 vs cap 16, which no rack can reach):

| | result |
|---|---|
| positions where cap 6 bound | 10 of 23 finished |
| value change at cap 16 | 0.0 in every position |
| top move change at cap 16 | none |
| cost change at cap 16 | -15% to +20% nodes/time, typically +1% |
| heavy racks (6-8 items a side) | 2 s - 48 s; one over 90 s at either cap |

`items_used_this_turn` is packed into the memo key only to serve the cap.
Decoding the memo after a cap-16 solve and zeroing that field leaves
**0.38-0.63** of the entries distinct (4 heavy positions), so dropping the cap
*and* the field from the key would cut heavy-rack searches roughly 1.6-2.6x.

Options:
- **a. Do nothing.** Warning stays; numbers are already exact in practice.
- **b. Raise the cap to 16.** Warning never fires. Edits `rules.toml`, so the
  fingerprint changes and the reload table must be rebuilt (the hour-plus,
  every-core build).
- **c. Remove the cap and drop `items_used_this_turn` from the memo key.**
  Same rebuild, plus SPEC §7.5 and `memo_key` changes; the 1.6-2.6x speed-up
  also applies to the rebuild itself.

Recommendation: **c**, run the rebuild when the operator says go.

## 12. Warm-starting the fixed point from a cheaper table — unbuilt

**Status: approved for implementation by the operator, 2026-10-24, as part
of item 13. Unmeasured until it lands.**

`_compute_reload_table` seeds `v0` with the health ratio `hp_p / (hp_p +
hp_d)`, which is a crude starting point: the first sweep's residual is ~0.42.
A converged low-sample table is a far better one — the 4-sample and 64-sample
fixed points differ only by sampling error (~0.027 vs ~0.0067), so seeding the
64-sample build from a converged 4-sample table should start it around 3e-2
rather than 4e-1, skipping roughly the first four sweeps.

Rough arithmetic, not measured: the 4-sample build costs 1/16 of a 64-sample
one, so the cascade would pay ~0.75 of a 64-sample sweep to skip ~4 of them —
a net saving around 25-30%, on top of decision 11's 42% and PyPy's 2.5x.

Nobody has built it. It needs a `v0` override threaded into
`_compute_reload_table` plus a cache-key convention for the coarse table, and
it is the only item here that needs real code rather than a config value or a
different interpreter.

## 13. Build and validation speed-ups — approved, being implemented

**Status: a-e all built (2026-10-25). Full-scale timings in "Measured runs"
below.**

Picked from a list offered before any further long build. Starting numbers:
BASE takes ~1.5 min per sweep under PyPy with 16 workers. The 64-sample DoN
build is estimated at ~8 min per sweep. The §12.1 harness is single-core at
~23 s per position (100 × 100k trials, PyPy).

| # | Speed-up | Answer changes? | Estimate before measuring |
|---|---|---|---|
| a | Finer tasks: one per (key, shell config), heaviest first | no, bit-identical | 15–30% (idle cores at the end of each sweep) |
| b | Per-worker memo kept across keys within one sweep | no, bit-identical | unmeasured |
| c | Aitken extrapolation of the linearly converging residual | yes, by less than the tolerance | 30–40% (7 → 4–5 sweeps) |
| d | Warm start from a converged 4-sample table (item 12) | yes, by less than the tolerance | 25–30% |
| e | §12.1 positions spread over every core | different random stream, still deterministic | ~38 min → ~3–4 min per mode |

Measured 2026-10-25, a and b together (BASE, 64 samples, PyPy, after the
2026-10-25 rules corrections): 9 sweeps, 11 min 05 s, ~71 s per sweep
(was ~90 s), peak RSS 583 MB. Each sweep's only progress line reads
"1/29 keys 72s in", printed just before the sweep ends: either the progress
counter is wrong or one key dominates the sweep. Checked 2026-10-25: neither.
Tasks are heaviest-first across all keys, so every key's lightest shell config
runs last and the keys all complete in one burst at the sweep's end. The
counter now counts finished tasks (scaled to keys), one event per task.

c and d landed 2026-10-25 behind `solver.reload_aitken` and
`solver.reload_warm_start_samples` (both default on; the coarse table caches as
`<fingerprint>+samplesN`). Small-scale main-sweep counts (SMALL_OVERRIDES,
tolerance 1e-3, serial; the warm start's own coarse sweeps cost 1/4 to 1/8 of a
sweep each at 2 samples):

| draw samples | plain | Aitken | warm start (2) | both |
|---|---|---|---|---|
| 4 | 12 | 12 | 11 | 8 |
| 8 | 13 | 12 | 12 | 10 |
| 16 | 11 | 10 | 10 | 8 |

Every variant lands within the tolerance of the fully converged table. The
gain is smaller than the 30-40% / 25-30% estimates above: Aitken only fires
once two residual ratios agree (the early sweeps are not yet linear), and
with a 1e-3 tolerance the plain run is already close to done when it does.
Full-scale numbers are for the operator's run. Toggling either key changes the
`rules.toml` bytes, hence the fingerprint and so the item-draw seed.

f (2026-10-26): after an engine-only edit, the build seeds from the newest cached
table of the same mode and rules fingerprint (an older engine's file) before
falling back to the 4-sample warm start. Same rules means the same item-draw
sample, so the old table sits close to the new fixed point. It only seeds: the
stop test is still the true residual. A `rules.toml` edit re-rolls the draw and
finds no stale file. Unmeasured at full scale; the test shows fewer sweeps from
a converged stale table at small scale.

The operator also asked for the build's ETA output to be a tidy scrolling
log: aligned columns, one row per sweep, timestamps and a wall-clock ETA.
That makes it a cleaner CLI, not a TUI.

---

## 15. A safety preference at low HP — the operator's call

**Status: (a) BUILT 2026-10-26 (`analysis.death_chance`, shown as "dies N%" in the alternatives and the watch side list); (b) waits on item 16's data.
Raised by the operator, 2026-10-26.** They would rather take the
safer line than the statistically better one, especially at 1 HP (e.g. not
shooting themselves at ~40% live even when that maximizes `P(win)`).

What the maths says first: a round is binary and health never carries across
rounds (SPEC §15.7), so a line with lower `P(win)` loses more rounds and more
Double or Nothing runs. "Safer" only pays if the numbers are wrong. They were
wrong in one flattering place until today (the Inverter leak, SPEC §5.2), and
still lean optimistic in two (§15.9 Dealer phone; the worst-10 rows of the
2026-10-25 §12.1 run all read +0.001 to +0.003 high).

Options, none built:

- **(a) Show, don't change.** Add "dies this turn" (the chance this action
  ends with us at 0 HP before the Dealer moves) beside each ranked action.
  The operator weighs it. Zero cost to `P(win)` unless they choose to.
- **(b) Safety margin.** Recommend the lowest-immediate-death action among
  those within `m` points of the best (`m` in settings, default 0 = today).
  Costs at most `m` per decision, typically far less. Display-only: no table
  rebuild.
- **(c) Risk-weighted objective.** Penalize death inside the search. Rebuild
  per setting, and no longer the `P(win)` §1.1 locks in. Not recommended.

Recommendation: (a) now, (b) once item 16's log shows how often the operator
deviates and what it costs them.

## 16. Advice log — recommendation vs what the operator did

**Status: BUILT 2026-10-26: writer `tui/advice.py` (hooked in `app.py` `_run_bridge`; `--advice FILE`, `--no-advice`), report `scripts/advice_report.py` (not `sim/`). Requested by the operator.**

Today only the raw bridge stream is saved (`temp/games/*.jsonl`); the
recommendation lives in memory (`RecapRow`) and dies with the round.

Plan:

1. **Record** at `app.py` `_run_bridge.handle`, where `_recommendation_for` already
   pairs the pre-event state with the best action, before `apply_event`. One
   JSON line per player decision to `temp/advice/<same stamp as the game
   log>.jsonl`: game file + `seq`, rules and engine fingerprints, the state
   (`memo_key.encode` plus a readable summary: counts, HP, items, known
   slots), every ranked child `(action, value)`, the action taken and its
   value, and why a row has no recommendation when it does not (stale
   result, withheld, no table). Item 15(a)'s immediate-death figure per
   child, once built. A `round_end` row with the winner.
2. **Report** with `sim/advice_report.py` (stdlib), over all logs:
   - *Calibration:* bucket predicted `P(win)` at each round's first decision
     against actual outcomes. This is the only check against the real game;
     §12.1's Monte Carlo shares the engine's rules, so it cannot catch a rule
     error (it passed with the Inverter leak in place).
   - *Deviations:* rate and summed cost (`best - taken`) by situation: HP,
     shell count, shoot vs item, which item.
   - *Preference fit:* e.g. at 1 HP, how often the operator takes the
     lower-death move and at what gap; that gap is item 15(b)'s `m`.
   - *Re-solve:* rerun old states on the current engine, so engine fixes
     are not mistaken for operator mistakes.
3. **Sample size:** deviation patterns read after ~50 deviations; calibration
   needs a few hundred rounds for ±5-point buckets.

## Measured runs

2026-10-25, PyPy, 16 workers, shipped `rules.toml` (fingerprint `1c9efb0b...`,
64 samples, tolerance 1e-3, Aitken and 4-sample warm start on).

| Run | Result |
|---|---|
| BASE reload build | 6 sweeps, 7 min 25 s, peak 558 MB (was 9 sweeps / 11 min 05 s with a+b only, 8 / 11 min 53 s before) |
| DoN reload build | warm start 7 coarse sweeps (~2.7 min), then 4 main sweeps, 26 min 19 s total, peak 2.0 GB (estimated ~1 h before) |
| §12.1 BASE, 100 positions x 100k trials | **PASS**, 0/100 over 0.01. Worst error 0.0031, worst 2.05 sigma. 3 min 10 s |
| §12.1 DoN, 100 positions x 100k trials | **PASS**, 0/100 over 0.01. Worst error 0.0028, worst 2.60 sigma. 4 min 33 s |

Observation, not a failure: every one of the worst-10 rows in both modes has
predicted above empirical, by +0.001 to +0.003. A worst-by-error list is
biased toward one sign only if there is a real lean, so this is likely a small
upward bias, consistent with SPEC §15.9 and the reload table's fixed offset
(item 1). It is inside the budget; item 4's production-scale measurement would
size it.

Logs: `temp/build_{base,don}.log`, `temp/accept_{base,don}.log` (not tracked).

Aitken and warm start checked against the true fixed point, 2026-10-25, at
`SMALL_OVERRIDES` scale with the item-draw sample pinned (same draw for every
variant; tolerance 1e-10, 400 sweeps as truth): plain 1e-3 is 1.4e-4 off,
Aitken 1e-3 is 6.3e-5 off, Aitken + 2-sample warm start 1e-3 is 3.7e-4 off.
All inside the tolerance. An unpinned comparison looked like a 0.15 gap; that
was entirely item 1's re-roll (any key edit changes the fingerprint and so the
draw), which is also why the slow monotonicity test flipped and is now pinned.

## Settled

- **Decision 11, `reload_fixedpoint_tolerance = 1e-3`**: settled by the
  operator, 2026-10-24, when asked in session, and already in `rules.toml`.
  Measured: 7 sweeps instead of 12 (42% less build time). The iteration error
  stays ~13× under the ±0.0067 sampling error, and the remaining distance to
  the true fixed point is ~3e-4. The operator wants new tolerance options
  recommended again once the speed-ups (item 13) land. The full entry is
  below.

- **§15.13, the reload table's blindness to carried-over items** — measured in
  M5 with an empty-rack control arm: the net effect is **+0.03 to +0.09,
  positive**, i.e. the table systematically *undervalues* holding items into a
  reload. Direction settled; magnitude at shipped scale still unknown (blocked
  on decision 1). Recorded in SPEC §15.13 and in `sim/montecarlo.py`.
- **SPEC §3.3's "beer in the Dealer's hands usually helps you"** — found in M6
  to be position-dependent, with the sign flipping between a tied count and a
  live majority. Recorded as SPEC §15.14; it is a correction to the advice,
  not to the rules, so it needs no `rules.toml` key.
- **M7's two interaction affordances** — arrow-key navigation and the `[?]`
  help overlay were both left unbuilt by the milestone and are now done.
  Arrows walk whatever is choosable (a prompt's options, else the mover's
  held items) and `enter` submits the selected key through the ordinary key
  dispatch, so an arrow can never reach a branch a keypress could not; the
  selection is rendered, and mono-tested, because a cursor the operator
  cannot see is not navigation.
- **Native (Rust) engine core** — considered and declined for v1; the reasoning
  and the conditions under which it could be revisited are in SPEC §7.7.

<details><summary>Decision 11 as it was recorded while open</summary>

### 11. `reload_fixedpoint_tolerance` converges four orders of magnitude below the table's own noise

**Status: open, recommended. Measured this session. Settle it BEFORE any long
build — it changes `rules.toml`, and therefore the fingerprint, and therefore
invalidates every cached table.**

`solver.reload_fixedpoint_tolerance` ships at `1e-6`. The table being
converged has a sampling standard error of **±0.0067** at the shipped 64
samples (SPEC §7.7, and item 1 above). So the iteration is driven to a
precision roughly 6,700x finer than the noise in the numbers it is iterating
on — every sweep past about the seventh is refining digits that the item-draw
sample does not have.

Measured residual trace (Double or Nothing, 4 samples, 16 workers — the decay
rate is a property of the iteration, not the sample count):

| sweep | residual | | sweep | residual |
|---|---|---|---|---|
| 5 | 7.69e-03 | | 9 | 3.06e-05 |
| 6 | 1.95e-03 | | 10 | 7.59e-06 |
| 7 | 4.91e-04 | | 11 | 1.88e-06 |
| 8 | 1.23e-04 | | 12 | 4.66e-07 (converged) |

Where each tolerance would stop, and what it saves:

| tolerance | sweeps | saving | iteration error vs +/-0.0067 sample noise |
|---|---|---|---|
| 1e-6 (shipped) | 12 | — | 14,000x below |
| 1e-5 | 10 | 17% | 1,300x below |
| 1e-4 | 9 | 25% | 130x below |
| **1e-3** | **7** | **42%** | **13x below** |
| 1e-2 | 5 | 58% | comparable — too close |

**Recommendation: `1e-3`.** It cuts the build by 42% and still leaves the
iteration's contribution an order of magnitude under the sampling error that
dominates the result either way. `1e-2` is where the two become comparable
and is not recommended.

Caveat worth stating: a residual is a per-sweep *change*, not a distance from
the true fixed point. At the measured decay rate (~0.25/sweep) the remaining
distance is roughly `0.3 x` the last residual, so `1e-3` leaves ~3e-4 — which
does not change the conclusion, but the two quantities are not the same thing
and the table above uses the conservative one.

This is a `[solver]` dial, explicitly "ours, not the game's", so it is an
engineering decision rather than a `rules.toml` rule question — but it is
still a change to committed behaviour with a real accuracy cost, so it is
recorded here rather than applied.

</details>
