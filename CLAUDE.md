# price-intelligence

## Agent skills

### Issue tracker

Issues live as GitHub issues in `Whyslab/price-intelligence`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. There is no `CONTEXT.md` or `docs/adr/` yet — the domain lives in
`docs/ru/README.md` (how it behaves and why; `README.md` is the English overview), `docs/product.md` (what it is for and what is
next) and `docs/function-map.md` (where each thing is). See `docs/agents/domain.md`
for the convention if an ADR is ever added.

## Working on it

- **The timers run this checkout.** `systemd/` units have
  `WorkingDirectory=%h/Projects/price-intelligence`, so an unfinished edit here is
  in production at the next `*:15`. Do larger work in a separate `git worktree` and
  merge when it is green.
- CI is exactly `ruff check .` and `pytest -q` (Python 3.11 and 3.13).
- The live database is `data/pi.db` (SQLite, WAL, ~3 GB). Read it with
  `sqlite3 -readonly`; copy it with `python -m pi backup`, never `cp`.
- Every probe against Shopify spends the same per-IP quota the collector lives on.
