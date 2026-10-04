"""Registry of every money market fund, built from EDGAR Form N-MFP filings.

Every money market fund files Form N-MFP monthly, and the filing states what consumers need to value it:

* ``seekStablePricePerShare`` — whether the fund seeks a stable price per share (government and retail funds
  hold $1.00; institutional prime and tax-exempt funds have floated since the 2016 reforms),
* ``stablePricePerShare`` — that price (1.0000),
* ``moneyMarketFundCategory`` — Government, Prime, Single State, Other Tax Exempt…,
* ``fundRetailMoneyMarketFlag``.

N-MFP doesn't list tickers, so they come from SEC's mutual-fund ticker map (``company_tickers_mf.json``:
series → class → ticker). Together that gives every money market fund's tickers with no hand-maintained list.

Discovery reads the EDGAR quarterly form index for N-MFP3 filings and parses only each fund's latest filing in a
recent window. A previous registry can be passed in so filings already parsed (same accession) aren't re-fetched.

    nix develop -c python -m pipeline.money_market --out data/money_market_funds.json

Output: ``money_market_funds.json`` (schema ``schemas/money_market_funds.json``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests

from . import models, nport

log = logging.getLogger(__name__)

FORM = "N-MFP3"
INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"
FILING_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"

# SEC allows 10 requests/second; stay comfortably under it.
_REQUEST_INTERVAL_SECONDS = 0.15


# ---------------------------------------------------------------------------
# Pure parsing (no network) — unit tested
# ---------------------------------------------------------------------------

def parse_form_index(index_text: str, form: str = FORM) -> list[dict]:
    """Rows of an EDGAR ``form.idx`` for one form type: ``{cik, date_filed, accession_no}``.

    The index is fixed-width but company names contain spaces, so each row is read from both ends: the form type
    is the first token; CIK, date filed and file name are the last three.
    """
    rows = []
    for line in index_text.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or tokens[0] != form:
            continue
        cik, date_filed, file_name = tokens[-3], tokens[-2], tokens[-1]
        if not file_name.endswith(".txt") or not cik.isdigit():
            continue
        accession_no = file_name.rsplit("/", 1)[-1].removesuffix(".txt")
        rows.append({"cik": cik, "date_filed": date_filed, "accession_no": accession_no})
    return rows


def recent_filings(rows: Iterable[dict], window_days: int) -> list[dict]:
    """Filings within ``window_days`` of the newest one — each fund's latest monthly report."""
    rows = list(rows)
    if not rows:
        return []
    newest = max(date.fromisoformat(r["date_filed"]) for r in rows)
    cutoff = newest - timedelta(days=window_days)
    return [r for r in rows if date.fromisoformat(r["date_filed"]) >= cutoff]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _yes(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().upper() in ("Y", "YES", "TRUE")


def parse_nmfp(xml_text: str) -> dict | None:
    """The fields of an N-MFP3 ``primary_doc.xml`` this registry needs, or None if it isn't one.

    Namespace-agnostic (tags are matched on their local name), so N-MFP schema namespace bumps don't break it.
    The first ``cik`` in document order is the filer's (registrant's) CIK.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    first: dict[str, str] = {}
    class_ids: list[str] = []
    for el in root.iter():
        name = _local(el.tag)
        text = (el.text or "").strip()
        if not text:
            continue
        if name == "classesId":
            class_ids.append(text)
        first.setdefault(name, text)

    series_id = first.get("seriesId")
    if not series_id:
        return None

    stable = first.get("stablePricePerShare")
    try:
        stable_price = float(stable) if stable else None
    except ValueError:
        stable_price = None

    return {
        "series_id": series_id,
        "name": first.get("nameOfSeries"),
        "registrant_cik": nport._normalize_cik(first.get("cik")),
        "registrant_name": first.get("registrantFullName"),
        "as_of": first.get("reportDate"),
        "category": first.get("moneyMarketFundCategory"),
        "seeks_stable_price": bool(_yes(first.get("seekStablePricePerShare"))),
        "stable_price_per_share": stable_price,
        "is_retail": _yes(first.get("fundRetailMoneyMarketFlag")),
        "class_ids": list(dict.fromkeys(class_ids)),
    }


def money_market_info(parsed: dict) -> dict:
    """The per-snapshot subset (``models.MoneyMarketInfo``) of a parsed N-MFP."""
    return {
        "category": parsed.get("category"),
        "seeks_stable_price": parsed.get("seeks_stable_price", False),
        "stable_price_per_share": parsed.get("stable_price_per_share"),
        "is_retail": parsed.get("is_retail"),
    }


def tickers_by_series(company_tickers_mf: dict) -> dict[str, dict[str, str]]:
    """``company_tickers_mf.json`` → ``{series_id: {class_id: ticker}}``."""
    fields = company_tickers_mf.get("fields") or []
    try:
        i_series, i_class, i_symbol = fields.index("seriesId"), fields.index("classId"), fields.index("symbol")
    except ValueError:
        raise ValueError(f"unexpected company_tickers_mf.json fields: {fields}") from None

    out: dict[str, dict[str, str]] = {}
    for row in company_tickers_mf.get("data") or []:
        series_id, class_id, symbol = row[i_series], row[i_class], row[i_symbol]
        if series_id and class_id and symbol:
            out.setdefault(series_id, {})[class_id] = str(symbol).strip().upper()
    return out


def filing_url(cik: str, accession_no: str) -> str:
    return FILING_DOC_URL.format(cik=int(cik), accession_nodash=accession_no.replace("-", ""))


def build_registry(
    filings: Iterable[dict],
    fetch_xml: Callable[[str], str | None],
    tickers: dict[str, dict[str, str]],
    previous: dict | None = None,
) -> dict:
    """Parse each filing (reusing ``previous`` entries with the same accession) into a registry document.

    When a series appears in several filings, the latest report wins. Classes come from the filing; tickers are
    attached from SEC's ticker map, and classes it knows that the filing omitted are added too.
    """
    reuse = {f["source_filing"]: f for f in (previous or {}).get("funds", [])}
    by_series: dict[str, dict] = {}

    for f in filings:
        accession = f["accession_no"]
        url = filing_url(f["cik"], accession)
        if accession in reuse:
            entry = dict(reuse[accession])
        else:
            xml_text = fetch_xml(url)
            parsed = parse_nmfp(xml_text) if xml_text else None
            if parsed is None:
                log.warning("skipping %s: not a parsable N-MFP3", accession)
                continue
            entry = {k: v for k, v in parsed.items() if k != "class_ids"}
            entry.update(source_filing=accession, source_url=url)
            entry["classes"] = [{"class_id": c, "ticker": None} for c in parsed["class_ids"]]

        current = by_series.get(entry["series_id"])
        if current is None or (entry.get("as_of") or "") > (current.get("as_of") or ""):
            by_series[entry["series_id"]] = entry

    funds = []
    for series_id, entry in sorted(by_series.items()):
        known = tickers.get(series_id, {})
        class_ids = [c["class_id"] for c in entry.get("classes", [])]
        class_ids += [c for c in known if c not in class_ids]
        entry["classes"] = [{"class_id": c, "ticker": known.get(c)} for c in class_ids]
        funds.append(models.MoneyMarketFund(**entry))

    registry = models.MoneyMarketRegistry(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        funds=funds,
    )
    return registry.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class _Edgar:
    """Polite EDGAR client: identifying User-Agent, throttled, retried once on transient errors."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = nport.user_agent()
        self._last = 0.0

    def get(self, url: str) -> requests.Response | None:
        for attempt in range(2):
            wait = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            resp = self._session.get(url, timeout=60)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503) and attempt == 0:
                time.sleep(2)
                continue
            log.warning("GET %s → %s", url, resp.status_code)
            return None
        return None


