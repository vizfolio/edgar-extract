"""Intermediate parsed dict → snapshot wire shape.

The wire contract lives in `pipeline/models.py` as pydantic models; this
module just maps the loose intermediate dict from `nport.fetch_latest`
into those models, then dumps to a JSON-safe dict for `fetch_holdings`
to gzip.

CUSIPs do not enter this module — the intermediate dict from
`nport.fetch_latest` doesn't carry them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import mappings, models

log = logging.getLogger(__name__)

SCHEMA_VERSION = models.FUND_SNAPSHOT_SCHEMA_VERSION


def _holding(h: dict) -> models.Holding:
    asset_cat = mappings.asset_cat(h.get("asset_cat_code"))
    debt = h.get("debt") if asset_cat == "debt" else None
    return models.Holding(
        ticker=h.get("ticker"),
        isin=h.get("isin"),
        lei=h.get("lei"),
        issuer_cik=h.get("issuer_cik"),
        name=h.get("name"),
        weight=(h.get("pct_val") or 0.0) / 100.0,
        fair_value_usd=h.get("val_usd"),
        balance=h.get("balance"),
        units=h.get("units"),
        asset_cat=asset_cat,
        issuer_cat=mappings.issuer_cat(h.get("issuer_cat_code")),
        country=h.get("inv_country"),
        currency=h.get("cur_cd"),
        payoff_profile=h.get("payoff_profile"),
        is_restricted=bool(h.get("is_restricted")),
        is_fair_valued=bool(h.get("is_fair_valued")),
        fair_value_level=h.get("fair_value_level"),
        debt=models.DebtBlock(**debt) if debt else None,
    )


def to_json1(parsed: dict) -> dict:
    fund_in = parsed["fund"]
    filing = parsed["filing"]

    holdings_out: list[models.Holding] = []
    dropped_weight = 0.0
    dropped_count = 0

    for h in parsed["holdings"]:
        if not h.get("ticker") and not h.get("isin"):
            dropped_count += 1
            dropped_weight += (h.get("pct_val") or 0.0) / 100.0
            log.warning("skipping holding without ticker or isin: %s", h.get("name"))
            continue
        holdings_out.append(_holding(h))

    if dropped_count:
        log.info(
            "dropped %d holdings (%.4f weight) without a usable identifier",
            dropped_count,
            dropped_weight,
        )

    fund = models.Fund(
        name=fund_in.get("name"),
        series_id=fund_in.get("series_id"),
        series_lei=fund_in.get("series_lei"),
        registrant_cik=fund_in.get("registrant_cik"),
        registrant_name=fund_in.get("registrant_name"),
        registrant_lei=fund_in.get("registrant_lei"),
        as_of=fund_in.get("as_of"),
        total_assets_usd=fund_in.get("total_assets_usd"),
        total_liabs_usd=fund_in.get("total_liabs_usd"),
        net_assets_usd=fund_in.get("net_assets_usd"),
        cash_not_in_portfolio_usd=fund_in.get("cash_not_in_portfolio_usd"),
        source_filing=filing["accession_no"],
        source_url=filing["source_url"],
        is_final_filing=bool(fund_in.get("is_final_filing", False)),
        dropped_holdings_count=dropped_count,
        dropped_weight=dropped_weight,
        interest_rate_risk=[
            models.InterestRateRisk(
                currency=r["currency"],
                dv01=models.PeriodBucket.model_validate(r["dv01"]) if r.get("dv01") else None,
                dv100=models.PeriodBucket.model_validate(r["dv100"]) if r.get("dv100") else None,
            )
            for r in fund_in.get("interest_rate_risk", [])
        ],
        credit_spread_risk=_credit_spread_risk(fund_in.get("credit_spread_risk")),
        monthly_returns=[
            models.MonthlyReturn(**r) for r in fund_in.get("monthly_returns", [])
        ],
        share_classes=[
            models.ShareClass(**sc) for sc in fund_in.get("share_classes", [])
        ],
        fees_source_filings=[
            models.FeesSourceFiling(**s) for s in fund_in.get("fees_source_filings", [])
        ],
    )

    snapshot = models.FundSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fund=fund,
        holdings=holdings_out,
    )
    return snapshot.model_dump(mode="json", by_alias=True)


def _credit_spread_risk(raw: dict | None) -> models.CreditSpreadRisk | None:
    if not raw:
        return None
    ig = raw.get("investment_grade")
    nig = raw.get("non_investment_grade")
    return models.CreditSpreadRisk(
        investment_grade=models.PeriodBucket.model_validate(ig) if ig else None,
        non_investment_grade=models.PeriodBucket.model_validate(nig) if nig else None,
    )
