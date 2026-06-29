"""Update config/funds.json with new tickers.

Usage:
    python add_funds.py SCHD VBR VTIAX ...

Resolves each ticker via SEC's company_tickers_mf.json (series_id + CIK),
then fetches the series name with edgartools. Skips tickers whose series_id
is already in the config, and warns on any ticker not found in the MF index.

Requires EDGAR_USER_AGENT to be set (same requirement as the pipeline).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import edgar
import requests

MF_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
CONFIG_PATH = Path("config/funds.json")


def _user_agent() -> str:
    import os
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        sys.exit(
            "error: EDGAR_USER_AGENT is not set.\n"
            "Export it before running, e.g.:\n"
            "  export EDGAR_USER_AGENT='portfolio-tracker you@example.com'"
        )
    return ua


def _fetch_mf_index(ua: str) -> dict[str, dict]:
    """Return {ticker_upper: {series_id, cik}} from SEC's MF ticker index."""
    resp = requests.get(MF_TICKERS_URL, headers={"User-Agent": ua}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    fields = data["fields"]
    index: dict[str, dict] = {}
    for row in data["data"]:
        rec = dict(zip(fields, row))
        sym = rec.get("symbol", "")
        sid = rec.get("seriesId")
        cik = rec.get("cik")
        if sym and sid and cik:
            index[sym.upper()] = {
                "series_id": sid,
                "cik": f"{int(cik):010d}",
            }
    return index


def _series_name(series_id: str) -> str:
    """Resolve a series_id to its fund name via edgartools."""
    series = edgar.find(series_id)
    return series.name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Add funds to config/funds.json")
    p.add_argument("tickers", nargs="+", metavar="TICKER")
    p.add_argument("--config", default=str(CONFIG_PATH))
    args = p.parse_args(argv)

    ua = _user_agent()
    edgar.set_identity(ua)

    config_path = Path(args.config)
    existing: list[dict] = json.loads(config_path.read_text())
    existing_series = {f["series_id"] for f in existing}

    print(f"Fetching SEC mutual-fund ticker index...")
    mf_index = _fetch_mf_index(ua)

    added, skipped, errors = [], [], []

    for raw in args.tickers:
        ticker = raw.upper()
        entry = mf_index.get(ticker)
        if entry is None:
            errors.append(ticker)
            print(f"  {ticker}: not found in SEC MF index — skipping")
            continue

        sid = entry["series_id"]
        if sid in existing_series:
            # Find which ticker already covers it
            covering = next(
                (f["ticker"] for f in existing if f["series_id"] == sid), sid
            )
            skipped.append(ticker)
            print(f"  {ticker}: series {sid} already in config (as {covering}) — skipping")
            continue

        print(f"  {ticker}: resolving series name for {sid}...")
        try:
            name = _series_name(sid)
        except Exception as e:
            errors.append(ticker)
            print(f"  {ticker}: failed to resolve name — {e}")
            continue

        new_entry = {
            "ticker": ticker,
            "cik": entry["cik"],
            "series_id": sid,
            "name": name,
        }
        existing.append(new_entry)
        existing_series.add(sid)
        added.append(ticker)
        print(f"  {ticker}: added ({name})")

    if added:
        config_path.write_text(json.dumps(existing, indent=2) + "\n")
        print(f"\nWrote {config_path} — {len(existing)} funds total")

    print(
        f"\nDone: {len(added)} added, {len(skipped)} skipped, {len(errors)} errors"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
