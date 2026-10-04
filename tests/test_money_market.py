"""Tests for pipeline.money_market — pure parsing, no network.

    nix develop -c python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from pipeline import money_market, transform

# Trimmed from a real N-MFP3 primary_doc.xml (structure and tag names as filed).
NMFP3 = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nmfp3" xmlns:ns1="http://www.sec.gov/edgar/common">
  <headerData>
    <submissionType>N-MFP3</submissionType>
    <filerInfo><filer><issuerCredentials><cik>0000106830</cik></issuerCredentials></filer></filerInfo>
  </headerData>
  <formData>
    <generalInfo>
      <reportDate>2026-08-31</reportDate>
      <registrantFullName>Example Money Market Reserves</registrantFullName>
      <cik>0000106830</cik>
      <nameOfSeries>Example Federal Money Market Fund</nameOfSeries>
      <seriesId>S000004462</seriesId>
    </generalInfo>
    <seriesLevelInfo>
      <adviser><cik>0000735286</cik></adviser>
      <moneyMarketFundCategory>Government</moneyMarketFundCategory>
      <fundRetailMoneyMarketFlag>N</fundRetailMoneyMarketFlag>
      <seekStablePricePerShare>Y</seekStablePricePerShare>
      <stablePricePerShare>1.0000</stablePricePerShare>
    </seriesLevelInfo>
    <classLevelInfo><classesId>C000012238</classesId></classLevelInfo>
  </formData>
</edgarSubmission>
"""

FLOATING = NMFP3.replace(
    "<seekStablePricePerShare>Y</seekStablePricePerShare>", "<seekStablePricePerShare>N</seekStablePricePerShare>"
).replace("<stablePricePerShare>1.0000</stablePricePerShare>", "").replace(
    "<moneyMarketFundCategory>Government", "<moneyMarketFundCategory>Prime"
).replace("S000004462", "S000009999").replace("C000012238", "C000099999")

FORM_INDEX = """Description:           Master Index of EDGAR Dissemination Feed by Form Type
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------
N-MFP3      EXAMPLE MONEY MARKET RESERVES                                 106830      2026-09-05  edgar/data/106830/0001410368-26-091076.txt
N-MFP3      ANOTHER FUND TRUST INC                                        857156      2026-07-06  edgar/data/857156/0000857156-26-000123.txt
N-MFP3      OLD FILING TRUST                                              999999      2026-05-01  edgar/data/999999/0000999999-26-000001.txt
NPORT-P     SOME EQUITY FUND                                              36405       2026-08-29  edgar/data/36405/0000036405-26-000480.txt
"""

TICKERS = {
    "fields": ["cik", "seriesId", "classId", "symbol"],
    "data": [
        [106830, "S000004462", "C000012238", "vmfxx"],
        [106830, "S000004462", "C000077777", "VMFAX"],  # a class the filing didn't list
        [36405, "S000002848", "C000007806", "VTSAX"],
    ],
}


class ParseFormIndexTests(unittest.TestCase):
    def test_reads_only_the_requested_form_with_names_containing_spaces(self):
        rows = money_market.parse_form_index(FORM_INDEX)

        self.assertEqual([r["accession_no"] for r in rows], [
            "0001410368-26-091076", "0000857156-26-000123", "0000999999-26-000001",
        ])
        self.assertEqual(rows[0], {"cik": "106830", "date_filed": "2026-09-05", "accession_no": "0001410368-26-091076"})

    def test_recent_filings_keeps_each_funds_latest_monthly_report(self):
        rows = money_market.parse_form_index(FORM_INDEX)

        recent = money_market.recent_filings(rows, window_days=70)

        self.assertEqual({r["cik"] for r in recent}, {"106830", "857156"})  # the May filing is too old


