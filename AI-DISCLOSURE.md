# AI disclosure

All source code, tests, documentation and rules analysis in this repository were
written by AI: Anthropic's Claude, used through Claude Code. James Thiessen
prompted, directed, tested against the game, and approved the work. No code was
hand-written line by line.

What that means for you:

- It has not had independent human code review. Verify before relying on it.
- Correctness evidence is the automated test suite and the Monte Carlo agreement
  check in `sim/montecarlo.py`, not human audit.
- Game rules were inferred from the game's observed behaviour and may be wrong
  (see the confidence levels in `buckshotcalc/rules.toml`).

Credit and copyright for the repo as published: James Thiessen, MIT license.
