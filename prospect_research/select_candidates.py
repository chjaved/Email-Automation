"""
Step 1: Select Top 100 candidate companies from Companies_Data_Master_Tiered.xlsx
Grounded in real scraped directory data (Master sheet) + FWCMS foreign-worker
request signals (Whale Accounts / Tier A-D sheets). No fabricated data.

Output: prospect_research/candidates_100.csv
"""
import re
import pandas as pd

SRC = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\Companies_Data_Master_Tiered.xlsx"
OUT_CSV = r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\candidates_100.csv"

TARGET_INDUSTRY_MAP = {
    "Furniture": ["furniture", "wood products"],
    "Food Manufacturing": ["prepare foods", "food & beverages", "beverage", "food products supplier"],
    "Plastic Manufacturing": ["plastic products"],
    "Rubber": ["rubber products", "rubber products supplier", "gloves"],
    "Electronics": [
        "electrical & electronic", "consumer & industrial electrica",
        "electronics manufacturer", "electronic parts supplier", "electronics company",
        "electrical & electronic parts a",
    ],
    "Automotive": ["automotive, parts & components", "transport equipment"],
    "Metal Fabrication": [
        "iron and steel", "steel fabricator", "steel distributor",
        "metal construction company", "steelwork design service", "scrap metal dealer",
    ],
    "Construction Materials": ["building & construction materia"],
    "Logistics": ["logistics service", "transportation service", "warehouse"],
    "General Manufacturing": ["manufacturer", "machinery & equipment"],
}

CATEGORY_WEIGHT = {
    "Electronics": 10, "Semiconductor": 12, "Food Manufacturing": 9,
    "Furniture": 8, "Plastic Manufacturing": 9, "Rubber": 10, "Logistics": 7,
    "Automotive": 9, "Metal Fabrication": 8, "Construction Materials": 7,
    "General Manufacturing": 6,
}

def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()

def base_name(s):
    """Strip branch/location suffixes and legal-form/punctuation noise so that
    branches or near-duplicate listings of the same company dedupe together."""
    s = str(s or "")
    s = re.split(r"[•\u2022]", s)[0]
    s = re.split(r",\s*(?:melaka|shah alam|penang|johor|selangor|kl|kuala lumpur|sales|central warehouse|branch)", s, flags=re.IGNORECASE)[0]
    s = re.sub(r"\([^)]*\)", "", s)  # drop parenthetical branch notes e.g. (Jalan Nikel)
    s = norm(s)
    s = re.sub(r"\bAND\b", "&", s)
    s = re.sub(r"\bSDN\.?\s*BHD\.?\b", "", s)
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

EXCLUDE_KEYWORDS = ["farm", "farming", "plantation", "dairy", "agricultur"]

def classify(row):
    cat = str(row.get("Directory Category", "") or "").lower()
    about = str(row.get("About", "") or "").lower()
    name = str(row.get("Company Name", "") or "").lower()
    blob = cat + " " + about + " " + name
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return None
    if "semiconductor" in blob:
        return "Semiconductor"
    for industry, keywords in TARGET_INDUSTRY_MAP.items():
        for kw in keywords:
            if kw in cat:
                return industry
    return None

