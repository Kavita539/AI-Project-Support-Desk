# AI Project Support Desk

See `docs/prd.md` and `docs/design.md`.

## Setup

Copy `.env.example` values into `apps/web/.env.local` and `apps/api/.env`.

### API (`apps/api`, Python 3.12)
```
py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
ruff check . ; mypy ; pytest
```

### Web (`apps/web`)
```
npm install
npm run dev
npm run lint ; npm run typecheck ; npm test ; npm run e2e
```
(First e2e run: `npx playwright install chromium`.)
