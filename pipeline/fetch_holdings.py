"""CLI entry: emit one fund's latest N-PORT to a date-keyed JSON snapshot.

Layout:
    data/snapshots/{series_id}/{period_of_report}.json.gz

Each snapshot is the full holdings file for a single quarter end. The
runtime manifest (`data/funds.json`, built separately by
`pipeline.build_manifest`) carries the `latest_period` per series so
consumers can construct the right snapshot URL.

Skip-if-exists: before downloading XML we compare the latest filing's
accession_no against what's already on disk for that period_of_report.
Same accession → no-op. Different accession (an NPORT-P/A amendment) →
overwrite the dated file.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from pathlib import Path

from . import expenses, money_market, nport, transform


def _read_gz(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _write_gz(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", compresslevel=6) as f:
        json.dump(data, f)


MMF_FORMS = ["N-MFP3", "N-MFP2", "N-MFP"]


def _read_money_market(cik: str, accession_no: str) -> dict | None:
    """The N-MFP body's fields (see pipeline.money_market.parse_nmfp), or None if it can't be read."""
    import requests

    log = logging.getLogger("fetch_holdings")
    url = money_market.filing_url(cik, accession_no)
    try:
        resp = requests.get(url, headers={"User-Agent": nport.user_agent()}, timeout=60)
    except requests.RequestException as e:
        log.warning("could not fetch %s: %s", url, e)
        return None
    if resp.status_code != 200:
        log.warning("could not fetch %s: HTTP %s", url, resp.status_code)
        return None
    return money_market.parse_nmfp(resp.text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a snapshot for one fund.")
    parser.add_argument("--cik", required=True, help="SEC CIK (e.g. 0000036405)")
    parser.add_argument("--series-id", required=True, help="SEC series ID")
    parser.add_argument("--out", default="data/snapshots", help="Output root")
    parser.add_argument(
        "--ticker-index",
        default="data-securities/by_ticker.json",
        help="Optional ticker→CIK index from the securities repo. "
        "Missing file is fine — every ticker just hits EDGAR.",
    )
    parser.add_argument(
        "--is-cash",
        action="store_true",
        help="Treat this series as a cash equivalent (money market fund). "
        "Use N-MFP filings for provenance and emit a 100%% cash stub "
        "snapshot instead of parsing N-PORT-P holdings.",
    )
    parser.add_argument(
        "--fund-name",
        default=None,
        help="Display name for --is-cash stubs. Falls back to the series_id "
        "when omitted.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fetch_holdings")

    is_cash = args.is_cash
    if is_cash:
        log.info("locating latest N-MFP for cash series=%s", args.series_id)
        filing, meta = nport.find_latest(args.cik, args.series_id, form=MMF_FORMS)
    else:
        log.info("locating latest NPORT-P for series=%s", args.series_id)
        try:
            filing, meta = nport.find_latest(args.cik, args.series_id)
        except LookupError:
            # Money market funds file N-MFP instead of N-PORT: a series with N-MFP filings is one, so it no
            # longer needs a hand-set --is-cash flag.
            log.info("no NPORT-P; trying N-MFP (money market fund) for series=%s", args.series_id)
            filing, meta = nport.find_latest(args.cik, args.series_id, form=MMF_FORMS)
            is_cash = True
    log.info(
        "latest filing: %s (period %s)", meta["accession_no"], meta["period_of_report"]
    )

    snapshot_path = Path(args.out) / args.series_id / f"{meta['period_of_report']}.json.gz"

    if snapshot_path.exists():
        try:
            existing = _read_gz(snapshot_path)
            existing_acc = existing.get("fund", {}).get("source_filing")
        except (OSError, json.JSONDecodeError, KeyError):
            existing_acc = None
        if existing_acc == meta["accession_no"]:
            log.info("up-to-date: %s already has accession %s", snapshot_path, existing_acc)
            return 0
        log.info(
            "amendment: on-disk acc=%s, filing acc=%s — re-fetching",
            existing_acc,
            meta["accession_no"],
        )

    if is_cash:
        parsed = nport.parse_cash_stub(
            filing,
            series_id=args.series_id,
            series_name=args.fund_name,
            registrant_cik=args.cik,
        )
        # The stable-price facts consumers need to value the fund ($1.00 or floating).
        nmfp = _read_money_market(args.cik, meta["accession_no"])
        if nmfp is not None:
            parsed["fund"]["money_market"] = money_market.money_market_info(nmfp)
            parsed["fund"]["name"] = parsed["fund"]["name"] or nmfp.get("name")
    else:
        parsed = nport.parse(filing, ticker_index_path=args.ticker_index)
    parsed["filing"] = {
        "accession_no": meta["accession_no"],
        "source_url": meta["source_url"],
    }

    share_classes = parsed["fund"].get("share_classes") or []
    class_ids = [c["class_id"] for c in share_classes if c.get("class_id")]
    by_class_id, sources = expenses.fetch_expense_ratios(
        args.cik, args.series_id, class_ids
    )
    for sc in share_classes:
        ratio = by_class_id.get(sc.get("class_id"), {}).get("expense_ratio")
        sc["expense_ratio"] = ratio
    parsed["fund"]["fees_source_filings"] = sources
    log.info(
        "expense ratios: %d/%d classes covered from %d 497K(s)",
        sum(1 for sc in share_classes if sc.get("expense_ratio") is not None),
        len(share_classes),
        len(sources),
    )

    output = transform.to_json1(parsed)

    _write_gz(snapshot_path, output)
    log.info("wrote %s (%d holdings)", snapshot_path, len(output["holdings"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
