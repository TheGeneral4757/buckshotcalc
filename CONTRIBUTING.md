# Contributing to BuckshotCalc

## Maintenance status

This repository is published for public use and is **not actively maintained**.
Issues and pull requests may receive a delayed response or none at all. No
response time, review, or merge is promised.

If you need a change, the recommended route is to fork the repository. The MIT
license permits this, and a fork will move faster than waiting for a review.

## Reporting a bug

Please open an issue that includes:

1. The position: shells remaining, both players' health, maximum health, and each
   side's items.
2. The game mode and the version of the repository (commit hash).
3. The recommendation the calculator produced, and the one you expected, with the
   reasoning for your expectation.

A failing test case is the most useful report.

## Proposing a change

Before opening a pull request, please keep to the project's design rules:

- **Game rules are not invented.** Every rule must come from observation of the
  game or its source. A rule that is uncertain is recorded as an open question in
  `docs/decisions-pending.md`, not decided in code.
- **Constants live in `buckshotcalc/rules.toml`.** Damage values, health options,
  shell counts and the Dealer's item priority must not be hardcoded.
- **The engine stays pure standard library.** `buckshotcalc/engine/` and
  `buckshotcalc/sim/` may import only the standard library. Third-party
  dependencies belong in `buckshotcalc/tui/` only.
- **The Dealer's knowledge is kept separate from the player's.** See
  [`docs/dealer-knowledge.md`](docs/dealer-knowledge.md) before modifying
  `dealer.py` or the chance-node logic in `solver.py`.
- **Changes are accompanied by tests.**

A change to `rules.toml` or to engine logic invalidates the cached reload table.
It must be rebuilt (`buckshotcalc --build`) before results are compared.

### Development setup

```bash
pip install -e ".[dev]"
pytest -q -m "not slow"
ruff check buckshotcalc tests
```

Tests marked `slow` (Monte Carlo and reload fixed-point acceptance) take a long
time and are run deliberately rather than on every change.

## AI-assisted contributions

This project was itself produced with AI assistance (see
[AI-DISCLOSURE.md](AI-DISCLOSURE.md)), so AI-assisted contributions are
acceptable. Please state in the pull request description that AI was used, and
confirm that you have run the test suite.

## Licensing of contributions

By submitting a contribution, you agree that it is licensed under the
[MIT License](LICENSE) that covers this repository.
