"""
Step 2: Turn candidates_100.csv into the 20-column base dataset.
Fields grounded in real scraped data get filled directly. Fields that require
live verification per company (HR email, recruitment email, careers page,
employee count, hiring status, recent expansion) default to
"Not Publicly Available" here and are only overwritten later for companies
that get live web-verification (see verify_top_prospects.py).
"""
import re
import pandas as pd

IN_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\candidates_100.csv"
OUT_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\base_100.csv"

STATES = [
    "Johor", "Kedah", "Kelantan", "Malacca", "Melaka", "Negeri Sembilan",
    "Pahang", "Penang", "Pulau Pinang", "Perak", "Perlis", "Sabah", "Sarawak",
    "Selangor", "Terengganu", "Kuala Lumpur", "Labuan", "Putrajaya",
    "Wilayah Persekutuan Kuala Lumpur",
]
STATE_CANON = {
    "melaka": "Malacca", "pulau pinang": "Penang",
    "wilayah persekutuan kuala lumpur": "Kuala Lumpur",
    "wp kuala lumpur": "Kuala Lumpur", "kl": "Kuala Lumpur",
}

CITY_TO_STATE = {
    "shah alam": "Selangor", "petaling jaya": "Selangor", "klang": "Selangor",
    "subang jaya": "Selangor", "bayan lepas": "Penang", "george town": "Penang",
    "pasir gudang": "Johor", "johor bahru": "Johor", "batu pahat": "Johor",
    "ipoh": "Perak", "kuantan": "Pahang", "kota kinabalu": "Sabah",
    "kuching": "Sarawak", "seremban": "Negeri Sembilan", "alor setar": "Kedah",
    "kota bharu": "Kelantan", "kangar": "Perlis",
}


def _resolve_place(text):
    low = str(text or "").strip().lower()
    if not low:
        return None
    if low in STATE_CANON:
        return STATE_CANON[low]
    if low in CITY_TO_STATE:
        return CITY_TO_STATE[low]
    for city, state in CITY_TO_STATE.items():
        if city in low:
            return state
    for state in STATES:
        if state.lower() in low:
            return STATE_CANON.get(state.lower(), state)
    return None


def extract_state(directory_location, address):
    resolved = _resolve_place(directory_location)
    if resolved:
        return resolved
    resolved = _resolve_place(address)
    if resolved:
        return resolved
    if isinstance(directory_location, str) and directory_location.strip():
        loc = directory_location.strip()
        return loc.title() if loc.isupper() else loc
    return "Not Publicly Available"


def demand_bucket(workers_requested, industry_weight):
    try:
        wr = float(workers_requested)
        if wr >= 200:
            return "Very High"
        if wr >= 80:
            return "High"
        if wr >= 20:
            return "Medium"
        if wr > 0:
            return "Low"
    except (TypeError, ValueError):
        pass
    # No FWCMS record -> infer from category labour-intensity only, conservative
    if industry_weight >= 10:
        return "Medium"
    return "Low"


def clean_email(raw):
    if not isinstance(raw, str) or not raw.strip():
        return "Not Publicly Available"
    first = re.split(r"[;,]", raw)[0].strip()
    first = first.replace("%20", "")
    return first if "@" in first else "Not Publicly Available"


def clean_phone(website_phone, directory_phone):
    for val in (website_phone, directory_phone):
        if isinstance(val, str) and val.strip():
            first = re.split(r"[;,]", val)[0].strip()
            if first:
                return first
    return "Not Publicly Available"


def clean_url(val):
    if isinstance(val, str) and val.strip():
        v = val.strip()
        if not v.startswith("http"):
            v = "http://" + v
        return v
    return "Not Publicly Available"


def na(val):
    if val is None:
        return "Not Publicly Available"
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return "Not Publicly Available"
    return s


def main():
    df = pd.read_csv(IN_CSV)
    rows = []
    for _, r in df.iterrows():
        state = extract_state(r.get("Directory Location"), r.get("Address"))
        website = clean_url(r.get("Website URL"))
        email = clean_email(r.get("Email"))
        phone = clean_phone(r.get("Phone (from website)"), r.get("Directory Phone"))
        linkedin = na(r.get("LinkedIn"))
        address = na(r.get("Address")) if na(r.get("Address")) != "Not Publicly Available" else na(r.get("Directory Location"))
        industry_weight = {
            "Electronics": 10, "Semiconductor": 12, "Food Manufacturing": 9,
            "Furniture": 8, "Plastic Manufacturing": 9, "Rubber": 10, "Logistics": 7,
            "Automotive": 9, "Metal Fabrication": 8, "Construction Materials": 7,
            "General Manufacturing": 6,
        }.get(r["industry_bucket"], 6)

        demand = demand_bucket(r.get("workers_requested"), industry_weight)

        rows.append({
            "Company Name": r["Company Name"].strip(),
            "Industry": r["industry_bucket"],
            "Directory Category (raw)": na(r.get("Directory Category")),
            "State": state,
            "Headquarters Address": address,
            "Website": website,
            "General Company Email": email,
            "HR Email": "Not Publicly Available",
            "Recruitment Email": "Not Publicly Available",
            "Main Phone Number": phone,
            "Company LinkedIn URL": linkedin,
            "Careers Page URL": "Not Publicly Available",
            "Estimated Employees": "Not Publicly Available",
            "Estimated Foreign Worker Demand": demand,
            "Workers Requested (FWCMS, if on record)": r.get("workers_requested") if pd.notna(r.get("workers_requested")) else "",
            "Hiring Status": "Not Publicly Available",
            "Recent Expansion": "Not Publicly Available",
            "raw_score": r["score"],
        })

    out = pd.DataFrame(rows)
    # Prospect score 1-100 rescale of composite raw_score
    mn, mx = out["raw_score"].min(), out["raw_score"].max()
    out["Prospect Score"] = out["raw_score"].apply(
        lambda s: int(round(1 + 99 * (s - mn) / (mx - mn))) if mx > mn else 50
    )
    out = out.drop(columns=["raw_score"]).sort_values("Prospect Score", ascending=False).reset_index(drop=True)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(out)} rows -> {OUT_CSV}")
    print(out["State"].value_counts(dropna=False).head(20))
    print(out["Estimated Foreign Worker Demand"].value_counts())


if __name__ == "__main__":
    main()
