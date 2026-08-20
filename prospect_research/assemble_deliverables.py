"""
Step 5: Assemble final deliverables:
- AP_Online_Jobs_Prospects.xlsx (Prospects + Sources sheets)
- AP_Online_Jobs_Prospects.csv
- AP_Online_Jobs_Prospects_Summary.md
"""
import pandas as pd

BASE = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research"
FINAL_CSV = f"{BASE}\\final_100.csv"
SOURCES_CSV = f"{BASE}\\sources.csv"

XLSX_OUT = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\AP_Online_Jobs_Prospects.xlsx"
CSV_OUT = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\AP_Online_Jobs_Prospects.csv"
MD_OUT = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\AP_Online_Jobs_Prospects_Summary.md"

COLUMN_ORDER = [
    ("Company Name", "Company Name"),
    ("Industry", "Industry"),
    ("State", "State"),
    ("Headquarters Address", "Headquarters Address"),
    ("Website", "Website"),
    ("General Company Email", "General Company Email"),
    ("HR Email", "HR Email"),
    ("Recruitment Email", "Recruitment Email"),
    ("Main Phone Number", "Main Phone Number"),
    ("Company LinkedIn URL", "Company LinkedIn URL"),
    ("Careers Page URL", "Careers Page URL"),
    ("Estimated Employees", "Estimated Employees"),
    ("Estimated Foreign Worker Demand", "Estimated Foreign Worker Demand"),
    ("Why This Company Is A Good Prospect", "Why This Company Is A Good Prospect"),
    ("Suggested Decision Maker", "Suggested Decision Maker"),
    ("Hiring Status", "Hiring Status"),
    ("Recent Expansion", "Recent Expansion"),
    ("Prospect Score", "Prospect Score"),
    ("Personalization Notes", "Personalization Notes"),
    ("First Outreach Email", "First Outreach Email"),
]


def build_prospects_df():
    df = pd.read_csv(FINAL_CSV)
    out = pd.DataFrame()
    for src, dst in COLUMN_ORDER:
        out[dst] = df[src]
    out = out.sort_values("Prospect Score", ascending=False).reset_index(drop=True)
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out


def build_markdown(prospects: pd.DataFrame, raw: pd.DataFrame):
    top20 = prospects.head(20)
    industry_counts = prospects["Industry"].value_counts()
    state_counts = prospects["State"].value_counts()
    avg_score = prospects["Prospect Score"].mean()
    hiring_now = prospects[prospects["Hiring Status"] == "Currently Hiring"]
    expansion = prospects[prospects["Recent Expansion"] != "Not Publicly Available"]
    hr_email_listed = prospects[prospects["HR Email"] != "Not Publicly Available"]

    lines = []
    lines.append("# AP Online Jobs — B2B Prospect Database Summary\n")
    lines.append(
        "Research base: 100 Malaysian manufacturing/logistics companies selected from "
        "`Companies_Data_Master_Tiered.xlsx` (Master directory scrape + Whale Accounts / "
        "Tier A-D FWCMS signals), filtered to the priority sectors in the brief and screened "
        "against the Suppression List. Top-ranked prospects were additionally verified via "
        "live web research (see `Sources` sheet / `sources.csv`).\n"
    )

    lines.append("## Top 20 Hottest Prospects\n")
    lines.append("| Rank | Company | Industry | State | Prospect Score | Hiring Status |")
    lines.append("|---|---|---|---|---|---|")
    for _, r in top20.iterrows():
        lines.append(
            f"| {r['Rank']} | {r['Company Name']} | {r['Industry']} | {r['State']} | "
            f"{r['Prospect Score']} | {r['Hiring Status']} |"
        )
    lines.append("")

    lines.append("## Industry Breakdown\n")
    lines.append("| Industry | Companies |")
    lines.append("|---|---|")
    for ind, cnt in industry_counts.items():
        lines.append(f"| {ind} | {cnt} |")
    lines.append("")

    lines.append("## State Breakdown\n")
    lines.append("| State | Companies |")
    lines.append("|---|---|")
    for st, cnt in state_counts.items():
        lines.append(f"| {st} | {cnt} |")
    lines.append("")

    lines.append("## Key Metrics\n")
    lines.append(f"- **Average Prospect Score:** {avg_score:.1f} / 100")
    lines.append(f"- **Companies Currently Hiring (verified):** {len(hiring_now)}")
    lines.append(f"- **Companies with Verified Manufacturing Expansion:** {len(expansion)}")
    lines.append(f"- **Companies with Publicly Listed HR Email:** {len(hr_email_listed)}")
    lines.append("")

    if len(hiring_now):
        lines.append("### Companies Currently Hiring (verified)\n")
        for _, r in hiring_now.iterrows():
            lines.append(f"- **{r['Company Name']}** ({r['Industry']}, {r['State']})")
        lines.append("")

    if len(expansion):
        lines.append("### Companies with Verified Recent Expansion\n")
        for _, r in expansion.iterrows():
            lines.append(f"- **{r['Company Name']}**: {r['Recent Expansion']}")
        lines.append("")

    lines.append("## Methodology & Limitations\n")
    lines.append(
        "- Base fields (industry category, address, website, general email, phone, LinkedIn) "
        "come directly from the provided directory-scrape workbook, which itself reflects "
        "public directory/website scraping.\n"
        "- `Estimated Foreign Worker Demand` is derived from real FWCMS government worker-request "
        "records where a company appeared in the Whale Accounts sheet; otherwise it is a "
        "conservative estimate based on sector labour-intensity.\n"
        "- `Prospect Score` (1-100) is a composite of sector labour-intensity, FWCMS/Tier signal "
        "strength, and contact-data completeness — it is a relative ranking tool, not a "
        "probability.\n"
        "- `Why This Company Is A Good Prospect`, `Personalization Notes`, `Suggested Decision "
        "Maker`, and `First Outreach Email` were generated by an LLM constrained strictly to the "
        "verified facts above (no invented company-specific claims).\n"
        "- `Careers Page URL`, `Hiring Status`, `Recent Expansion`, `Estimated Employees`, `HR "
        "Email`, and `Recruitment Email` were **live-verified via web search for the Top 20 "
        "ranked prospects only** (see Sources sheet). For the remaining companies these fields "
        "are honestly marked **Not Publicly Available** rather than guessed, per the brief's "
        "verification rules. Extending live verification to the full 100 is a straightforward "
        "next step (see `prospect_research/apply_verification.py`).\n"
    )

    with open(MD_OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {MD_OUT}")


def main():
    prospects = build_prospects_df()
    sources = pd.read_csv(SOURCES_CSV)

    prospects.to_csv(CSV_OUT, index=False, encoding="utf-8-sig")
    print(f"Wrote {CSV_OUT}")

    with pd.ExcelWriter(XLSX_OUT, engine="openpyxl") as writer:
        prospects.to_excel(writer, sheet_name="Prospects", index=False)
        sources.to_excel(writer, sheet_name="Sources", index=False)
    print(f"Wrote {XLSX_OUT}")

    build_markdown(prospects, sources)


if __name__ == "__main__":
    main()
