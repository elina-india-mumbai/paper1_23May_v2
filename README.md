# Paper 1 — RSRT Data Pipeline (v2: award-level)

This is the second iteration. The first iteration used
`spending_by_category/recipient` (recipient-aggregate, no descriptions).
This version uses `spending_by_award` so each award's description is
available for manual verification.

## What it produces

**One pull → two outputs:**

1. **`rsrt_master_awards.{csv,pkl}`** — award-level audit file.
   One row per award. Has the description column. Use this for
   manual spot-checks and as a supplementary table in the paper.

2. **`rsrt_master.{csv,pkl}`** — recipient-aggregate master file.
   One row per `(rsrt, agency, recipient, fy)` with summed
   `obligated_usd`. No description column. This is what
   `kg_build_clean.ipynb` consumes.

Plus `rsrt_master_pull_log.json` with per-bucket call/result counts.

## Schema

**`rsrt_master_awards`** (audit file):
```
rsrt, rsrt_canonical_term, agency, award_id, internal_id,
recipient, recipient_id, award_description, fy,
obligated_usd, award_amount, award_type, start_date, end_date,
awarding_agency, funding_agency
```

**`rsrt_master`** (analysis file):
```
rsrt, agency, recipient, fy, obligated_usd
```

## Filters applied

- **Award types:** grants + cooperative agreements only (codes 02, 03, 04, 05).
  This is a tightening from earlier ad-hoc pulls, which included contracts and
  other award types. Matches the documented methodology.
- **Recipient types:** higher_education + public/private institution + MSI.
- **Agencies:** DoD, DOE, HHS, NSF, NASA (funding tier, top-level).
- **FY2020–FY2025** inclusive; FY2026 excluded as partial year.

## Methodology disclosures the paper must state

1. **Multi-RSRT overlap is not split.** An award matching two canonical
   terms (e.g., "hypersonic" AND "advanced materials") appears under both
   RSRTs at its full obligated value. Per-RSRT totals describe "obligations
   to awards matching this canonical term," not a partition of total
   federal spending. The awards file lets you quantify the overlap
   post-hoc by `groupby('award_id')['rsrt'].nunique()`.

2. **C4ISR uses three sub-terms.** Within C4ISR, an award matching more
   than one sub-term is counted ONCE inside C4ISR (deduplicated by
   `award_id` before summing). Across RSRTs the no-split rule above
   applies.

3. **Award-type filter:** grants + cooperative agreements only. Contracts,
   IDVs, loans, direct payments are excluded.

4. **Agency filter type:** `funding` (who paid), not `awarding` (who
   issued). Pass-through awards from one agency funded by another are
   credited to the funder.

5. **`obligated_usd`** = USAspending field "Total Obligated Amount"
   sliced by the FY time-period filter. Same field used in earlier pulls,
   so figures remain comparable.

## Why the old `master_consolidation.ipynb` bug can't recur

The old bug: DoD Award IDs (contract numbers like `N00014-22-1-2367`)
caused silent drops during `groupby('Award ID')` in consolidation.

The new pipeline:
- Filters out contracts entirely (only grants/coops).
- Doesn't use Award ID as a groupby key in the analysis path. The audit
  file keeps Award IDs for spot-checking, but the recipient-aggregate
  master groups by `(rsrt, agency, recipient, fy)`.
- Per-agency row counts are printed at the end of every pull; zero-row
  agencies trigger a `WARNING` line. The Streamlit app shows the same
  sanity panel on every fetch.

## To run

```bash
pip install -r requirements.txt

# One-shot master pull (~30–90 min depending on result volume)
python pull_rsrt_master.py

# Optional live validator + drill-down
streamlit run app.py
```

## What changed vs. v1

| Aspect | v1 | v2 (this) |
|---|---|---|
| Endpoint | `spending_by_category/recipient` | `spending_by_award` |
| Granularity | Recipient-aggregate only | Award-level + recipient-aggregate (both) |
| Descriptions | No | Yes (in audit file) |
| Award-type filter | None | Grants + coops only |
| Multi-RSRT detection | Impossible | Possible via `groupby award_id` |
| Drill-down in app | No | Yes (per recipient × RSRT) |
| Pull time | ~30 min | ~60–90 min |

## Files

- `pull_rsrt_master.py` — main data pull.
- `app.py` — Streamlit validator with award-level drill-down.
- `requirements.txt`
- `README.md` (this file)

## Next steps in the paper

1. Run `pull_rsrt_master.py` → verify the sanity panel.
2. Open `app.py`, pick a high-stakes RSRT (e.g., AI_Autonomy or Hypersonics),
   drill into the top 3–5 recipients, and confirm descriptions actually
   read as that topic. This is your reproducibility evidence for Methods.
3. Update `kg_build_clean.ipynb` to read `rsrt_master.pkl`. Schema:
   `rsrt, agency, recipient, fy, obligated_usd`.
4. Update `rq_analysis_v3.ipynb` for the new schema.
5. Draft Section IV (Methods) using the disclosures above.
6. Draft Section V (Results).