def _quarters(today: date, count: int) -> list[tuple[int, int]]:
    """The current quarter and the ``count - 1`` before it, newest first."""
    year, quarter = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(count):
        out.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the money market fund registry from N-MFP filings.")
    p.add_argument("--out", default="data/money_market_funds.json")
    p.add_argument("--previous", default=None, help="An earlier registry; filings it already parsed are reused.")
    p.add_argument("--quarters", type=int, default=2, help="Quarterly form indexes to read (current + previous).")
    p.add_argument("--window-days", type=int, default=45, help="Keep filings this close to the newest one.")
    p.add_argument("--limit", type=int, default=None, help="Parse at most this many filings (smoke tests).")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    edgar = _Edgar()

    rows: list[dict] = []
    for year, quarter in _quarters(date.today(), args.quarters):
        resp = edgar.get(INDEX_URL.format(year=year, quarter=quarter))
        if resp is None:
            log.info("no form index for %d Q%d (yet)", year, quarter)
            continue
        found = parse_form_index(resp.text)
        log.info("%d Q%d: %d %s filings", year, quarter, len(found), FORM)
        rows.extend(found)

    filings = recent_filings(rows, args.window_days)
    if args.limit is not None:
        filings = filings[: args.limit]
    log.info("parsing %d recent %s filings", len(filings), FORM)

    tickers_resp = edgar.get(TICKERS_URL)
    if tickers_resp is None:
        log.error("could not download %s", TICKERS_URL)
        return 1
    tickers = tickers_by_series(tickers_resp.json())

    previous = None
    if args.previous and Path(args.previous).exists():
        previous = json.loads(Path(args.previous).read_text())

    def fetch_xml(url: str) -> str | None:
        resp = edgar.get(url)
        return resp.text if resp is not None else None

    registry = build_registry(filings, fetch_xml, tickers, previous)
    if not registry["funds"]:
        log.error("no money market funds parsed — refusing to write an empty registry")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    stable = sum(1 for f in registry["funds"] if f["seeks_stable_price"])
    log.info("wrote %s: %d funds (%d stable-price)", out, len(registry["funds"]), stable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
