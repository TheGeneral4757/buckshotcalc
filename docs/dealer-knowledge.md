# Architecture note — the Dealer's private knowledge

*Resolves an ambiguity in SPEC.md §7.3. Binding on M3 (dealer policy) and M4
(solver). Written before either was implemented.*

## The problem

`GameState.dealer_knows` holds slot indices **the Dealer resolved itself**. Such
a slot may be unresolved for us: we watch the Dealer use a Magnifying Glass, but
we do not see the answer. The Dealer's subsequent behaviour then depends on a
value we do not have.

SPEC.md §7.3 says to "branch as a chance node weighted by our belief". That is
correct but underspecified in one important way: **when** to branch, and what
the successor state may be allowed to know.

The naive reading — resolve the slot for both sides the moment the Dealer
resolves it, then branch — is wrong, and wrong in the direction that flatters
the tool. It is textbook **strategy fusion**: the search would let *our* play
diverge between branches we cannot actually distinguish, and report the average
of two informed lines as if it were achievable. The number comes back too high,
and no test of the form "values are in [0,1]" catches it.

## The rule

**Branch lazily, at the moment the Dealer's policy consults the value — never at
the moment the Dealer acquires it.**

Concretely:

1. A Dealer information action (Glass, Burner Phone) adds an index to
   `dealer_knows` and **does not resolve the slot for us**. No chance node. The
   chamber's pool is untouched, because our belief is untouched.
2. At a Dealer decision node, if the policy needs slot 0's value and
   `0 in dealer_knows` while `slots[0] is None` for us, *then* open a chance
   node weighted by `p_live()` — and inside each branch the slot is resolved for
   us as well.

Step 2's last clause needs justification, and it has one.

## Why resolving inside the branch is sound here

Resolving slot 0 for ourselves inside the branch is only legitimate if the
Dealer's action tells us the value anyway. In v1 it always does. Every Dealer
behaviour whose *condition* depends on the hidden value produces an observation
that discloses it:

| Dealer behaviour (SPEC.md §3.3) | Depends on value? | What we observe |
|---|---|---|
| Shoot player / shoot self, knowing the shell | yes | the target — and the shot resolves the shell regardless |
| Beer (knows blank, or does not know live) | yes | whether beer was used; and the ejection reveals the shell |
| Inverter (only when it knows blank) | yes | whether the inverter was used |
| Handsaw (only when intending to shoot us) | yes | the saw, and then the shot |
| Magnifying Glass (shell unknown) | no | — |
| Burner Phone | no | — |
| Handcuffs / Cigarettes / Medicine | no | — |

The two behaviours that do *not* depend on the value are exactly the two that
acquire it.

The same argument covers a slot 0 flipped by an unseen Inverter
(`ChamberState.inverted0`, SPEC.md §5.2): the Dealer counts the true shells, so
its view depends on the flipped value, and its turn always ends by firing or
ejecting slot 0 in front of us. `dealer_step` therefore branches on it at the
top of the Dealer's turn, the same way. So the Dealer never carries a secret across a node where we must
act: by the time the secret changes its behaviour, the behaviour has told us the
secret.

**Therefore v1 is exact under lazy branching**, with no strategy-fusion error.
That is a property of the Dealer's deterministic policy, not a general one — it
does not survive into v2, where a human opponent may hold information and act on
it in ways that do not disclose it. v2's CFR engine exists precisely because
this argument stops working.

## Consequences for implementers

- `dealer_knows` is **never** used to resolve a slot in `ChamberState`. If a
  diff makes an information item write to both, that is the bug this note
  exists to prevent.
- Never consult our resolved slots when evaluating a Dealer condition. The
  Dealer's view is `dealer_knows`, full stop. Leaking our knowledge into its
  decisions makes it look far stronger than it is and drags every
  recommendation toward excessive caution (SPEC.md §7.3).
- Indices in `dealer_knows` renumber when a shell pops, and index 0 drops. That
  is the same relative-index mechanism the Burner Phone relies on
  (SPEC.md §3.2, `LIKELY`).
