# Architecture note — the Dealer's item rack is observed

*Settles the "own items only" proposal, 2026-09-07. Binding on `tui/`.
Companion to [`dealer-knowledge.md`](dealer-knowledge.md): that note governs
**shell** knowledge, this one governs **item racks**. Conflating the two is
the mistake this document exists to prevent — see §1.*

## Context

The proposal is to redesign the assistant so the operator enters only their own
items during a round, not the Dealer's. Before designing anything, the question
posed was which kind of change this is: cosmetic (the solver already treats the
Dealer's rack as hidden and the TUI merely collects data the engine ignores) or
structural (the solver's exactness depends on knowing both racks).

The answer is structural, and it is worse than the framing suggests — the
Dealer's rack feeds the *player's* action space too, not just the Dealer's
policy. Beyond that, the investigation turned up a defect and a probable
mis-diagnosis of the underlying problem, both recorded below.

### Decision (operator, 2026-09-07)

Two questions were put to the operator and both are settled:

1. **The Dealer's rack is visible at the table.** The problem is entering it
   fast enough, not seeing it. So this is an input-model problem, not an
   information-model one.
2. **Direction: the §8 plan — fix the input, keep the rack.** Own-items-only is
   *not* being built. §§1–7 below are retained as the reasoning that settled
   it, and as the record for anyone who proposes it again.

**Scope of the work: `buckshotcalc/tui/` only.** No engine change, no
`rules.toml` change, no fingerprint change, no reload-cache invalidation, no
§12.1 re-baselining, and no reversal of SPEC §1.1's locked "full information
modeling for both sides."

---

## 1. Verdict: (b) STRUCTURAL — decisively

The Dealer's `ItemCounts` is a fully observed component of the state vector and
is load-bearing in four independent places. Citations, all current:

**(1) It is the domain of the entire Dealer policy walk.**
`dealer.py:400` — `_evaluate_from` opens with `avail_actions =
legal_actions(state, rules)`, and `legal_actions` (`actions.py:198-199`) reads
`player = state.turn; held = state.items_of(player)` then skips every item with
`held[item] <= 0`. At a Dealer node `state.turn is Player.DEALER`, so the set of
items the Dealer holds *is* the set of §3.4 priority entries that can fire.
Change the rack from `{beer}` to `{}` and the Dealer stops ejecting; the whole
subtree changes. This is the mechanism, not an incidental read.

**(2) Two of the Dealer's IF-AND-ONLY-IF conditions read the rack directly.**
`dealer.py:199` — `dealer_cigarettes = state.items_of(Player.DEALER)[Item.CIGARETTES]`,
the `medicine_requires_no_cigarettes_held` conjunct (SPEC §15.17).
`dealer.py:272,277` — `_adrenaline_want` reads `state.items_of(Player.DEALER)`
and skips any item the Dealer already holds.

**(3) The *player's own* action space depends on the Dealer's rack.**
`actions.py:163-180` — `_adrenaline_stealable_items(state, player)` enumerates
`state.items_of(opponent)`. At a **player** node the opponent is the Dealer, so
`USE(adrenaline, steal=X)` exists as a legal action for the operator iff the
Dealer holds X. `legal_actions` at our own nodes is not well-defined without the
Dealer's inventory. This is the part the cosmetic reading misses entirely.

**(4) Both racks are part of state identity.**
`memo_key.py:264-266` — `for side_items in state.items: … _pack(code,
side_items[item], w.w_item, "item")`. Two positions differing only in the
Dealer's rack are distinct memo entries with generally distinct values.

Supporting: `panels.py:218` dims Adrenaline based on the Dealer's rack;
`solver.py:619` and `montecarlo.py:275,752` construct concrete Dealer racks.

**There is no conflict with `docs/dealer-knowledge.md`.** That document governs
*shell* knowledge — its own addendum names three tiers, and item racks are not
in any of them; the private tiers are "slots we resolved" and "slots in
`dealer_knows`". `dealer_view` (`dealer.py:49-124`) rebuilds the Dealer's
*chamber* belief from scratch precisely so our shell knowledge cannot leak; it
never touches items, and correctly so. Reading the Dealer's rack is not the
leak that doc exists to prevent. The proposal's option (a) rests on conflating
the two, and that conflation is worth writing into that doc explicitly so the
next reader does not make it (see §7).

**The repo independently establishes that the rack is observable.** SPEC §1.1's
locked decision (line 98): *"Full information modeling for both sides."*
`docs/decisions-pending.md:317` sets item 9's exit condition as a line where the
fixed order "visibly diverges from **a rack the operator can see**." SPEC §3.4's
whole rack-order finding required watching the Dealer's rack. And SPEC §3.3's
exploit at line 275 — *"If the Dealer holds Adrenaline, spend your own dangerous
items first"* — is only actionable by an operator who can see it.

So the proposed change does not remove data the tool shouldn't have. It removes
data the operator has and the solver needs.

---

## 2. What is actually being asked for (the re-diagnosis)

There is a real bug underneath this request, and it is not the one in the
proposal.

Today, an un-entered Dealer rack is not "unknown". It is
`ItemCounts.empty()` — an **assertion that the Dealer holds nothing**. The
solver believes it, and the banner prints a confident percentage that is
systematically too high. Nothing on screen distinguishes "I checked, it has
nothing" from "I haven't looked yet."

The magnitude is known and it is not small. SPEC §15.14 measured a single Dealer
Beer moving `P(win)` from 0.5000 to 0.4500 at a tied count, and 0.4167 to 0.5000
at a live majority — 5 to 8 points, against §12.1's ±0.01 acceptance budget.
One unentered item can be eight times the tool's entire error budget, in an
unsignposted direction.

Two aggravating factors found in the code:

- **`app.py:638` silently deletes both racks on `ctrl+l`.** `_reload_gun` calls
  `self.session.new_load(live, blank)` with no `items=`; `new_load`
  (`session.py:537-558`) forwards `items=None` and `GameState.create`
  (`state.py:342-343`) fills `(empty, empty)`. SPEC §3.1 step 5 and §15.13 both
  state a reload deals 2–5 items **on top of what is still held**. The
  `items=` parameter exists for exactly this; `app.py` never passes it. So every
  mid-round reload both loses carried items (which §15.13 already measures as
  systematically undervalued, +0.03 to +0.09) *and* forces total re-entry of
  both racks. Note `tests/test_session.py:302-311` pins the empty-rack default
  at the `Session` layer, so this is a wiring gap in `app.py`, not a `Session`
  bug.
- **Item entry costs one keystroke per item with no repeat**, twice per load.
  DoN deals 2–5 to each side per round and again on every reload — 4 to 10
  keypresses of pure data entry before the operator can act.

That is the friction the proposal is reacting to. It is fixable without
touching the engine, and the fixes are strictly better than removing the
information (§6).

---

## 3. Information model

What the operator actually knows at a decision point:

| Fact | Known? | Where the model stands |
|---|---|---|
| Load composition (live/blank totals) | yes, announced | public tier, correct |
| Own rack | yes, on the table | correct |
| **Dealer's rack** | **yes, on the table** | **correct today** |
| Shells we resolved (glass/phone/beer/deduction) | yes | correct |
| What the Dealer's glass saw | no | `dealer_knows` index only — correct |
| Which slot the Dealer's phone hit, and its value | no | fixed index-1 assumption, §15.9 / pending item 10 |
| Shell order | no | correct |

"Own items only" would model a game in which the opponent's rack is hidden.
That is not this game, and it is not v2's game either — v2's private information
is shells and intentions, not racks.

The one honest argument for it: the operator may not *have* entered what they
could have seen. But that is an input-completeness problem with a UI answer, not
an information-model problem with an engine answer. Modelling the rack as
permanently unknown throws away information on every position to compensate for
the positions where it happened to be missing.

---

## 4. Solver/engine impact if built anyway

Three implementable shapes, increasing cost.

### (A) Marginalize at the root — belief lives in `Session`, engine untouched

`Session` holds `[(w_i, ItemCounts_i)]` candidate Dealer racks; the TUI reports
`Σ wᵢ·V(stateᵢ)` and, per action, `argmax_a Σ wᵢ·V(apply(stateᵢ, a))`.
`solver.py`, `dealer.py`, `actions.py`, `memo_key.py` all unchanged.

**It is not exact, and it is wrong in the flattering direction.** It is textbook
strategy fusion — the same failure `docs/dealer-knowledge.md` was written to
forbid, applied to a different hidden variable. Its own words apply verbatim:
*"the search would let our play diverge between branches we cannot actually
distinguish, and report the average of two informed lines as if it were
achievable."* If we would cuff in the has-beer world and saw in the no-beer
world, averaging the two values claims a line we cannot play.

Cost: N searches per keystroke. The candidate space over 9 item types with 2–5
dealt is ~2,000 multisets before carry-over, so it needs a top-K truncation
that is inherently arbitrary. The 16 ms redraw budget (SPEC §7.5) survives at
small chambers and will not at 8 shells; unmeasured.

### (B) Marginalize inside the tree, at each Dealer node

Doing this *correctly* requires Bayesian conditioning: a Dealer that could have
beered and didn't tells you it holds no beer. That is a belief state, not a
chance node, and `GameState` cannot carry it — the identical argument already
recorded in SPEC §15.9 and `decisions-pending.md` items 6 and 10. It is
`decisions-pending` item 6 option (b), which that entry calls *"the correct
model and the most invasive — it changes what 'the state' means everywhere
above the engine"*, and which remains unbuilt for a strictly **smaller** problem
(one chamber slot, two branches). Without conditioning, (B) is (A) with worse
performance and the same bias.

Not a v1 change. Not a v2 change as v2 is currently scoped.

### (C) Sample rather than enumerate — mean plus a visible band

Reuse `solver._draw_items` (`solver.py:161`) and `_draw_samples`'s
fingerprint-seeded determinism (`solver.py:543-577`); solve n sampled racks,
report mean and min/max. Same fusion bias as (A), but the **spread** is arguably
the most useful thing to put on screen: *"62% if it's holding nothing scary,
41% if it has the saw — tell me what you can see."* This is the version worth
prototyping if the operator wants the direction at all.

### `V_reload` — the important finding

Its fixed point is **unchanged in meaning under (A) and (C), and it is already
the marginalization being asked for.** `_draw_samples` draws *two* racks per
sample (`solver.py:568-576`) and `_build_reload_state` takes both
(`solver.py:619`), so `V_reload(hp_p, hp_d, max_hp)` already means "expected
`P(win)` from a fresh load with **both racks unknown and drawn from the
prior**." That is exactly own-items-only semantics — shipped, cached, working.

It is tractable there for one specific reason: at a reload boundary the belief
is **fresh and unconditioned**. Every in-load node after the Dealer's first turn
has a belief conditioned on observed non-use, and conditioning is precisely what
expectimax over `GameState` cannot carry. That asymmetry is the whole answer to
"why not everywhere."

Under (B) the table's *definition* is unchanged but its inner solve is not —
full recompute, new fingerprint, hours (~4.2 h CPython / ~1.7 h PyPy, SPEC §7.7).

### Item-value tables

`decisions-pending` item 7 fixes "the Dealer holds nothing" as part of the M8
sheet's declared position family. Under (A)/(C) that header line becomes
wrong-in-a-new-way and every number regenerates (cheap — sub-second). Under a
UI-only change, untouched.

### Is this "v2 pulled forward"? No.

CFR (SPEC §7.6) solves for mixed strategies against a *strategic* opponent with
private shell information and unobservable intent. The Dealer is deterministic
and its rack is public; there is no strategic hiding to solve. What
own-items-only needs is a **belief-state / POMDP layer** — `decisions-pending`
item 6 option (b) — which is different machinery, is not on the roadmap at any
version, and which CFR does not hand you for free. Treating them as the same
thing would be a roadmap error worth avoiding explicitly.

---

## 5. TUI impact

Fields that disappear: `app.py:317` and `:325` (`seat = Player.DEALER if
char.isupper()`), the `SHIFT+key` row in `panels._KEY_ROWS:641`, and the DEALER
column in `_items_panel` (`panels.py:489,492,499`).

Three new ambiguities the current UI answers for free:

1. **The DEALER column cannot just go blank** — blank reads as "holds nothing",
   the exact lie described in §2. It has to become a belief display ("likely:
   cuffs 61% · beer 44% …" or a band "2–5 items, composition unknown"). Real
   screen-space cost in the middle column.
2. **Adrenaline breaks.** `_begin_item_use` (`app.py:470-484`) builds its steal
   prompt by enumerating `_held_items(opponent)`. With no Dealer rack there is
   nothing to enumerate, and the solver cannot rank the steal. This is the
   sharpest UI consequence and it is downstream of §1 item (3).
3. **`item_is_pointless`** for Adrenaline (`panels.py:218`) would have to stop
   dimming.

**Display when the answer is a distribution.** `_headline_number`
(`panels.py:244-252`) prints one numeral; that stops being honest. It needs mean
plus band. More seriously, the alternatives panel's founding premise — SPEC §8:
*"the gap matters more than the rank … 71% vs 40% means do not improvise"* —
**breaks when the bands overlap**. A 3-point gap is meaningless against a
20-point band, so the panel must be able to say "these are indistinguishable
given what you've told me" rather than rank them. That is a genuine redesign of
the panel's contract, not a formatting change.

Good news: the withholding mechanism already exists. `Session.degraded` /
`DEGRADED_INVERTER` (`session.py:46,204`) and `panels._notices:333` are the
right shape for "Dealer rack unconfirmed" — see §8 step 2.

---

## 6. Regression risk

| Surface | Under UI-only fixes (§7) | Under (A)/(C) | Under (B) |
|---|---|---|---|
| `tests/test_dealer.py` (38 rack refs; helpers at :55,:59) | none | none | essentially all of it |
| `tests/test_actions.py` (30, mostly adrenaline steals) | none | none | rewritten |
| `tests/test_tui.py:487-488` (`pilot.press(beer.upper())` pins SHIFT→Dealer) | delete/adjust | delete | delete |
| `tests/test_session.py` (27) | mostly none | none | rewritten |
| `sim/corpus.py` (§12.2) | none | header note | rebuilt |
| `memo_key` widths + SPEC §7.7's "4,867 keys, zero collisions" record | none | none | invalidated |

Sharper items:

- **§12.1 becomes ill-posed.** `random_position` (`montecarlo.py:444-447`) draws
  both racks and hands the full state to `solver.value`. If the solver's
  contract becomes "the Dealer's rack is a belief", a marginalized prediction is
  compared against rollouts that used one concrete sampled rack — a guaranteed
  mismatch far exceeding ±0.01 **by construction**. The harness needs a matching
  change (either validate the search with the rack revealed, or draw a fresh
  rack per trial from the same prior to validate the marginalization). Those
  measure different things and both are wanted. This is real re-baselining, on
  top of `decisions-pending` item 3's never-executed run.
- **`decisions-pending` item 4 becomes unmeasurable.**
  `measure_dealer_phone_bias` is *defined* over Dealer-holds-phone positions
  (`montecarlo.py:737-740` forces `with_delta(Item.BURNER_PHONE, 1)`). That
  distinction cannot be drawn if the rack is unobserved. §15.9's bias would be
  permanently un-quantifiable.
- **`decisions-pending` item 9 closes as unresolvable.** Its exit condition is
  literally a rack the operator can see.
- **Cache invalidation hazard.** Any new `[solver]` key (e.g.
  `dealer_rack_samples`) changes `rules.fingerprint`, invalidating every cached
  reload table and re-rolling `_draw_samples` — `decisions-pending` item 1's
  measured lesson, and item 11's explicit warning to settle config *before* a
  long build. Same class of hazard; must be sequenced the same way.

---

## 7. Docs/roadmap contradictions — exact lines

- **SPEC.md:98** — `| Knowledge | Full information modeling for both sides,
  including what the Dealer knows |`, inside §1.1, which SPEC.md:92 marks
  *"These are settled. Do not relitigate them during implementation."* The
  proposal directly reverses a locked decision. This is the load-bearing line;
  it cannot be changed by a commit, it needs an operator ruling and arguably a
  spec version bump.
- **SPEC.md:84** — *"It is an **exact game-tree solver** … That distinction is
  the product."* Would need qualifying for a second reason beyond §15.9/§15.13.
- **SPEC.md:275** — the Adrenaline-denial exploit. **SPEC.md:903-907 makes the
  §3.3 exploits mandatory §12.2 corpus entries**, so one mandatory entry becomes
  undiscoverable.
- **SPEC.md:895** — §12.1's `|predicted − empirical| < 0.01` needs restating
  against a rack prior, per §6.
- **SPEC.md §3.4 / §15.2** and **`decisions-pending.md:277-318`** — the entire
  rack-order discussion presumes an observable rack.
- **README.md:113** (v1 roadmap row, "Exact solver…") — same qualification as
  SPEC.md:84. **README.md:114** (v2 row) would need a note that CFR is *not*
  what delivers hidden-rack modelling, or the roadmap gains a belief-state row.
- **README.md:100-107** ("Design in one paragraph") survives — it commits to
  lethality/turn economy/item leverage/information value and to maximizing
  `P(win)`, none of which this touches. Its "derives all four from the game
  tree rather than encoding them as rules" is unaffected.
- **`docs/dealer-knowledge.md`** — add a paragraph stating its rule governs
  *shell* knowledge and that item racks are tier-1 public. Right now that is
  implicit, and the proposal's option (a) is direct evidence a careful reader can
  mistake this change for an application of the doc's own rule.
- **CLAUDE.md** non-negotiable #5 — same clarification (shells, not racks).

---

## 8. The plan

Keep Dealer item entry. Fix the three things that actually hurt. Build in this
order — each step stands alone and is independently shippable.

### Step 1 — Fix the `ctrl+l` item wipe (a defect; do it first)

`app.py:636-642` `_reload_gun` calls `new_load(live, blank)` with no `items=`,
so `GameState.create` fills `(empty, empty)` and both racks vanish. SPEC §3.1
step 5 and §15.13 both state items carry across a reload and are topped up.

- `app.py:638` → pass the racks through:
  `self.session.new_load(live, blank, items=self.session.state.items)`.
  `Session.new_load` already accepts `items=` (`session.py:542`) — the parameter
  exists for exactly this and `app.py` simply never used it.
- The log line should say the items carried, so the operator can see it did.
- `tests/test_session.py:302-311` stays valid unchanged: it pins the `Session`
  *default*, which is not what moves. Add a `tests/test_tui.py` case that drives
  `ctrl+l` through the pilot and asserts both racks survive.

### Step 2 — Distinguish "unconfirmed" from "empty" (highest value)

An un-entered Dealer rack currently *asserts* the Dealer holds nothing, and
§15.14's own measurements put one unentered Beer at 5–8 points of `P(win)` —
five to eight times §12.1's entire error budget, in an unsignposted direction.

- `Session`: add `dealer_rack_confirmed: bool`, `False` after every
  `new_round`/`new_load`, `True` on the first Dealer-side item entry. Carry it
  on the undo stack exactly the way `degraded` already is
  (`session.py:204-206,226,256`) so `ctrl+z` restores it — that pattern is there
  to copy.
- It needs an explicit "the Dealer has nothing" gesture, or confirmation is
  unreachable for a genuinely empty rack. Simplest: committing an empty Dealer
  draft in step 3 counts as confirmation.
- `panels._notices` (:330): when unconfirmed, emit a warning alongside
  `reload_table_missing` — *"you have not said what the dealer is holding — the
  number above assumes it holds nothing, which runs optimistic."*
- **Do not suppress the number** (unlike `degraded`). An optimistic number with
  a stated caveat beats no number at a live table; suppression is reserved for
  §15.16(b), where the belief is genuinely unrepresentable rather than merely
  unentered.
- Mark the DEALER column in `_items_panel` (`panels.py:489-500`) unconfirmed
  too, so the caveat sits where the data is, not only on the banner.

### Step 3 — Make item entry cheap (the actual complaint)

DoN deals 2–5 to each side per round and again on every reload; today that is
4–10 discrete keystrokes with no repeat, twice per load.

- **Item-draft mode.** A modal state that pins one seat while letters
  accumulate, `enter` commits the whole draft, `esc` cancels. Model it directly
  on the existing `space`/`-` prefixes (`app.py:164,335-346`) — always visible
  on the question line, always `esc`-able, never state-dependent — since that is
  the interaction pattern already proven in this app. Two draft gestures replace
  up to ten keypresses, and committing an empty Dealer draft is what sets
  `dealer_rack_confirmed` in step 2.
- It needs a key. §9's table is otherwise full and items own every bare letter,
  so this has to be a ctrl-chord or a second `tab`-like modal — worth choosing
  deliberately, since `decisions-pending` item 5 already flags "§9's table is
  full" as a live constraint.
- Print the gesture in `panels._KEY_ROWS` (:636-650). Nothing lives behind a
  help key on this dashboard, and that is an acceptance criterion
  (`panels.py:14-17`), not a preference.
- **`ctrl+l` should focus the NEW ROUND stepper row** rather than silently
  reusing the previous round's counts (`app.py:638` reads `self._setup`). A
  mid-round reload announces a *different* count, and correcting it today costs
  four gestures (`tab`, step, `esc`, `ctrl+l`).

### Verification

```bash
.venv/bin/pytest -q -m "not slow"          # the regression net
.venv/bin/ruff check buckshotcalc tests
.venv/bin/python -m buckshotcalc.tui.app   # drive steps 1-3 by hand
```

The acceptance signal that this stayed on the TUI side of the line:
`test_solver.py`, `test_dealer.py`, `test_actions.py` and `test_montecarlo.py`
must pass **completely unchanged**, and `test_layering.py` must still pin
`session.py`/`analysis.py` as stdlib-only. No engine value moves, so no cached
reload table is invalidated and no §12.1 baseline shifts.

By hand, at the app: start a round, enter both racks via the draft mode,
`ctrl+l`, and confirm both racks survive and the shell counts are asked for
rather than assumed; then start a fresh round without touching the Dealer
column and confirm the unconfirmed warning appears on the banner *and* in the
items panel, and that `ctrl+z` restores the unconfirmed state after a Dealer
item is entered.

---

## 9. For `docs/decisions-pending.md`

**Two entries to record as SETTLED (2026-09-07)**, with the reasoning, so the
proposal is not re-opened from scratch:

1. **Whether the Dealer's rack is treated as observed — settled: yes, keep it.**
   The game shows it (SPEC §1.1's locked decision, SPEC §3.4's rack-order
   observation, and item 9's own "a rack the operator can see"), and the
   operator confirms the rack is visible; the failure is input speed, not
   sight. Own-items-only was investigated and declined: it is structural (§1),
   it reverses a locked SPEC decision (§7), it makes `decisions-pending` items 4
   and 9 permanently unmeasurable (§6), and every tractable version of it
   reintroduces the strategy fusion `docs/dealer-knowledge.md` exists to forbid
   (§4). Record the §4 (A)/(B)/(C) analysis so a future proposal starts from it
   rather than from zero.
2. **What the tool does with an unconfirmed rack — settled: warn, keep the
   number** (§8 step 2). Rejected: status quo (silently optimistic by 5–8 points
   per §15.14), and suppression (reserved for §15.16(b), where the belief is
   genuinely unrepresentable rather than merely unentered).

**One genuinely open, worth adding:**

3. **Which key the item-draft mode takes.** §9's table is full and items own
   every bare letter (SPEC §9, `panels._KEY_ROWS`). `decisions-pending` item 5
   already names this same constraint as unresolved for a different feature —
   the two should be settled together rather than each grabbing a chord.

**One documentation fix, not a decision** (§7): `docs/dealer-knowledge.md` and
CLAUDE.md non-negotiable #5 should state explicitly that the rule governs
*shell* knowledge and that item racks are tier-1 public. The premise of this
brief is direct evidence a careful reader can mistake the two.

---

## 10. TUI review, beyond this change

**Right calls, consistently applied.** One key one meaning; everything on screen
with no toggles; modal prefixes that are always visible and always `esc`-able;
undo that narrates what it undid; and — the best instinct in the codebase — a
refusal to guess (`degraded`, `record_dealer_phone` dropping the operator prompt
in favour of one documented assumption the search shares).

**Mismatches with how Double or Nothing actually plays:**

- **Item entry burden**, §2 / §8 step 3 — the real complaint behind this brief.
- **`ctrl+l` wipes both racks**, §2 / §8 step 2 — a defect, not a nit.
- **`ctrl+l` reuses stale shell counts** — four gestures for the most common
  mid-round event.
- **No unconfirmed-rack state**, §8 step 1.
- **Off-by-one round counter**: `_round_number` starts at 1 and
  `_load_round` (`app.py:629`) increments *before* the first round is
  displayed, so the first `enter` shows ROUND 2. Cosmetic.
- **Dominating everything above:** out of the box the banner shows `?` and
  withholds every recommendation until someone runs a ~4 h precompute
  (`app.py:217`, README:34-37, `decisions-pending` items 1/3/5). No input-speed
  work changes that. Any conversation about whether the TUI is fast should
  start there.

**Correct for DoN specifically:** `mode.mode = "double_or_nothing"`, the
endless-gated majority rule and count-closure, the `size > 2` phone guard, and
the DoN item list. The tool is aimed at the right game and the mode gating is
explicit rather than assumed universal.
