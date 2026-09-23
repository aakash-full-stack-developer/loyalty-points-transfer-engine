# Contributing

Thanks for helping. The full guide (adding programs, partner adapters, rates and
migrations, code style, commit conventions and the pull request checklist) is the
[Developer guide in the README](README.md#developer-guide).

The short version:

```bash
cp .env.example .env
make reset          # stack up, schema migrated, seed data loaded
make format         # before committing
make lint && make typecheck && make test
```

Rules that protect correctness:

- Points are always integers, and every balance change goes through the ledger
  (`app/services/ledger.py`); never write a balance directly.
- Never hold a database transaction or lock while calling a partner.
- Every behaviour change comes with tests for its failure paths.
- New settings go in `app/config.py`, `.env.example` and the README; new error codes in
  `app/domain/errors.py` and the README. `make test` checks these stay in sync.
- Commits follow Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`).
