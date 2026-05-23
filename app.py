"""
RSRT Validator Dashboard (v2 — with award-level drill-down)
============================================================

Live USAspending.gov validation tool for the 15 RSRTs x 5 parent agencies.
Pulls AWARD-LEVEL data (not recipient-aggregate) so descriptions are
manually verifiable per (recipient, RSRT) cell.

Layout:
    Sidebar: RSRT / agency / FY filters, top-N slider, fetch button.
    Main:
        1. Summary KPIs.
        2. Per-agency sanity panel (catches zero-row agencies prominently).
        3. Top-N recipients table + horizontal bar.
        4. Agency x RSRT heatmap.
        5. Year-over-year trend by RSRT.
        6. AWARD-LEVEL DRILL-DOWN:
              Pick a recipient + RSRT and inspect every award returned,
              with full descriptions, dates, types, dollar amounts.

Run:
    streamlit run app.py
"""

import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st


# ── Config ──────────────────────────────────────────────────────────────────
st.set_page_config(page_title="RSRT Validator (Paper 1)", layout="wide")

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

AWARD_TYPE_CODES = ["02", "03", "04", "05"]  # grants + cooperative agreements

FISCAL_YEARS = list(range(2020, 2025))

RSRT_TERMS = {
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

AWARD_FIELDS = [
    "Award ID", "Recipient Name", "Award Amount", "Total Obligated Amount",
    "Description", "Start Date", "End Date", "Award Type",
    "Awarding Agency", "Funding Agency",
    "recipient_id", "generated_internal_id",
]

PAGE_LIMIT = 100
RATE_SLEEP = 0.3


# ── API helpers ────────────────────────────────────────────────────────────
def fy_to_dates(fy: int) -> dict:
    return {"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30"}


def _agency_name(d):
    if not isinstance(d, dict):
        return None
    top = d.get("toptier_agency") or {}
    return top.get("name")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_bucket(agency_name: str, fy: int, keyword: str) -> list[dict]:
    """One bucket: (agency, fy, keyword) -> list of award dicts."""
    dates = fy_to_dates(fy)
    filters = {
        "time_period": [dates],
        "agencies": [{"type": "funding", "tier": "toptier", "name": agency_name}],
        "recipient_type_names": RECIPIENT_TYPES,
        "keywords": [keyword],
        "award_type_codes": AWARD_TYPE_CODES,
    }
    all_results = []
    page = 1
    while True:
        payload = {
            "filters": filters,
            "fields": AWARD_FIELDS,
            "limit": PAGE_LIMIT,
            "page": page,
            "sort": "Total Obligated Amount",
            "order": "desc",
            "subawards": False,
        }
        try:
            r = requests.post(ENDPOINT, json=payload, timeout=90)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            st.error(f"API error ({agency_name} | FY{fy} | {keyword!r} | page {page}): {e}")
            break

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
    return all_results


def build_dataframes(rsrt_list, agency_full_list, fy_list):
    """Pull and return (awards_df, master_df). awards_df has descriptions."""
    rows = []
    total = sum(len(RSRT_TERMS[r]) for r in rsrt_list) * len(agency_full_list) * len(fy_list)
    progress = st.progress(0.0, text="Fetching...")
    step = 0

    for rsrt in rsrt_list:
        for term in RSRT_TERMS[rsrt]:
            for agency_full in agency_full_list:
                agency_abbr = AGENCIES[agency_full]
                for fy in fy_list:
                    progress.progress(
                        step / total,
                        text=f"{rsrt[:24]} | {agency_abbr} | FY{fy} | {term!r}",
                    )
                    results = fetch_bucket(agency_full, fy, term)
                    for r in results:
                        rows.append({
                            "rsrt": rsrt,
                            "rsrt_canonical_term": term,
                            "agency": agency_abbr,
                            "award_id":           r.get("Award ID"),
                            "internal_id":        r.get("generated_internal_id"),
                            "recipient":          r.get("Recipient Name", "Unknown"),
                            "award_description":  r.get("Description", "") or "",
                            "fy":                 fy,
                            "obligated_usd":      float(r.get("Total Obligated Amount") or 0),
                            "award_amount":       float(r.get("Award Amount") or 0),
                            "award_type":         r.get("Award Type"),
                            "start_date":         r.get("Start Date"),
                            "end_date":           r.get("End Date"),
                            "awarding_agency":    _agency_name(r.get("Awarding Agency")),
                            "funding_agency":     _agency_name(r.get("Funding Agency")),
                        })
                    step += 1

    progress.empty()
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    awards_df = pd.DataFrame(rows)

    master_df = (
        awards_df
          .drop_duplicates(subset=["rsrt", "agency", "recipient", "fy", "award_id"])
          .groupby(["rsrt", "agency", "recipient", "fy"], as_index=False)["obligated_usd"]
          .sum()
    )
    return awards_df, master_df


def format_dollars(v) -> str:
    if pd.isna(v) or v == 0:
        return "—"
    if abs(v) >= 1e9: return f"${v/1e9:,.2f}B"
    if abs(v) >= 1e6: return f"${v/1e6:,.2f}M"
    if abs(v) >= 1e3: return f"${v/1e3:,.1f}K"
    return f"${v:,.0f}"


# ── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.title("Filters")

rsrt_mode = st.sidebar.radio("RSRT Scope", ["Single RSRT", "Multiple RSRTs"])
if rsrt_mode == "Single RSRT":
    rsrt_list = [st.sidebar.selectbox("RSRT", options=list(RSRT_TERMS.keys()))]
else:
    rsrt_list = st.sidebar.multiselect(
        "Select RSRTs",
        options=list(RSRT_TERMS.keys()),
        default=list(RSRT_TERMS.keys())[:3],
    )

agency_pick = st.sidebar.multiselect(
    "Agencies",
    options=list(AGENCIES.keys()),
    default=list(AGENCIES.keys()),
)

fy_pick = st.sidebar.multiselect(
    "Fiscal Years",
    options=FISCAL_YEARS,
    default=FISCAL_YEARS,
)

top_n = st.sidebar.slider("Top N recipients", 10, 100, 25, 5)

fetch = st.sidebar.button("Fetch Data", type="primary", use_container_width=True)


# ── Main ────────────────────────────────────────────────────────────────────
st.title("RSRT Validator — Paper 1")
st.caption(
    "FY2020–FY2025 | 5 parent agencies | Higher-Ed recipients | "
    "Grants + Cooperative Agreements only (codes 02,03,04,05) | "
    "Source: USAspending.gov spending_by_award"
)

st.markdown(
    f"**Selected scope:** {len(rsrt_list)} RSRT(s) × {len(agency_pick)} agency(ies) "
    f"× {len(fy_pick)} FY(s) = "
    f"**{sum(len(RSRT_TERMS[r]) for r in rsrt_list) * len(agency_pick) * len(fy_pick)} buckets**"
)

if fetch:
    if not rsrt_list or not agency_pick or not fy_pick:
        st.warning("Select at least one RSRT, agency, and FY.")
    else:
        with st.spinner("Pulling award-level data..."):
            awards_df, master_df = build_dataframes(rsrt_list, agency_pick, fy_pick)
        if awards_df.empty:
            st.warning("No rows returned.")
        else:
            st.session_state["awards_df"] = awards_df
            st.session_state["master_df"] = master_df


# ── Display ─────────────────────────────────────────────────────────────────
if "awards_df" in st.session_state:
    awards_df = st.session_state["awards_df"]
    master_df = st.session_state["master_df"]

    # ── KPIs ────────────────────────────────────────────────────────────
    total = master_df["obligated_usd"].sum()
    n_awards = awards_df["award_id"].nunique()
    n_recipients = master_df["recipient"].nunique()
    n_rsrts = master_df["rsrt"].nunique()
    n_agencies = master_df["agency"].nunique()

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Obligations", format_dollars(total))
    k2.metric("Unique Awards", f"{n_awards:,}")
    k3.metric("Unique Recipients", f"{n_recipients:,}")
    k4.metric("RSRTs returned", f"{n_rsrts}")
    k5.metric("Agencies returned", f"{n_agencies}")

    st.divider()

    # ── Sanity panel ────────────────────────────────────────────────────
    st.subheader("Sanity check — rows and dollars by agency")
    sanity = (
        master_df.groupby("agency")
          .agg(rows=("obligated_usd", "size"),
               dollars=("obligated_usd", "sum"))
          .reindex(list(AGENCIES.values()), fill_value=0)
          .reset_index()
    )
    sanity["dollars_fmt"] = sanity["dollars"].apply(format_dollars)
    st.dataframe(sanity, use_container_width=True, hide_index=True)
    zeros = sanity[sanity["rows"] == 0]["agency"].tolist()
    if zeros:
        st.error(f"Zero rows for: {', '.join(zeros)} — investigate before treating as final.")

    st.divider()

    # ── Top recipients ──────────────────────────────────────────────────
    st.subheader(f"Top {top_n} recipients (across selected scope)")
    top_recip = (
        master_df.groupby("recipient")["obligated_usd"].sum()
                 .sort_values(ascending=False)
                 .head(top_n).reset_index()
    )
    top_recip["formatted"] = top_recip["obligated_usd"].apply(format_dollars)
    st.dataframe(
        top_recip[["recipient", "formatted"]].rename(
            columns={"recipient": "Recipient", "formatted": "Total Obligated"}
        ),
        use_container_width=True, hide_index=True
    )

    fig = px.bar(
        top_recip, x="obligated_usd", y="recipient", orientation="h",
        labels={"obligated_usd": "Obligated ($)", "recipient": ""},
        color="obligated_usd", color_continuous_scale="Blues",
    )
    fig.update_layout(
        yaxis=dict(autorange="reversed"),
        height=max(400, top_n * 24),
        coloraxis_showscale=False,
    )
    fig.update_traces(hovertemplate="<b>%{y}</b><br>$%{x:,.0f}<extra></extra>")
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── Agency × RSRT heatmap ───────────────────────────────────────────
    st.subheader("Agency × RSRT — Total Obligations")
    pivot_ar = master_df.pivot_table(
        index="rsrt", columns="agency", values="obligated_usd",
        aggfunc="sum", fill_value=0,
    )
    pivot_ar = pivot_ar.reindex(columns=[a for a in AGENCIES.values() if a in pivot_ar.columns])
    fig_heat = go.Figure(data=go.Heatmap(
        z=pivot_ar.values, x=pivot_ar.columns.tolist(), y=pivot_ar.index.tolist(),
        colorscale="Blues",
        hovertemplate="<b>%{y}</b><br>%{x}: $%{z:,.0f}<extra></extra>",
    ))
    fig_heat.update_layout(
        height=max(400, len(pivot_ar) * 30),
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig_heat, use_container_width=True)

    # ── Temporal trend ──────────────────────────────────────────────────
    st.subheader("Year-over-year — Total by RSRT")
    trend = master_df.groupby(["fy", "rsrt"])["obligated_usd"].sum().reset_index()
    fig_line = px.line(
        trend, x="fy", y="obligated_usd", color="rsrt", markers=True,
        labels={"fy": "Fiscal Year", "obligated_usd": "Obligated ($)"},
    )
    fig_line.update_layout(height=500)
    st.plotly_chart(fig_line, use_container_width=True)

    st.divider()

    # ── DRILL-DOWN PANEL ────────────────────────────────────────────────
    st.subheader("🔍 Award-level drill-down")
    st.markdown(
        "Pick a recipient and RSRT to see every award returned, with full "
        "descriptions. Use this to spot-check whether the canonical keyword "
        "captured the right awards."
    )

    col_d1, col_d2 = st.columns(2)
    with col_d1:
        # Recipients sorted by descending total in the current pull, so the
        # top recipients are at the top of the picker.
        recipient_order = (
            master_df.groupby("recipient")["obligated_usd"].sum()
                     .sort_values(ascending=False).index.tolist()
        )
        drill_recipient = st.selectbox(
            "Recipient",
            options=recipient_order,
            index=0,
            key="drill_recipient",
        )
    with col_d2:
        # RSRTs that actually have rows for the selected recipient.
        avail_rsrts = (
            master_df[master_df["recipient"] == drill_recipient]["rsrt"]
            .unique().tolist()
        )
        drill_rsrt = st.selectbox(
            "RSRT",
            options=avail_rsrts if avail_rsrts else ["(none)"],
            index=0,
            key="drill_rsrt",
        )

    drill = awards_df[
        (awards_df["recipient"] == drill_recipient)
        & (awards_df["rsrt"] == drill_rsrt)
    ].copy()

    if drill.empty:
        st.info("No awards for that combination.")
    else:
        drill_total = drill.drop_duplicates(subset=["award_id"])["obligated_usd"].sum()
        drill_unique = drill["award_id"].nunique()
        d1, d2, d3 = st.columns(3)
        d1.metric("Awards", f"{drill_unique:,}")
        d2.metric("Total Obligated", format_dollars(drill_total))
        d3.metric("Canonical term(s)",
                  ", ".join(sorted(drill["rsrt_canonical_term"].unique())))

        display_cols = [
            "fy", "agency", "award_id", "rsrt_canonical_term",
            "obligated_usd", "award_amount", "award_type",
            "start_date", "end_date", "award_description",
        ]
        drill_display = drill[display_cols].sort_values(
            ["fy", "obligated_usd"], ascending=[True, False]
        )
        st.dataframe(
            drill_display,
            use_container_width=True,
            height=500,
            column_config={
                "obligated_usd": st.column_config.NumberColumn("Obligated", format="$%.0f"),
                "award_amount":  st.column_config.NumberColumn("Award Amount", format="$%.0f"),
                "award_description": st.column_config.TextColumn("Description", width="large"),
            },
        )

        # Individual award detail expanders for very long descriptions
        with st.expander(f"Show full descriptions for all {drill_unique} awards"):
            for _, row in drill_display.iterrows():
                st.markdown(
                    f"**{row['award_id']}** — FY{row['fy']} — "
                    f"{format_dollars(row['obligated_usd'])} — "
                    f"matched on `{row['rsrt_canonical_term']}`"
                )
                st.write(row["award_description"] or "_(no description)_")
                st.divider()

    st.divider()

    # ── Downloads ───────────────────────────────────────────────────────
    st.subheader("Downloads")
    cdl1, cdl2 = st.columns(2)
    with cdl1:
        st.download_button(
            "Download AWARD-LEVEL (with descriptions) — CSV",
            data=awards_df.to_csv(index=False),
            file_name="rsrt_master_awards.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with cdl2:
        st.download_button(
            "Download RECIPIENT-AGGREGATE master — CSV",
            data=master_df.to_csv(index=False),
            file_name="rsrt_master.csv",
            mime="text/csv",
            use_container_width=True,
        )

else:
    st.info("Select filters in the sidebar and click **Fetch Data**.")
    st.markdown("""
    **What this tool does**
    - Queries USAspending.gov `spending_by_award` live for any combination of the 15 RSRTs × 5 agencies × FY2020–FY2025.
    - Filters to grants + cooperative agreements (codes 02, 03, 04, 05) per stated methodology.
    - Uses the documented canonical search term per RSRT (3 terms for C4ISR).
    - **Award-level data** with descriptions for manual verification.
    - **Drill-down panel** lets you inspect every award for any (recipient, RSRT) combination.
    - For full master-file generation, run `pull_rsrt_master.py` (separate script).
    """)
