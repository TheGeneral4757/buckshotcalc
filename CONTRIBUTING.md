# Contributing

**This repo may not be maintained.** It is published so people can use the
calculator and TUI, or fork it. Issues and pull requests may sit unanswered for
a long time or forever. Please do not depend on a reply.

## Best option: fork it

The MIT license lets you take this and run with it. If you fix or extend
something, a fork is faster than waiting on a review.

## If you still open an issue or PR

- Bug reports: include the position (HP, shells, items), what the calc
  recommended, and what you expected. A failing test is the best report.
- Key rules (the private design spec is not published):
  - Do not invent game rules. If it isn't established, note it as an open question
    in `docs/decisions-pending.md` instead.
  - Do not hardcode anything that lives in `rules.toml`.
  - `engine/` and `sim/` are pure stdlib. Third-party deps live only in `tui/`.
  - Never leak the player's knowledge into the Dealer's decisions
    ([`docs/dealer-knowledge.md`](docs/dealer-knowledge.md)).
- Changes to rules or engine logic change the reload table. Rebuild it
  (about 35 min) before judging results.
- Include tests with your change.

```bash
pip install -e ".[dev]"
pytest -q -m "not slow"
ruff check buckshotcalc tests
```

## AI-assisted contributions

This project was written by AI, so AI-assisted PRs are fine. Say so in the PR
and make sure you've run the tests.

By contributing you agree your work is released under the repo's MIT license.
