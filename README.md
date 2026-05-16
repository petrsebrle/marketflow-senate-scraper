# Senate EFD Scraper (GitHub Actions)

Scheduled scraper for U.S. Senate Periodic Transaction Reports (PTRs).
Runs every 15 minutes on a GitHub Actions runner (US Azure IP) and POSTs
new filings to the MarketFlow political-monitor ingest endpoint.

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
   to verify before the cron starts.

## How it works

```
GitHub Actions cron (US IP)
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
- Estimated runtime per cron: 1–2 min
- 4 runs/hour × 24 h × 30 days × 1.5 min ≈ 4 320 min/month (free)

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
for a backfill of the past month. Default cron uses 7 days to keep each run
fast.
