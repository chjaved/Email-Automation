"""
Step 3: Generate the grounded narrative fields for every company using OpenAI,
strictly constrained to the verified facts we already have (industry, state,
raw directory category, FWCMS workers-requested signal if present, contact
completeness). The model is explicitly told NOT to invent unverifiable facts
(expansion news, hiring status, employee counts, HR/recruitment emails) --
those stay "Not Publicly Available" and are only filled in later for
companies that pass through live web verification.

Fields generated here:
- Suggested Decision Maker
- Why This Company Is A Good Prospect (2-4 sentences)
- Personalization Notes (3-5 bullets)
- First Outreach Email (<=180 words, no spam words)
"""
import json
import os
import re
import sys
import time

import pandas as pd
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import OPENAI_API_KEY, OPENAI_MODEL

IN_CSV = os.environ.get(
    "NARRATIVE_IN_CSV",
    r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\base_100.csv",
)
OUT_CSV = os.environ.get(
    "NARRATIVE_OUT_CSV",
    r"c:\Users\user\Downloads\EMAIL AUTOMATION ONLINE JOBS\prospect_research\enriched_100.csv",
)

client = OpenAI(api_key=OPENAI_API_KEY)

SPAM_WORDS = ["free", "guaranteed", "urgent", "limited time", "click here", "act now"]

SYSTEM_PROMPT = (
    "You are a B2B research and copywriting assistant for AP Online Jobs, a licensed "
    "Malaysian recruitment agency (18+ years, ~80% foreign-worker approval success rate, "
    "assists employers with the 60K foreign worker approval process, full recruitment/"
    "documentation/compliance support, and a soon-launching AI-powered multilingual "
    "interview platform). You will be given verified facts about ONE Malaysian company. "
    "You must ONLY use the facts provided. Do NOT invent expansions, hiring status, "
    "employee counts, executive names, or any fact not given to you. Where something is "
    "not given, do not mention it or guess at it."
)


def build_user_prompt(row):
    workers = row.get("Workers Requested (FWCMS, if on record)")
    workers_line = (
        f"- Foreign worker demand on record (FWCMS government data): {workers} workers requested\n"
        if pd.notna(workers) and str(workers).strip() not in ("", "nan") else ""
    )
    return f"""Verified facts about the company:
- Company name: {row['Company Name']}
- Industry / sector: {row['Industry']}
- Raw directory category: {row['Directory Category (raw)']}
- State: {row['State']}
- Estimated foreign worker demand tier (derived from sector labour-intensity{"/government FWCMS data" if workers_line else ""}): {row['Estimated Foreign Worker Demand']}
{workers_line}- Has a public website: {"Yes" if row['Website'] != 'Not Publicly Available' else "No"}
- Has a public contact email: {"Yes" if row['General Company Email'] != 'Not Publicly Available' else "No"}

Return ONLY valid JSON with these exact keys:
{{
  "decision_maker": "one specific job title from: HR Manager, HR Director, Human Resource Department, Factory Manager, Operations Manager, Managing Director, General Manager, Plant Manager -- pick the single best fit for this industry",
  "why_good_prospect": "2-4 sentences explaining why this company is a good prospect for foreign worker recruitment services, based only on the facts given (industry labour intensity, sector, worker-demand tier)",
  "personalization_notes": ["3 to 5 short bullet points usable inside a cold email, based only on the facts given, e.g. industry traits, labour intensity, export orientation typical of the sector -- do not invent company-specific news"],
  "outreach_email": "A personalized cold email, maximum 180 words, professional tone, that mentions: their industry, possible manpower challenges typical of that industry, AP Online Jobs, foreign worker recruitment, the 60K approval assistance, the AI interview system, and an invitation for a discussion. Do not use these words: free, guaranteed, urgent, limited time, click here, act now. Do not invent specific company facts (no fake expansion/hiring claims). Address the company by name once. Sign off as 'AP Online Jobs Team'."
}}"""


def clean_spam(text):
    for w in SPAM_WORDS:
        text = re.sub(re.escape(w), "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()


def enforce_word_limit(text, limit=180):
    words = text.split()
    if len(words) <= limit:
        return text
    truncated = " ".join(words[:limit])
    last_period = truncated.rfind(".")
    if last_period > len(truncated) * 0.6:
        truncated = truncated[: last_period + 1]
    return truncated


def generate_for_row(row):
    prompt = build_user_prompt(row)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.6,
        response_format={"type": "json_object"},
        max_tokens=700,
    )
    content = resp.choices[0].message.content or "{}"
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.DOTALL)
    data = json.loads(cleaned)

    email = clean_spam(data.get("outreach_email", ""))
    email = enforce_word_limit(email, 180)

    bullets = data.get("personalization_notes", [])
    if isinstance(bullets, list):
        bullets_str = "; ".join(str(b).strip() for b in bullets if str(b).strip())
    else:
        bullets_str = str(bullets)

    return {
        "Suggested Decision Maker": data.get("decision_maker", "Not Publicly Available"),
        "Why This Company Is A Good Prospect": clean_spam(data.get("why_good_prospect", "")),
        "Personalization Notes": bullets_str,
        "First Outreach Email": email,
    }


def main():
    df = pd.read_csv(IN_CSV)
    results = []
    total = len(df)
    for i, row in df.iterrows():
        for attempt in range(3):
            try:
                gen = generate_for_row(row)
                break
            except Exception as e:
                print(f"[{i+1}/{total}] {row['Company Name']}: error {e}, retry {attempt+1}")
                time.sleep(2 * (attempt + 1))
        else:
            gen = {
                "Suggested Decision Maker": "Human Resource Department",
                "Why This Company Is A Good Prospect": "Not Publicly Available",
                "Personalization Notes": "Not Publicly Available",
                "First Outreach Email": "Not Publicly Available",
            }
        results.append(gen)
        print(f"[{i+1}/{total}] Generated: {row['Company Name']}")

    gen_df = pd.DataFrame(results)
    out = pd.concat([df.reset_index(drop=True), gen_df], axis=1)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(out)} rows -> {OUT_CSV}")


if __name__ == "__main__":
    main()
