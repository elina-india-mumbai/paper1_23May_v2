"""
pull_rsrt_master.py  (v2 — award-level)
========================================

One-shot pull of higher-education awards from USAspending.gov, classified
by RSRT via keyword search on award descriptions, for the 5 parent agencies
in scope. Produces TWO outputs from a single pull:

1. rsrt_master_awards.{csv,pkl}
   Award-level. One row per award. Includes Award Description so each
   classification is manually verifiable. This is the audit / reproducibility
   artifact and the basis for any supplementary table in the paper.

   Columns:
       rsrt, rsrt_canonical_term, agency, award_id, recipient,
       award_description, fy, obligated_usd, award_type, start_date, end_date

2. rsrt_master.{csv,pkl}
   Recipient-aggregate. One row per (recipient, rsrt, agency, fy) with
   summed obligated_usd. No description column. This is what
   kg_build_clean.ipynb consumes.

   Columns:
       rsrt, agency, recipient, fy, obligated_usd

Also writes:
    rsrt_master_pull_log.json   (per-bucket call counts + result counts)

Endpoint
--------
POST /api/v2/search/spending_by_award

Filters applied per bucket
--------------------------
- time_period: FY window (Oct 1 prior year - Sep 30 FY year)
- agencies: {type=funding, tier=toptier, name=<full agency name>}
- recipient_type_names: higher_education + public/private institution + MSI
- keywords: [single canonical term for the RSRT]
- award_type_codes: ["02","03","04","05"]  (grants + cooperative agreements)

Methodology notes
-----------------
* Multi-RSRT overlap: an award matching two canonical terms (e.g. "hypersonic"
  and "advanced materials") appears under BOTH RSRTs at its full obligated
  value. We do NOT split equally. Per-RSRT totals therefore describe
  "obligations to awards matching this canonical term," not a partition of
  total federal spending. Multi-RSRT overlap is detectable post-hoc by
  grouping award_id across rsrt in the awards file.

* C4ISR uses three canonical sub-terms (synthetic aperture radar, target
  tracking, electronic warfare). An award matching more than one sub-term
  will appear multiple times in the awards file under different
  rsrt_canonical_term values. The recipient-aggregate master de-duplicates
  by award_id within (rsrt, recipient, agency, fy) BEFORE summing, so a
  C4ISR award matching two sub-terms is counted ONCE inside C4ISR.

* "obligated_usd" comes from the API field `Total Obligated Amount`,
  filtered to the FY by USAspending's time_period filter. This is the
  obligation as of the most recent transaction within the FY window; it is
  the same dollar field used in the recipient-level endpoint and in the
  prior working dashboard, so figures are comparable to earlier outputs.

* Agency filter type: 'funding', not 'awarding'. Captures the agency that
  actually paid, even when a pass-through awarding agency differs.

* FY2026 excluded as partial fiscal year.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE = "https://api.usaspending.gov/api/v2"
ENDPOINT = f"{API_BASE}/search/spending_by_award"

AGENCIES = {
    "Department of Defense":                          "DoD",
    "Department of Energy":                           "DOE",
    "Department of Health and Human Services":        "HHS",
    "National Science Foundation":                    "NSF",
    "National Aeronautics and Space Administration":  "NASA",
}

RECIPIENT_TYPES = [
    "higher_education",
    "public_institution_of_higher_education",
    "private_institution_of_higher_education",
    "minority_serving_institution_of_higher_education",
]

# Award type codes per USAspending docs:
#   02 = Block Grant
#   03 = Formula Grant
#   04 = Project Grant
#   05 = Cooperative Agreement
AWARD_TYPE_CODES = ["02", "03", "04", "05"]

FISCAL_YEARS = list(range(2020, 2026))  # FY2020..FY2025 inclusive

RSRT_TERMS: dict[str, list[str]] = {
    "Hypersonics":                          ["hypersonic"],
    "Directed_Energy":                      ["directed energy"],
    "Networked_Sensing_C4ISR":              ["synthetic aperture radar",
                                             "target tracking",
                                             "electronic warfare"],
    "Cybersecurity_Data_Privacy":           ["cybersecurity"],
    "Advanced_Computing_Semiconductors":    ["microelectronics"],
    "Quantum_Information_Science":          ["quantum information"],
    "AI_Autonomy":                          ["artificial intelligence"],
    "Advanced_Materials":                   ["advanced materials"],
    "Space_Technology":                     ["space technology"],
    "Advanced_Manufacturing":               ["advanced manufacturing"],
    "Biotechnology":                        ["biotechnology"],
    "Future_Gen_Communications":            ["5G"],
    "HMI_Robotics":                         ["human-machine interface"],
    "Advanced_Energy":                      ["advanced energy"],
    "Disaster_Resilience":                  ["disaster"],
}

# Fields requested from spending_by_award. USAspending uses these exact
# strings (case- and space-sensitive); they map onto the customizable
# award-search column set in the web UI.
AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Total Obligated Amount",
    "Description",
    "Start Date",
    "End Date",
    "Award Type",
    "Awarding Agency",
    "Funding Agency",
    "recipient_id",
    "generated_internal_id",
]

PAGE_LIMIT = 100        # max per page for spending_by_award
RATE_SLEEP = 0.30
REQUEST_TIMEOUT = 90
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

OUT_AWARDS_CSV   = "rsrt_master_awards.csv"
OUT_AWARDS_PKL   = "rsrt_master_awards.pkl"
OUT_MASTER_CSV   = "rsrt_master.csv"
OUT_MASTER_PKL   = "rsrt_master.pkl"
OUT_LOG          = "rsrt_master_pull_log.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fy_to_dates(fy: int) -> dict:
    return {"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30"}


@dataclass
class BucketLog:
    rsrt: str
    term: str
    agency_abbr: str
    agency_name: str
    fy: int
    pages: int = 0
    rows: int = 0
    obligated_total: float = 0.0
    error: Optional[str] = None


@dataclass
class PullLog:
    started_at: str
    finished_at: Optional[str] = None
    fiscal_years: list[int] = field(default_factory=list)
    agencies: list[str] = field(default_factory=list)
    rsrts: list[str] = field(default_factory=list)
    award_type_codes: list[str] = field(default_factory=list)
    buckets: list[dict] = field(default_factory=list)
    total_calls: int = 0
    total_errors: int = 0


def post_with_retry(payload: dict) -> dict:
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE ** attempt)
    assert last_exc is not None
    raise last_exc


def fetch_bucket(agency_name: str, fy: int, keyword: str) -> tuple[list[dict], int]:
    """Pull all paginated award-level results for one (agency, fy, keyword) bucket."""
    dates = fy_to_dates(fy)
    filters = {
        "time_period": [dates],
        "agencies": [{"type": "funding", "tier": "toptier", "name": agency_name}],
        "recipient_type_names": RECIPIENT_TYPES,
        "keywords": [keyword],
        "award_type_codes": AWARD_TYPE_CODES,
    }
    all_results: list[dict] = []
    page = 1
    while True:
        payload = {
            "filters": filters,
            "fields": AWARD_FIELDS,
            "limit": PAGE_LIMIT,
            "page": page,
            "sort": "Total Obligated Amount",
            "order": "desc",
            # subawards=false: top-level awards only, no sub-award explosion
            "subawards": False,
        }
        data = post_with_retry(payload)
        results = data.get("results", []) or []
        all_results.extend(results)
        page_meta = data.get("page_metadata", {}) or {}
        has_next = page_meta.get("hasNext")
        if has_next is None:
            if len(results) < PAGE_LIMIT:
                break
        elif not has_next:
            break
        page += 1
        time.sleep(RATE_SLEEP)
    return all_results, page


# ---------------------------------------------------------------------------
# Main pull
# ---------------------------------------------------------------------------

def run_pull() -> tuple[pd.DataFrame, pd.DataFrame]:
    log = PullLog(
        started_at=datetime.utcnow().isoformat() + "Z",
        fiscal_years=FISCAL_YEARS,
        agencies=list(AGENCIES.values()),
        rsrts=list(RSRT_TERMS.keys()),
        award_type_codes=AWARD_TYPE_CODES,
    )

    rows: list[dict] = []

    total_buckets = (
        sum(len(t) for t in RSRT_TERMS.values())
        * len(AGENCIES)
        * len(FISCAL_YEARS)
    )
    bucket_i = 0

    for rsrt, terms in RSRT_TERMS.items():
        for term in terms:
            for agency_full, agency_abbr in AGENCIES.items():
                for fy in FISCAL_YEARS:
                    bucket_i += 1
                    bucket = BucketLog(
                        rsrt=rsrt, term=term,
                        agency_abbr=agency_abbr, agency_name=agency_full,
                        fy=fy,
                    )
                    prefix = (
                        f"[{bucket_i:>4}/{total_buckets}] "
                        f"{rsrt[:28]:<28} | {agency_abbr:<4} | FY{fy} | term={term!r}"
                    )
                    try:
                        results, pages = fetch_bucket(agency_full, fy, term)
                    except Exception as e:
                        bucket.error = f"{type(e).__name__}: {e}"
                        log.total_errors += 1
                        log.total_calls += 1
                        log.buckets.append(bucket.__dict__)
                        print(f"{prefix}  ERROR: {bucket.error}", file=sys.stderr)
                        continue

                    bucket.pages = pages
                    bucket.rows = len(results)
                    bucket_obl = 0.0
                    for r in results:
                        obl = float(r.get("Total Obligated Amount") or 0)
                        bucket_obl += obl

                        # Awarding/Funding agency are returned as nested dicts with
                        # toptier_agency.name; we keep them as flat strings for the
                        # audit file (None-safe).
                        def _agency_name(d):
                            if not isinstance(d, dict):
                                return None
                            top = d.get("toptier_agency") or {}
                            return top.get("name")

                        rows.append({
                            "rsrt": rsrt,
                            "rsrt_canonical_term": term,
                            "agency": agency_abbr,                # the bucket's agency
                            "award_id":           r.get("Award ID"),
                            "internal_id":        r.get("generated_internal_id"),
                            "recipient":          r.get("Recipient Name", "Unknown"),
                            "recipient_id":       r.get("recipient_id"),
                            "award_description":  r.get("Description", ""),
                            "fy":                 fy,
                            "obligated_usd":      obl,
                            "award_amount":       float(r.get("Award Amount") or 0),
                            "award_type":         r.get("Award Type"),
                            "start_date":         r.get("Start Date"),
                            "end_date":           r.get("End Date"),
                            "awarding_agency":    _agency_name(r.get("Awarding Agency")),
                            "funding_agency":     _agency_name(r.get("Funding Agency")),
                        })
                    bucket.obligated_total = bucket_obl
                    log.total_calls += pages
                    print(
                        f"{prefix}  -> {bucket.rows:>4} awards, "
                        f"${bucket_obl/1e6:>8.2f}M, {pages} page(s)"
                    )
                    log.buckets.append(bucket.__dict__)
                    time.sleep(RATE_SLEEP)

    if not rows:
        print("\nNo data returned. Aborting.", file=sys.stderr)
        sys.exit(1)

    df_awards = pd.DataFrame(rows)

    # -----------------------------------------------------------------
    # Recipient-aggregate master file.
    #
    # Within a single RSRT, an award could appear multiple times if it
    # matched more than one canonical term (only relevant for C4ISR, which
    # has three sub-terms). We de-duplicate by award_id within
    # (rsrt, recipient, agency, fy) before summing, so a C4ISR award
    # matching two sub-terms is counted once inside C4ISR.
    # -----------------------------------------------------------------
    df_master = (
        df_awards
          .drop_duplicates(subset=["rsrt", "agency", "recipient", "fy", "award_id"])
          .groupby(["rsrt", "agency", "recipient", "fy"], as_index=False)["obligated_usd"]
          .sum()
          .sort_values(["rsrt", "agency", "fy", "obligated_usd"],
                       ascending=[True, True, True, False])
          .reset_index(drop=True)
    )

    # -----------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------
    print("\n=== Per-agency row counts (recipient-aggregate master) ===")
    for agency_abbr in AGENCIES.values():
        n = int((df_master["agency"] == agency_abbr).sum())
        tag = "  <-- WARNING: zero rows" if n == 0 else ""
        print(f"  {agency_abbr:<5} {n:>6} rows{tag}")

    print("\n=== Per-RSRT award counts (audit file) ===")
    for rsrt in RSRT_TERMS:
        n_awards = int((df_awards["rsrt"] == rsrt).sum())
        n_unique = df_awards.loc[df_awards["rsrt"] == rsrt, "award_id"].nunique()
        print(f"  {rsrt[:32]:<32} {n_awards:>6} rows, {n_unique:>6} unique awards")

    # Multi-RSRT awards (informational, not corrected)
    multi = (
        df_awards.groupby("award_id")["rsrt"].nunique().loc[lambda s: s > 1]
    )
    print(f"\n=== Multi-RSRT awards: {len(multi)} awards match >1 RSRT ===")
    print("(These are counted at full value under each matching RSRT;\n"
          " disclose in Methods. Detection key: groupby award_id in awards file.)")

    log.finished_at = datetime.utcnow().isoformat() + "Z"

    # -----------------------------------------------------------------
    # Write outputs
    # -----------------------------------------------------------------
    df_awards.to_csv(OUT_AWARDS_CSV, index=False)
    df_awards.to_pickle(OUT_AWARDS_PKL)
    df_master.to_csv(OUT_MASTER_CSV, index=False)
    df_master.to_pickle(OUT_MASTER_PKL)
    with open(OUT_LOG, "w") as f:
        json.dump(log.__dict__, f, indent=2, default=str)

    print(f"\nWrote:")
    print(f"  {OUT_AWARDS_CSV}   ({df_awards.shape[0]:,} rows, {df_awards.shape[1]} cols)")
    print(f"  {OUT_AWARDS_PKL}")
    print(f"  {OUT_MASTER_CSV}   ({df_master.shape[0]:,} rows, {df_master.shape[1]} cols)")
    print(f"  {OUT_MASTER_PKL}")
    print(f"  {OUT_LOG}")
    print(f"\nTotal API calls: {log.total_calls}  |  Errors: {log.total_errors}")

    return df_awards, df_master


if __name__ == "__main__":
    run_pull()
