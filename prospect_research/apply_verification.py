"""
Step 4: Apply live web-verification results for the Top prospects to
enriched_100.csv. Only overwrites fields we could actually confirm via a
public source; everything else stays "Not Publicly Available". Also builds
the Sources worksheet data (one row per citation).

This covers the Top 20 ranked prospects (mostly Semiconductor + top Rubber
tier). The remaining companies keep the grounded-but-unverified defaults
from generate_narrative.py / build_base.py.
"""
import pandas as pd

IN_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\enriched_100.csv"
OUT_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\final_100.csv"
SOURCES_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\sources.csv"

# field overrides per company (verified via live web search, Aug 2026)
OVERRIDES = {
    "Exis Tech Sdn Bhd": {
        "Careers Page URL": "https://www.ricebowl.my/company/exis-tech-sdn-bhd/jobs",
        "Recruitment Email": "hr_admin@exis-tech.com",
        "Estimated Employees": "40-50",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
    "Durapower Sdn Bhd": {
        "Industry": "Plastic Manufacturing",
        "Careers Page URL": "Not Publicly Available",
        "Hiring Status": "Not Publicly Available",
        "Recent Expansion": "Not Publicly Available",
    },
    "MIMOS SEMICONDUCTOR SDN BHD (MSSB)": {
        "Careers Page URL": "https://mimos-services.my/",
        "Recruitment Email": "career@mimos-services.my",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Renamed/restructured from MIMOS Semiconductor Sdn Bhd to MIMOS Services Sdn Bhd (May 2024) with a commercialisation charter for semiconductor, electronics and digital-identity services.",
    },
    "Get Technologies Sdn Bhd": {
        "Careers Page URL": "Not Publicly Available",
        "Hiring Status": "Occasionally Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
    "JHT SEMICONDUCTOR SDN. BHD.": {
        "Careers Page URL": "https://jhtsemiconductor.com/jobs/",
        "Recruitment Email": "fangyee.lim@jhtsemi.com",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Opened a new state-of-the-art IC test-handler manufacturing facility in Batu Kawan, Penang (grand opening 15 Feb 2025), per MIDA press release.",
    },
    "CONTROL AUTOMATION TECHNOLOGY SDN BHD": {
        "Careers Page URL": "https://cat-my.com/careers/",
        "Estimated Employees": "~14",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
    "OXFORD INNOTECH BERHAD": {
        "Careers Page URL": "https://oxfordinnotech.com/career/",
        "Recruitment Email": "hr@oxfordinnotech.com",
        "Estimated Employees": "~25",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "New Penang Science Park Factory 2 (Phase 1, 39,392 sqft) completed May 2025 and running production trials; Phase 2 (+67,722 sqft) targeted by 2027, expanding total manufacturing area to ~192,896 sqft (The Edge Malaysia; company blog).",
    },
    "Texchem-Pack (PP) Sdn Bhd": {
        "Careers Page URL": "https://texchemgroup.com/careers/",
        "Hiring Status": "Occasionally Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
    "JF MICROTECHNOLOGY SDN BHD": {
        "Careers Page URL": "https://www.jf-technology.com/join-us",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Parent JF Technology Berhad established a RM40 million Test Contacting Centre of Excellence in Kota Damansara, Selangor (announced 2022 via MIDA), targeting ~77 new jobs by 2025.",
    },
    "Kilang Kejenteraan Hup Hing Sdn Bhd": {
        "Careers Page URL": "Not Publicly Available",
        "Hiring Status": "Not Publicly Available",
        "Recent Expansion": "Not Publicly Available",
    },
    "EDELTEQ TECHNOLOGIES SDN BHD": {
        "Careers Page URL": "Not Publicly Available",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "New Batu Kawan factory construction underway for additional capacity; listed on the ACE Market of Bursa Malaysia (per FY2023 Annual Report).",
    },
    "RENESAS SEMICONDUCTOR KL SDN BHD": {
        "Careers Page URL": "https://rskl.renesas.com/career/",
        "Estimated Employees": "~86",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Not Publicly Available for this Selangor entity specifically (a related Renesas Malaysia entity in Penang separately announced a RM1 billion, ~200-job AI-manufacturing expansion in Jul 2026).",
    },
    "FREESCALE SEMICONDUCTOR MALAYSIA SDN BHD": {
        "Careers Page URL": "https://www.nxp.com/careers",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Legal entity now operates under the NXP Semiconductors brand (NXP acquired Freescale in 2015); NXP Malaysia has active openings in KL/Petaling Jaya.",
    },
    "PARADIGM PRECISION COMPONENTS SDN BHD": {
        "Careers Page URL": "https://ppc.net.my/career/",
        "Recruitment Email": "kobay.paces.hr@kobaytech.com",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
    "VISHAY SEMICONDUCTOR MALAYSIA SDN BHD": {
        "State": "Malacca",
        "Careers Page URL": "https://jobs.vishay.com/",
        "Hiring Status": "Currently Hiring",
        "Recent Expansion": "Not Publicly Available",
    },
}

SOURCES = [
    ("Exis Tech Sdn Bhd", "Hiring Status / Careers", "https://www.ricebowl.my/company/exis-tech-sdn-bhd/jobs", "Ricebowl company jobs page"),
    ("Exis Tech Sdn Bhd", "Hiring Status", "https://www.linkedin.com/posts/exis-tech_existech-wearehiring-joinourteam-activity-7484496116730720256-8IZ4", "LinkedIn hiring post, Jul 2026"),
    ("Durapower Sdn Bhd", "Industry / Address", "https://durapower.com.my/about", "Company About page (Kulai, Johor - plastic films)"),
    ("MIMOS SEMICONDUCTOR SDN BHD (MSSB)", "Recent Expansion / Careers", "https://mimos-services.my/about/", "MIMOS Services About page (rename history)"),
    ("Get Technologies Sdn Bhd", "Hiring Status", "https://my.trabajo.org/job-5053-a5dc5e509809e62d3f88ad4dbefa4c5b", "3rd-party job listing"),
    ("JHT SEMICONDUCTOR SDN. BHD.", "Recent Expansion", "https://www.mida.gov.my/media-release/jht-semiconductor-launches-state-of-the-art-manufacturing-facility-in-penang/", "MIDA press release, 17 Feb 2025"),
    ("JHT SEMICONDUCTOR SDN. BHD.", "Careers / Hiring Status", "https://jhtsemiconductor.com/jobs/", "Company careers page"),
    ("CONTROL AUTOMATION TECHNOLOGY SDN BHD", "Careers / Hiring Status", "https://cat-my.com/careers/", "Company careers page"),
    ("OXFORD INNOTECH BERHAD", "Recent Expansion", "https://theedgemalaysia.com/node/771350", "The Edge Malaysia, Sept 2025"),
    ("OXFORD INNOTECH BERHAD", "Careers / Hiring Status", "https://oxfordinnotech.com/career/", "Company careers page"),
    ("Texchem-Pack (PP) Sdn Bhd", "Careers / Corporate structure", "https://texchemgroup.com/careers/", "Texchem Resources Bhd (Bursa Malaysia-listed) group careers page"),
    ("JF MICROTECHNOLOGY SDN BHD", "Recent Expansion", "https://www.mida.gov.my/media-release/jf-technology-announced-the-establishment-of-test-contacting-centre-of-excellence-in-selangor-with-an-investment-of-rm40-million/", "MIDA press release, 16 Mar 2022"),
    ("JF MICROTECHNOLOGY SDN BHD", "Careers / Hiring Status", "https://my.mncjobz.com/company/jf-technology-berhad", "JF Technology Berhad careers listing"),
    ("EDELTEQ TECHNOLOGIES SDN BHD", "Recent Expansion", "https://edelteq.com/wp-content/uploads/2024/04/Annual-Report-2023.pdf", "Edelteq Group FY2023 Annual Report"),
    ("EDELTEQ TECHNOLOGIES SDN BHD", "Hiring Status", "https://malaysia.indeed.com/q-edelteq-jobs.html", "Indeed job listings"),
    ("RENESAS SEMICONDUCTOR KL SDN BHD", "Careers / Employees", "https://rskl.renesas.com/career/", "Company careers page"),
    ("RENESAS SEMICONDUCTOR KL SDN BHD", "Related-entity expansion context", "https://www.pocketnews.com.my/2026/07/29/renesas-launches-second-phase-of-solar-project-in-penang/", "Pocket News, 29 Jul 2026 (separate Penang Renesas entity)"),
    ("FREESCALE SEMICONDUCTOR MALAYSIA SDN BHD", "Corporate structure / Hiring", "https://my.jobstreet.com/companies/nxp-semiconductors-168550041790370", "Jobstreet NXP Semiconductors company profile"),
    ("PARADIGM PRECISION COMPONENTS SDN BHD", "Careers / Hiring Status", "https://ppc.net.my/career/", "Company careers page"),
    ("PARADIGM PRECISION COMPONENTS SDN BHD", "Recruitment Email", "https://www.linkedin.com/posts/sharon-tan-541931101_hiring-precisionengineering-manufacturing-activity-7480437563023912961-ZThe", "LinkedIn hiring post, Jul 2026"),
    ("VISHAY SEMICONDUCTOR MALAYSIA SDN BHD", "State / Careers", "https://jobs.vishay.com/jobs/staff-package-development-engineer/", "Vishay Jobs portal (Krubong, Melaka)"),
]


def main():
    df = pd.read_csv(IN_CSV)
    for name, fields in OVERRIDES.items():
        mask = df["Company Name"] == name
        if not mask.any():
            print(f"WARNING: {name} not found in dataset")
            continue
        for col, val in fields.items():
            df.loc[mask, col] = val

    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(df)} rows -> {OUT_CSV}")

    src_df = pd.DataFrame(SOURCES, columns=["Company Name", "Field(s) Verified", "Source URL", "Note"])
    src_df.to_csv(SOURCES_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(src_df)} source rows -> {SOURCES_CSV}")


if __name__ == "__main__":
    main()
