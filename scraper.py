"""Senate EFD scraper for GitHub Actions.

Runs on a Microsoft Azure US-IP runner every 15 minutes. Scrapes PTRs (Periodic
Transaction Reports) filed in the last 30 days, parses electronic HTML tables,
and POSTs new filings to a private MarketFlow ingest endpoint.

Paper-filed PTRs (PDFs) are listed but not parsed here; their metadata is sent
with `rows=[]` so the server can choose to backfill them later.

Required environment variables:
  INGEST_URL    e.g. "https://marketflow.cz/api/political/ingest"
  INGEST_TOKEN  Bearer token (must match server's INGEST_TOKEN)

Optional:
  LOOKBACK_DAYS         default 30
  HEADLESS              default "true"; set "false" to debug locally
  KNOWN_IDS_URL         default derived from INGEST_URL
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

BASE_URL = "https://efdsearch.senate.gov/search/"
TIMEOUT = 20_000  # ms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("senate_scraper")


# ---------------------------------------------------------------------------
# Server ingest helpers
# ---------------------------------------------------------------------------

def _ingest_url() -> str:
    url = os.environ.get("INGEST_URL")
    if not url:
        log.error("INGEST_URL not set")
        sys.exit(2)
    return url


def _token() -> str:
    tok = os.environ.get("INGEST_TOKEN")
    if not tok:
        log.error("INGEST_TOKEN not set")
        sys.exit(2)
    return tok


def fetch_known_doc_ids() -> set[str]:
    base = _ingest_url()
    derived = os.environ.get("KNOWN_IDS_URL") or base.rsplit("/", 1)[0] + "/known_doc_ids"
    r = requests.get(
        derived,
        params={"source": "senate"},
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    r.raise_for_status()
    return set(r.json().get("doc_ids") or [])


def post_filings(filings: list[dict]) -> dict:
    r = requests.post(
        _ingest_url(),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        json={"source": "senate", "filings": filings},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------

def setup(pw: Playwright):
    browser = pw.chromium.launch(headless=os.environ.get("HEADLESS", "true").lower() != "false")
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        viewport={"width": 1400, "height": 900},
        locale="en-US",
    )
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT)
    return browser, ctx, page


def accept_agreement(page: Page) -> None:
    log.info("Navigating to %s", BASE_URL)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    cb = page.locator("#agree_statement")
    cb.wait_for(state="visible", timeout=TIMEOUT)
    if not cb.is_checked():
        cb.check()
    log.info("Accepted agreement")
    page.wait_for_load_state("networkidle", timeout=TIMEOUT)


def fill_search(page: Page, lookback_days: int) -> None:
    """Tick PTR filter and set from-date to today − lookback_days."""
    log.info("Filling search form (lookback %d days)", lookback_days)

    # Select Periodic Transactions ("11"). The checkbox label may not be visible
    # but we click via JS-friendly check().
    ptr_cb = page.locator("input[id='reportTypes'][value='11']")
    ptr_cb.wait_for(state="attached", timeout=TIMEOUT)
    if not ptr_cb.is_checked():
        ptr_cb.check()

    from_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
    page.locator("#fromDate").fill(from_date)
    log.info("From date: %s", from_date)

    page.locator("button[type='submit']").click()
    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
    except PWTimeout:
        pass

    # Try to set DataTables length to 100 so we paginate less.
    try:
        page.evaluate(
            "() => { const sel = document.querySelector('select[name=\"filedReports_length\"]'); "
            "if (sel) { sel.value = '100'; sel.dispatchEvent(new Event('change')); } }"
        )
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass


def collect_report_urls(page: Page, max_pages: int = 10) -> list[dict]:
    """Iterate result table pages and return rows with {filer, type, date, url}."""
    out: list[dict] = []
    pages_seen = 0
    while pages_seen < max_pages:
        pages_seen += 1
        rows = page.locator("table#filedReports tbody tr").all()
        log.info("Page %d: %d rows", pages_seen, len(rows))
        if not rows or "No data available" in (rows[0].text_content() or ""):
            break

        for row in rows:
            cells = row.locator("td").all()
            if len(cells) < 6:
                continue
            link = cells[3].locator("a")
            href = link.get_attribute("href") if link.count() else None
            if not href:
                continue
            full_url = urljoin(BASE_URL, href)
            out.append({
                "first": (cells[0].text_content() or "").strip(),
                "last": (cells[1].text_content() or "").strip(),
                "office": (cells[2].text_content() or "").strip(),
                "title": (cells[3].text_content() or "").strip(),
                "report_type": (cells[4].text_content() or "").strip(),
                "filed_date": (cells[5].text_content() or "").strip(),
                "url": full_url,
            })

        # Pagination
        nxt = page.locator("a.paginate_button.next:not(.disabled)")
        if nxt.count() == 0:
            break
        try:
            nxt.first.click()
            page.wait_for_load_state("networkidle", timeout=10_000)
            time.sleep(1)
        except Exception:
            break
    log.info("Collected %d total filings across %d pages", len(out), pages_seen)
    return out


def doc_id_from_url(url: str) -> str:
    """Senate filing URLs end with a slug like /paper/<uuid>/ or /ptr/<uuid>/.

    Use the last non-empty path segment as DocID.
    """
    path = urlparse(url).path.rstrip("/")
    segs = [s for s in path.split("/") if s]
    return segs[-1] if segs else url


def is_paper_filing(meta: dict) -> bool:
    """Heuristic: filings under /search/view/paper/ are PDF scans; HTML otherwise."""
    return "/paper/" in meta["url"]


def parse_electronic_ptr(page: Page, meta: dict) -> dict:
    """Open a filing URL and extract rows from `table.table`."""
    log.info("Parsing %s — %s", meta["filed_date"], meta["url"])
    page.goto(meta["url"], wait_until="domcontentloaded")
    try:
        page.wait_for_selector("table.table tbody tr", timeout=15_000)
    except PWTimeout:
        log.warning("No transaction table on %s", meta["url"])
        return _make_filing(meta, [])

    rows: list[dict] = []
    for tr in page.locator("table.table tbody tr").all():
        cells = [c.text_content().strip() if c.text_content() else "" for c in tr.locator("td").all()]
        if len(cells) < 9:
            continue
        # Columns: # | Tx Date | Owner | Ticker | Asset Name | Asset Type |
        #          Tx Type | Amount | Comment
        tx_date = _norm_date(cells[1])
        owner = cells[2].lower() or None
        ticker = cells[3] or None
        asset_name = cells[4] or None
        asset_type = _norm_asset_type(cells[5])
        tx_type = _norm_tx_type(cells[6])
        amt_min, amt_max = _parse_amount(cells[7])
        comment = cells[8] or None
        if not tx_date or not tx_type:
            continue
        rows.append({
            "tx_date": tx_date,
            "owner": owner,
            "ticker": ticker,
            "asset_name": asset_name,
            "asset_type": asset_type,
            "tx_type": tx_type,
            "amount_min": amt_min,
            "amount_max": amt_max,
            "comment": comment,
        })
    log.info("  → %d rows", len(rows))
    return _make_filing(meta, rows)


def _make_filing(meta: dict, rows: list[dict]) -> dict:
    return {
        "doc_id": doc_id_from_url(meta["url"]),
        "filing_url": meta["url"],
        "filer_name": f"{meta.get('first', '')} {meta.get('last', '')}".strip(),
        "last_name": meta.get("last") or None,
        "state": meta.get("office") or None,  # column is "Office (StateDst)"; refined by server
        "notify_date": _norm_date(meta.get("filed_date", "")),
        "report_type": meta.get("report_type"),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Normalization helpers (kept lightweight; server enforces canonical schema)
# ---------------------------------------------------------------------------

def _norm_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_tx_type(s: str) -> str | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    if "purchase" in s:
        return "buy"
    if "sale" in s or "sell" in s:
        return "sell"
    if "exchange" in s:
        return "exchange"
    return None


def _norm_asset_type(s: str) -> str | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    if "stock" in s or "equity" in s:
        return "stock"
    if "etf" in s:
        return "etf"
    if "option" in s:
        return "option"
    if "bond" in s or "note" in s or "treasur" in s:
        return "bond"
    if "fund" in s:
        return "mutualfund"
    if "crypto" in s:
        return "crypto"
    return "other"


def _parse_amount(s: str) -> tuple[int | None, int | None]:
    """Bracket strings like '$1,001 - $15,000' or '$50,000,001 +'."""
    s = (s or "").strip()
    if not s:
        return None, None
    digits = lambda part: int("".join(ch for ch in part if ch.isdigit()) or 0) or None  # noqa: E731
    if "+" in s:
        return digits(s), 0
    if "-" in s:
        low, _, high = s.partition("-")
        return digits(low), digits(high)
    n = digits(s)
    return n, n


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    lookback = int(os.environ.get("LOOKBACK_DAYS", "30"))
    max_per_run = int(os.environ.get("MAX_PER_RUN", "200"))

    known = fetch_known_doc_ids()
    log.info("Server reports %d known Senate DocIDs", len(known))

    with sync_playwright() as pw:
        browser, ctx, page = setup(pw)
        try:
            accept_agreement(page)
            fill_search(page, lookback_days=lookback)
            metas = collect_report_urls(page)
            log.info("Filings to consider: %d", len(metas))

            new_filings: list[dict] = []
            for m in metas:
                did = doc_id_from_url(m["url"])
                if did in known:
                    continue
                if is_paper_filing(m):
                    # Mark with empty rows so server can later backfill via PDF parsing.
                    new_filings.append(_make_filing(m, []))
                    continue
                try:
                    parsed = parse_electronic_ptr(page, m)
                    new_filings.append(parsed)
                except Exception as e:
                    log.exception("Failed to parse %s: %s", m["url"], e)
                if len(new_filings) >= max_per_run:
                    log.info("Hit MAX_PER_RUN=%d, stopping early", max_per_run)
                    break
        finally:
            ctx.close()
            browser.close()

    if not new_filings:
        log.info("No new filings to post")
        return 0

    log.info("POSTing %d filings to ingest", len(new_filings))
    resp = post_filings(new_filings)
    log.info("Server response: %s", json.dumps(resp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
