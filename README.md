# Senate EFD Scraper (GitHub Actions)

Scraper for U.S. Senate Periodic Transaction Reports (PTRs). Runs on a
GitHub Actions runner (US Azure IP) and POSTs new filings to the MarketFlow
political-monitor ingest endpoint.

**Cadence is driven from our own server, not by the cron in this workflow.**
A systemd timer on VPS (`senate-dispatch.timer`) calls the
`workflow_dispatch` API every 10 minutes. The reason: since ~February 2026
GitHub drops the vast majority of scheduled events — measured on this very
workflow over 82 days, a `*/10` cron produced 6.5 % of its runs, with a
median gap of 100 minutes and never once a gap under 44. GitHub has
acknowledged it as an upstream regression with no fix date. Dispatched runs
start in the same second as the API call. The `schedule:` trigger is kept
only as a fallback for when VPS is unavailable.

Senate EFD blocks requests from many hosting providers via Akamai WAF, so
the scraper cannot run from our server. GitHub-hosted runners use rotating
US-based IPs that the Senate site accepts.

## Deployment (one-time)

1. Create a **public** GitHub repo (data is already public; public repos get
   unlimited Actions minutes).
2. Push the contents of this directory to the repo's root.
3. Add two repository secrets (Settings → Secrets and variables → Actions):
   - `INGEST_URL` — `https://marketflow.cz/api/political/ingest`
   - `INGEST_TOKEN` — copy from `/root/political/political.env` (`INGEST_TOKEN=`)
4. Enable Actions in the repo (Settings → Actions → Allow all actions).
5. Manually trigger once via the Actions tab → "senate-scrape" → "Run workflow"
   to verify before the dispatcher takes over.

## How it works

```
GitHub Actions runner (US IP)
  ├─ chromium via Playwright → efdsearch.senate.gov/search/
  ├─ accept agreement, filter PTRs, list filings
  ├─ for each NEW filing (DocID not in our DB):
  │     parse the electronic HTML table → row list
  │     OR mark paper PDFs with rows=[] (server can fetch later)
  └─ POST {"source":"senate","filings":[...]} to INGEST_URL
```

The scraper is **stateless**: every run asks the server for the list of
DocIDs it already has (`GET /api/political/known_doc_ids?source=senate`)
and skips them.

## Cost

- Public repo on free tier: **unlimited minutes**
- Estimated runtime per run: 1–2 min
- 6 runs/hour × 24 h × 30 days × 1.5 min ≈ 6 480 min/month (free)

## Local debug

```bash
pip install -r requirements.txt
playwright install chromium
INGEST_URL=https://marketflow.cz/api/political/ingest \
INGEST_TOKEN=... \
HEADLESS=false \
LOOKBACK_DAYS=1 \
python scraper.py
```

Note: from a non-US IP, Senate EFD will reject the request (Akamai). Use a
GitHub runner instead, or run from a US location for local testing.

## Manual backfill

Use the `workflow_dispatch` trigger and set `lookback_days` to e.g. `30`
for a backfill of the past month. Dispatched runs use 7 days to keep each run
fast.