class ParseNmfpTests(unittest.TestCase):
    def test_reads_the_stable_price_facts_and_identity(self):
        parsed = money_market.parse_nmfp(NMFP3)

        self.assertEqual(parsed["series_id"], "S000004462")
        self.assertEqual(parsed["name"], "Example Federal Money Market Fund")
        self.assertEqual(parsed["registrant_cik"], "0000106830")  # the filer's, not the adviser's
        self.assertEqual(parsed["as_of"], "2026-08-31")
        self.assertEqual(parsed["category"], "Government")
        self.assertTrue(parsed["seeks_stable_price"])
        self.assertEqual(parsed["stable_price_per_share"], 1.0)
        self.assertFalse(parsed["is_retail"])
        self.assertEqual(parsed["class_ids"], ["C000012238"])

    def test_a_floating_nav_fund_does_not_seek_a_stable_price(self):
        parsed = money_market.parse_nmfp(FLOATING)

        self.assertFalse(parsed["seeks_stable_price"])
        self.assertIsNone(parsed["stable_price_per_share"])
        self.assertEqual(parsed["category"], "Prime")

    def test_something_that_is_not_an_nmfp_is_rejected(self):
        self.assertIsNone(money_market.parse_nmfp("<html>not xml"))
        self.assertIsNone(money_market.parse_nmfp("<doc><other>1</other></doc>"))


class BuildRegistryTests(unittest.TestCase):
    def test_joins_tickers_from_the_sec_map_including_classes_the_filing_omitted(self):
        filings = [{"cik": "106830", "date_filed": "2026-09-05", "accession_no": "0001410368-26-091076"}]

        registry = money_market.build_registry(filings, lambda url: NMFP3, money_market.tickers_by_series(TICKERS))

        fund = registry["funds"][0]
        self.assertEqual(registry["schema_version"], "1")
        self.assertEqual(fund["series_id"], "S000004462")
        self.assertTrue(fund["seeks_stable_price"])
        self.assertEqual(
            fund["classes"],
            [{"class_id": "C000012238", "ticker": "VMFXX"}, {"class_id": "C000077777", "ticker": "VMFAX"}],
        )
        self.assertEqual(
            fund["source_url"],
            "https://www.sec.gov/Archives/edgar/data/106830/000141036826091076/primary_doc.xml",
        )

    def test_filings_already_in_the_previous_registry_are_not_fetched_again(self):
        filings = [{"cik": "106830", "date_filed": "2026-09-05", "accession_no": "0001410368-26-091076"}]
        first = money_market.build_registry(filings, lambda url: NMFP3, {})
        fetched = []

        second = money_market.build_registry(filings, lambda url: fetched.append(url), {}, previous=first)

        self.assertEqual(fetched, [])
        self.assertEqual(second["funds"][0]["series_id"], "S000004462")

    def test_an_unreadable_filing_is_skipped_not_fatal(self):
        filings = [
            {"cik": "106830", "date_filed": "2026-09-05", "accession_no": "0001410368-26-091076"},
            {"cik": "857156", "date_filed": "2026-09-05", "accession_no": "0000857156-26-000123"},
        ]
        docs = {"0001410368-26-091076": NMFP3}

        registry = money_market.build_registry(
            filings, lambda url: next((v for k, v in docs.items() if k.replace("-", "") in url), None), {}
        )

        self.assertEqual([f["series_id"] for f in registry["funds"]], ["S000004462"])


class SnapshotTests(unittest.TestCase):
    def test_a_money_market_snapshot_carries_its_stable_price_facts(self):
        parsed = {
            "fund": {
                "series_id": "S000004462",
                "as_of": "2026-08-31",
                "share_classes": [{"class_id": "C000012238", "ticker": "VMFXX", "name": None}],
                "money_market": money_market.money_market_info(money_market.parse_nmfp(NMFP3)),
            },
            "holdings": [],
            "filing": {"accession_no": "0001410368-26-091076", "source_url": "https://example.invalid"},
        }

        snapshot = transform.to_json1(parsed)

        self.assertEqual(snapshot["schema_version"], "0.6")
        self.assertEqual(
            snapshot["fund"]["money_market"],
            {"category": "Government", "seeks_stable_price": True, "stable_price_per_share": 1.0, "is_retail": False},
        )


if __name__ == "__main__":
    unittest.main()
