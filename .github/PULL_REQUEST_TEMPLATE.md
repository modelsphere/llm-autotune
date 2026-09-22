**What this changes, and why it was needed**

<!-- If it changes behaviour, say what would go wrong without it. -->

**Checks**

- [ ] `uv run ruff check . && uv run pytest -q` in `backend/`
- [ ] `npm run typecheck && npm run build` in `frontend/`
- [ ] Schema change? A migration on top of `001_initial`, tested on a fresh
      database *and* on an upgraded one
- [ ] `src/report/` change? `npm run build:report` output committed
- [ ] Docs updated, if behaviour or a contract moved
- [ ] No internal hostnames, registries or machine names
