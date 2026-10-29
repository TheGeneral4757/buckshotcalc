# AI Disclosure

## Authorship

All source code, tests, documentation, and rules analysis in this repository were
written by an AI system: Anthropic's Claude, operated through Claude Code. James
Thiessen specified the requirements, directed the work, tested it against the
game, and approved the result. No part of the code was written line by line by a
human.

## Implications for users

- The code has **not** undergone independent human code review. It should be
  reviewed before being relied upon.
- Evidence of correctness consists of the automated test suite and a Monte Carlo
  agreement check (`buckshotcalc/sim/montecarlo.py`), not a human audit.
- The game rules were inferred from the game's observed behaviour and from
  community-maintained references, and may be incorrect or become outdated after a
  game update. Each rule carries a confidence level in `buckshotcalc/rules.toml`.

## Copyright and license

The repository is published under the [MIT License](LICENSE), with copyright held
by James Thiessen (c) 2026.