- The Dealer's coin flip on equal live/blank is a plain chance node with no
  hidden state, unrelated to any of the above.
- Add an M6 corpus entry for it: a position where the Dealer has glassed and we
  have not, whose value must equal the belief-weighted mixture of the two
  observable continuations — computed by hand, not by the solver.

---

## Addendum — public knowledge is not our knowledge

*Written during M2, before M3. Binding on `dealer.py`.*

The rule above says the Dealer's view is `dealer_knows`, full stop. That is
right, but it is not yet enough to implement, because `dealer_knows` is a set of
*indices* and the Dealer also knows things by counting. Splitting the two apart
matters more than it first looks.

Three tiers of knowledge exist in this game:

1. **Public.** The load composition is revealed to both players before it is
   loaded (§3.1), and every shell that is fired or ejected is observed by both.
   So the number of live and blank shells remaining — `chamber.total_live()` and
   `chamber.total_blank()` — is public, always, to both sides.
2. **Ours, privately.** Slots we resolved with our own Glass, Phone, or
   Inverter.
3. **The Dealer's, privately.** Slots in `dealer_knows`.

`ChamberState.slots` mixes tiers 1 and 2 and cannot distinguish them, because it
is *our* view and has no reason to. That is fine — until `dealer.py` reads it.

### The concrete failure

Load `L1 B2`. We use the Magnifying Glass on slot 0: LIVE. Our pool is now
`unknown_live=0, unknown_blank=2`, so `deduce()` fires and resolves slots 1 and 2
to BLANK. **We now know the entire chamber.**

The Dealer knows the multiset `{L, B, B}` and nothing else. Its `p_live` on
slot 0 is 1/3.

An implementation that asks `chamber.slots[0] is not None` to decide whether the
Dealer knows the chambered shell gets this exactly backwards — and note that our
*own* glass use is what made the Dealer look omniscient. That is the §7.3
warning ("never leak our knowledge into its decisions") arriving through a door
the spec does not name.

### The rule

`dealer.py` derives the Dealer's view rather than reading ours:

```
dealer_view(state) -> ChamberState:
    same length as state.chamber
    resolved at exactly the indices in state.dealer_knows
    pool = public totals, minus the shells at those resolved indices
    then deduce()   # the Dealer counts too — this generalizes
                    # "knows_shell_when_one_remains" (§3.3) for free
```

Everything the Dealer legitimately knows falls out: its own reveals, plus
count-closure over public totals. `knows_shell_when_one_remains` needs no
special case — with one shell left the public pool is a single shell and
`deduce()` resolves it.

### The residual, and it is a real one

`dealer_view` needs the *values* at `dealer_knows` indices. For slot 0 that is
supplied by the lazy branch above. For a **future** slot it is not: only the
Dealer's Burner Phone resolves one, and we never observe the answer.

Exactly modelling that requires carrying a belief over the Dealer's private
information across nodes where we act — a belief state, not a branch. Branching
at phone-use time would reintroduce the strategy fusion this note exists to
prevent, because the Dealer may sit on the answer for several turns while we
move. Expectimax over `GameState` cannot represent it.

So the exactness claim above is narrowed, honestly:

> v1 is exact for the base game, and for Double or Nothing except when **the
> Dealer** holds a Burner Phone.

Default (SPEC.md §15.9): a Dealer phone marks the index in `dealer_knows`, and
that index contributes to `dealer_view` only once it reaches slot 0, where lazy
branching supplies the value. Its effect on the Dealer's count-closure at other
slots is not modelled. This **understates** the Dealer, so it biases
`P(win)` *upward* — the flattering direction, which is why it is written down
here rather than left to be discovered. The effect is bounded: Double or Nothing
only, Dealer must hold a phone, and it changes nothing unless count-closure
would have fired early. **M5 must measure it** — a Monte Carlo cross-check
restricted to Dealer-holds-phone positions puts a number on the bias instead of
an argument.