def main():
    xl = pd.ExcelFile(SRC)
    master = xl.parse("Master")
    whale = xl.parse("Whale Accounts")
    tier_a = xl.parse("Tier A - Hot Leads")
    tier_b = xl.parse("Tier B - Priority")
    suppression = xl.parse("Suppression List - Do Not Contact")

    suppressed_names = set(norm(n) for n in suppression["Company Name"].dropna())

    # Build signal lookups
    whale_map = {}
    for _, r in whale.iterrows():
        whale_map[norm(r["Company Name"])] = {
            "workers_requested": r.get("Workers Requested"),
            "size_band": r.get("Size Band"),
            "tier": r.get("Current Tier"),
        }

    tierA_map = {norm(r["Company Name"]): r.get("Priority Score") for _, r in tier_a.iterrows()}
    tierB_map = {norm(r["Company Name"]): r.get("Priority Score") for _, r in tier_b.iterrows()}

    master = master.dropna(subset=["Company Name"]).copy()
    master["name_norm"] = master["Company Name"].map(norm)
    master["base_name"] = master["Company Name"].map(base_name)
    master = master[~master["name_norm"].isin(suppressed_names)]
    master = master[~master["base_name"].isin(suppressed_names)]

    master["industry_bucket"] = master.apply(classify, axis=1)
    candidates = master[master["industry_bucket"].notna()].copy()

    def score(row):
        s = CATEGORY_WEIGHT.get(row["industry_bucket"], 5)
        nm = row["name_norm"]
        if nm in whale_map:
            wr = whale_map[nm]["workers_requested"]
            try:
                s += min(float(wr) / 20.0, 30)
            except (TypeError, ValueError):
                s += 15
        if nm in tierA_map:
            try:
                s += float(tierA_map[nm]) / 5.0
            except (TypeError, ValueError):
                s += 8
        elif nm in tierB_map:
            try:
                s += float(tierB_map[nm]) / 10.0
            except (TypeError, ValueError):
                s += 4
        has_website = bool(str(row.get("Website URL", "") or "").strip())
        has_email = bool(str(row.get("Email", "") or "").strip())
        has_phone = bool(str(row.get("Phone (from website)", "") or row.get("Directory Phone", "") or "").strip())
        s += (2 if has_website else 0) + (2 if has_email else 0) + (1 if has_phone else 0)
        return s

    candidates["score"] = candidates.apply(score, axis=1)
    candidates = candidates.sort_values("score", ascending=False)
    candidates = candidates.drop_duplicates(subset=["name_norm"], keep="first")
    candidates = candidates.drop_duplicates(subset=["base_name"], keep="first")

    print("Bucket availability (pre-cap):")
    print(candidates["industry_bucket"].value_counts())

    # Enforce diversity: guarantee a minimum slate per requested industry bucket
    # (so every priority sector in the brief is represented), then fill the rest
    # of the 100 slots by best score overall, capped per bucket.
    BUCKET_MIN = 6
    BUCKET_CAP = 16
    all_buckets = list(TARGET_INDUSTRY_MAP.keys()) + ["Semiconductor"]

    guaranteed_frames = []
    for bucket in all_buckets:
        group = candidates[candidates["industry_bucket"] == bucket]
        guaranteed_frames.append(group.sort_values("score", ascending=False).head(BUCKET_MIN))
    guaranteed = pd.concat(guaranteed_frames).drop_duplicates(subset=["name_norm"])

    remaining_slots = 100 - len(guaranteed)
    remaining_pool = candidates[~candidates["name_norm"].isin(guaranteed["name_norm"])].copy()

    # Respect per-bucket cap while filling remaining slots by score
    bucket_counts = guaranteed["industry_bucket"].value_counts().to_dict()
    fill_rows = []
    for _, row in remaining_pool.sort_values("score", ascending=False).iterrows():
        if remaining_slots <= 0:
            break
        b = row["industry_bucket"]
        if bucket_counts.get(b, 0) >= BUCKET_CAP:
            continue
        fill_rows.append(row)
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
        remaining_slots -= 1

    fill_df = pd.DataFrame(fill_rows) if fill_rows else pd.DataFrame(columns=candidates.columns)
    diversified = pd.concat([guaranteed, fill_df], ignore_index=True)

    top100 = diversified.sort_values("score", ascending=False).head(100).copy()

    def whale_info(nm):
        return whale_map.get(nm, {})

    top100["workers_requested"] = top100["name_norm"].map(lambda n: whale_info(n).get("workers_requested"))
    top100["fwcms_tier"] = top100["name_norm"].map(lambda n: whale_info(n).get("tier"))
    top100["priority_score_A"] = top100["name_norm"].map(lambda n: tierA_map.get(n))
    top100["priority_score_B"] = top100["name_norm"].map(lambda n: tierB_map.get(n))

    keep_cols = [
        "Company Name", "industry_bucket", "Directory Category", "Directory Location",
        "Address", "Website URL", "Email", "Phone (from website)", "Directory Phone",
        "LinkedIn", "About", "score", "workers_requested", "fwcms_tier",
        "priority_score_A", "priority_score_B",
    ]
    top100[keep_cols].to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(top100)} candidates to {OUT_CSV}")
    print(top100["industry_bucket"].value_counts())

if __name__ == "__main__":
    main()
