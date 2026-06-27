# Terminology

A glossary of the SEC forms, identifiers, and external sources this
pipeline touches. **Keep this file current when adding support for a
new form or identifier** — it's the canonical orientation doc for
anyone reading the code who doesn't already know SEC terminology.

---

## SEC forms

### N-PORT-P — Monthly Portfolio Investments report

**What.** Form required of registered investment companies (mutual
funds, ETFs, closed-end funds) since 2018. Reports the fund's full
holdings as of the period close, plus risk metrics, monthly returns,
and aggregate financials. Filed quarterly but covers three months;
only the third (quarter-end) month is publicly disclosed — the prior
two stay confidential to the SEC.

**Why we use it.** Primary data source for the pipeline. Every fund
snapshot under `data/snapshots/` derives from one N-PORT-P. We
extract: holdings list with fair values + weights, asset/issuer
categories, share-class IDs, monthly returns, and DV01/DV100
interest-rate risk.

**Where.** `pipeline/nport.py`. Located via
`edgar.find(series_id).get_filings(form="NPORT-P")`, parsed via
`filing.obj()` to an edgartools `Report`.

**Cadence.** Quarterly, ~60-day lag after period close. CI runs weekly.

### N-PORT-P/A — N-PORT-P amendment

**What.** Amendment to a previously filed N-PORT-P. Same data shape,
new accession number.

**Why we use it.** Handled transparently by the skip-if-exists logic
in `fetch_holdings.py`: same `accession_no` for the same period →
no-op; different accession → overwrite the snapshot.

### 497K — Summary prospectus

**What.** Customer-facing 4-page fund summary filed under Rule 498 of
the Securities Act. Carries the standardized "Fees and Expenses"
table. Multi-class fund families typically file separate 497Ks per
class group on the same day rather than one comprehensive prospectus
(Vanguard publishes ~5 per fund per cycle, one per share-class
subset). No XBRL — purely HTML tables.

**Why we use it.** Only reliable per-class expense-ratio source.
N-PORT carries no expense data. `expenses.fetch_expense_ratios` walks
a series' 497Ks in reverse-chronological order and merges
`net_expenses` (post-waiver, falling back to `total_annual_expenses`)
into the snapshot's `share_classes[]` until every class is covered.

**Where.** `pipeline/expenses.py`. Edgartools' `Prospectus497K`
handles HTML parsing for the fee tables.

### N-1A — Registration statement / full prospectus

**Not currently used.** Full prospectus and amendments. Carries the
same fees-and-expenses table as 497K in a much larger document. We
use 497K instead because it's smaller and edgartools has dedicated
parsing for it.

### N-CSR / N-CSRS — Shareholder reports

**Not currently used.** Annual (N-CSR) and semi-annual (N-CSRS)
shareholder reports. Carry XBRL-tagged expense ratios via the
Open-End Fund (OEF) taxonomy — potentially a more authoritative
expense-ratio source than 497K HTML parsing. Listed in
`docs/DEFERRED_GAPS.md` as a possible cross-check.

---

## Identifiers

### CIK — Central Index Key

SEC's primary identifier for any filing entity (companies, funds,
individuals). 10-digit zero-padded number (e.g., `0000036405` for
Vanguard Index Funds). Normalize via `nport._normalize_cik` — SEC
uses both padded and unpadded forms in different contexts.

### Series ID

Identifier for a single fund within a registrant. Format: `S` + 9
digits (e.g., `S000002848` for Vanguard Total Stock Market). One
registrant CIK can have many series.

### Class / Contract ID

Identifier for a single share class within a series. Format: `C` + 9
digits (e.g., `C000007808` for VTI). One series typically has several
classes (VTI, VTSAX, VITSX all map to series `S000002848`).

### Accession Number

Unique filing identifier. Format: `XXXXXXXXXX-YY-NNNNNN` (e.g.,
`0000036405-26-000323`). Used as the cache key for "is this snapshot
up to date".

### CUSIP

9-character US/Canada security identifier. Edgartools resolves CUSIPs
to tickers via its bundled parquet; OpenFIGI is the fallback. **CUSIPs
are not persisted past `pipeline/nport.py`** — license terms restrict
redistribution.

### LEI — Legal Entity Identifier

20-character ISO 17442 entity identifier. Carried through from N-PORT
as `lei` on holdings and `series_lei` / `registrant_lei` on the fund.

### ISIN — International Securities Identification Number

12-character ISO 6166 security identifier. Used as the fallback
holding identifier when no ticker is available (common for non-US
positions).

---

## Classification systems

### SIC — Standard Industrial Classification

4-digit US industry code, ~1000 categories. Assigned by SEC to every
registrant. Surfaced on per-issuer registry records as `sic` +
`sic_description`.

### GICS — Global Industry Classification Standard

11 top-level sectors used by MSCI / S&P. Not directly available from
SEC; we derive a GICS-style `sector` field from SIC via
`config/sic_to_sector.json`, with per-CIK overrides in
`config/sector_overrides.json` for known SIC↔GICS mismatches
(Alphabet, Meta, Disney → Communication Services; UnitedHealth →
Health Care; etc.).

---

## Filing format & metadata

### SGML header

Every EDGAR filing carries a structured plain-text SGML header before
the actual filing payload (XML / HTML / XBRL). Carries the registrant
CIK, accession info, and — for fund filings — the
`<SERIES-AND-CLASSES-CONTRACTS-DATA>` block listing every series and
class. We parse this block directly in `nport.parse_share_classes` to
populate `share_classes[]` without fetching extra documents.

### `period_of_report`

"As of" date for the filing. For N-PORT-P it's the quarter-end. Used
as the snapshot filename (`{period_of_report}.json.gz`).

---

## External data sources

### `company_tickers_mf.json` (SEC)

SEC-published index of mutual-fund tickers, keyed by `series_id`.
Source: `https://www.sec.gov/files/company_tickers_mf.json`. Used in
`build_manifest.py` to enumerate every share-class ticker for each
tracked series (VTI + VTSAX + VITSX all map to `S000002848`).

### OpenFIGI

Bloomberg's free CUSIP → ticker / ISIN mapping API. Fallback for
holdings edgartools can't resolve via its bundled parquet. See
`pipeline/openfigi.py`. Auth-optional: 25 req/min unauthenticated,
250 req/min with `OPENFIGI_API_KEY` set.
