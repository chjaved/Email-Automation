"""Atlas Professional Bookkeeping email writing and caching.

The initial email follows the fixed, user-approved Atlas template verbatim.
The optional per-user `sample_email` path still lets the AI personalise a
custom template per company when one is configured on the dashboard.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_MINI_MODEL,
    OPENAI_MODEL,
    SIGNATURE_ADDRESS,
    SIGNATURE_COMPANY,
    SIGNATURE_CONFIDENTIALITY,
    SIGNATURE_EMAIL,
    SIGNATURE_NAME,
    SIGNATURE_PHONE,
    SIGNATURE_SERVICES,
    SIGNATURE_TITLE,
    SIGNATURE_WEBSITE,
)
from db import get_conn

logger = logging.getLogger(__name__)

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


SUBJECT_TEMPLATES = [
    "Bookkeeping Support for Your Business",
]

# Per-industry hints used both in the AI prompt and as safe fallbacks.
INDUSTRY_PROFILES: Dict[str, Dict[str, str]] = {
    "construction": {
        "workforce_categories": (
            "general workers, site labourers, steel fixers, bar benders, concreters, "
            "scaffolders, formwork carpenters and other project-based manpower"
        ),
        "operational_context": (
            "ongoing and upcoming construction projects where consistent site "
            "manpower is critical to keeping schedules, safety standards and "
            "handover timelines on track"
        ),
    },
    "cleaning": {
        "workforce_categories": (
            "cleaning staff, housekeeping, general cleaning, facility support, "
            "maintenance and material handling personnel"
        ),
        "operational_context": (
            "day-to-day cleaning and service operations across client sites and "
            "facilities where reliable manpower is essential for service quality "
            "and business continuity"
        ),
    },
    "hotel": {
        "workforce_categories": (
            "housekeeping attendants, room attendants, F&B service crew, "
            "stewarding, kitchen helpers and back-of-house support"
        ),
        "operational_context": (
            "hotel and hospitality operations where staffing stability directly "
            "affects guest experience, occupancy turnaround and service ratings"
        ),
    },
    "restaurant": {
        "workforce_categories": (
            "kitchen helpers, cooks, service crew, dishwashers, stewards and "
            "outlet support staff"
        ),
        "operational_context": (
            "restaurant and F&B operations where consistent kitchen and service "
            "staffing is key to daily throughput, service speed and customer "
            "experience"
        ),
    },
    "manufacturing": {
        "workforce_categories": (
            "production operators, packers, machine operators, QC helpers, "
            "warehouse and material handling staff"
        ),
        "operational_context": (
            "production and manufacturing operations where consistent line "
            "manpower is essential for output targets, shift coverage and "
            "on-time delivery"
        ),
    },
    "plantation": {
        "workforce_categories": (
            "harvesters, general estate workers, field workers and processing "
            "helpers"
        ),
        "operational_context": (
            "estate and plantation operations where reliable field manpower "
            "underpins harvest cycles and productivity"
        ),
    },
    "logistics": {
        "workforce_categories": (
            "warehouse assistants, pickers and packers, loaders, forklift "
            "helpers and general logistics support"
        ),
        "operational_context": (
            "warehousing and logistics operations where stable manpower is key "
            "to throughput, order fulfilment and dispatch timelines"
        ),
    },
    "other": {
        "workforce_categories": (
            "general workers, operational support staff and manpower-intensive "
            "service roles relevant to the business"
        ),
        "operational_context": (
            "day-to-day business operations where dependable manpower supports "
            "productivity, service quality and business continuity"
        ),
    },
}

FORBIDDEN_WORDS = {"guaranteed", "urgent"}


# ---------------------------------------------------------------------------
# State helpers (subject rotation)
# ---------------------------------------------------------------------------
def _load_enriched(lead: sqlite3.Row) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if lead["enriched_data"]:
        try:
            data = json.loads(lead["enriched_data"])
        except json.JSONDecodeError:
            pass
    return data


def _get_state(key: str, default: str = "0") -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def _set_state(key: str, value: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def _next_subject_index() -> int:
    key = "subject_angle_global"
    current = int(_get_state(key, "0"))
    idx = current % len(SUBJECT_TEMPLATES)
    _set_state(key, str((current + 1) % 1000))
    return idx


def _build_subject(company_name: str) -> str:
    idx = _next_subject_index()
    template = SUBJECT_TEMPLATES[idx]
    subject = template.format(company_name=company_name).strip()
    subject = " ".join(subject.split())  # remove any stray newlines/tabs
    words = subject.split()
    words = [w for w in words if w.lower().strip("?.!,;:") not in FORBIDDEN_WORDS]
    return " ".join(words)


# ---------------------------------------------------------------------------
# Industry helpers
# ---------------------------------------------------------------------------
def _normalise_industry(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return "other"
    # crude keyword matching so free-form values still map
    if any(k in key for k in ("construct", "builder", "contractor", "civil")):
        return "construction"
    if any(k in key for k in ("clean", "janitor", "facility")):
        return "cleaning"
    if any(k in key for k in ("hotel", "resort", "hospitality", "lodging")):
        return "hotel"
    if any(k in key for k in ("restaurant", "f&b", "cafe", "catering", "food")):
        return "restaurant"
    if any(k in key for k in ("manufactur", "factory", "industrial", "plant")):
        return "manufacturing"
    if any(k in key for k in ("plantation", "estate", "agri", "farm")):
        return "plantation"
    if any(k in key for k in ("logistic", "warehouse", "freight", "transport")):
        return "logistics"
    if key in INDUSTRY_PROFILES:
        return key
    return "other"


def _industry_profile(industry: str) -> Dict[str, str]:
    return INDUSTRY_PROFILES.get(industry, INDUSTRY_PROFILES["other"])


# ---------------------------------------------------------------------------
# AI touch: opening hook + industry value paragraph
# ---------------------------------------------------------------------------
def _ai_personalisation(
    company_name: str,
    industry: str,
    location: str,
    enriched: Dict[str, Any],
    ai_context: str = "",
) -> Dict[str, str]:
    profile = _industry_profile(industry)
    summary = enriched.get("summary", "")
    services = ", ".join(enriched.get("services_mentioned", []))

    fallback_opening = (
        f"We understand that {company_name} operates in the "
        f"{industry.replace('_', ' ')} sector, where accurate and timely financial records "
        f"are important to confident day-to-day decision-making."
    )
    fallback_value = (
        f"Atlas Professional Bookkeeping can support {company_name} with reliable bookkeeping, "
        f"VAT returns, payroll, bank reconciliation and invoice processing, helping reduce "
        f"administrative pressure and keep its accounts organised."
    )

    if client is None:
        return {"opening": fallback_opening, "value": fallback_value}

    account_brief = ""
    if ai_context and ai_context.strip():
        account_brief = (
            "SENDER ACCOUNT BRIEF (this is who is sending the email — study "
            "this carefully and let it shape the tone, value proposition and "
            "any references you make; NEVER invent facts beyond it):\n"
            f"{ai_context.strip()}\n\n"
        )

    prompt = (
        "You are writing two short paragraphs that will be inserted into a "
        "formal B2B email sent to a company.\n\n"
        f"{account_brief}"
        f"Recipient company: {company_name}\n"
        f"Normalised industry: {industry}\n"
        f"Location: {location}\n"
        f"Website summary (may be empty): {summary}\n"
        f"Services mentioned (may be empty): {services}\n"
        "Sender: Atlas Professional Bookkeeping, Ireland.\n"
        "Services: bookkeeping, VAT returns, payroll, bank reconciliation and invoice processing.\n\n"
        "Write:\n"
        "1. `opening` - 1 to 2 sentences acknowledging what the company does and a plausible "
        "financial administration challenge in its industry. Reference the company by name. "
        "Do not assume facts unsupported by the inputs.\n"
        "2. `value` - 2 to 3 sentences explaining how professional bookkeeping can save time, "
        "improve record accuracy and support compliance. Mention only relevant Atlas services. "
        "Do not mention pricing or include a call-to-action.\n\n"
        "Constraints:\n"
        "- Professional Irish/British English.\n"
        "- No emojis, no bullet points, no markdown.\n"
        "- No words like 'guaranteed' or 'urgent'.\n"
        "- Do not include a greeting, signature, or closing.\n\n"
        "Return ONLY valid JSON of the form: "
        "{\"opening\": \"...\", \"value\": \"...\"}"
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a professional B2B copywriter."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
            max_tokens=500,
        )
        content = resp.choices[0].message.content or ""
        parsed = json.loads(content)
        opening = str(parsed.get("opening", "")).strip() or fallback_opening
        value = str(parsed.get("value", "")).strip() or fallback_value
        return {"opening": opening, "value": value}
    except Exception as e:
        logger.warning("AI personalisation failed, using fallback: %s", e)
        return {"opening": fallback_opening, "value": fallback_value}


# ---------------------------------------------------------------------------
# Body assembly
# ---------------------------------------------------------------------------
UNSUBSCRIBE = "Reply 'remove' if this isn't relevant and we won't email you again."

# Approved initial outreach template (sent verbatim to every lead).
# {opening} is replaced by a 1-2 line company-specific hook built from the
# lead's scraped website summary (empty when no summary is available).
INITIAL_BODY = (
    "Hi,\n\n"
    "I hope you're doing well.\n"
    "{opening}"
    "My name is Stephen Darby from Atlas Professional Bookkeeping. I support sole traders and "
    "small to medium-sized businesses across Ireland with reliable and compliant bookkeeping - "
    "so they can focus on running their business without the stress.\n\n"
    "Many business owners I speak with mention similar challenges:\n"
    "• Uncertainty around what needs to be tracked\n"
    "• VAT and PAYE feeling complex or overwhelming\n"
    "• Bookkeeping taking time away from day to day work.\n\n"
    "That's where I can help.\n"
    "At Atlas Professional Bookkeeping, I provide:\n"
    "✔️ Accurate bookkeeping & bank reconciliations\n"
    "✔️ VAT & PAYE preparation and filing\n"
    "✔️ Payroll support\n"
    "✔️ Friendly, dependable service - fully remotely across Ireland\n\n"
    "I offer affordable monthly packages starting from €95. All packages include my full range "
    "of services with pricing based solely on transaction volume and employee numbers. If you'd "
    "like further details, feel free to reply to this email and I'll happily outline the options "
    "available - with no obligation.\n\n"
    "You can also find further details about my services at www.atlasprobookkeeping.ie\n\n"
    "Wishing you every success with your business"
)


def _signature_lines(sig: Dict[str, str]) -> str:
    """Format the shared Atlas signature block from a signature dict."""
    website_display = re.sub(r"^https?://", "", sig["website"]).rstrip("/")
    wordmark = "\n".join(sig["company"].upper().split())
    parts = [
        "Kind Regards,",
        "",
        wordmark,
        "",
        sig["name"],
        sig["company"],
        "",
        f"E {sig['email']}",
        f"W {website_display}",
        "L LinkedIn Profile",
        f"A {SIGNATURE_ADDRESS}",
        "",
        SIGNATURE_SERVICES,
        "",
        SIGNATURE_CONFIDENTIALITY,
    ]
    return "\n".join(parts)


def _build_signature(user_id: int) -> str:
    """Build email signature from per-user settings, falling back to config."""
    try:
        from settings import get_signature
        sig = get_signature(user_id)
    except Exception:
        sig = {
            "name": SIGNATURE_NAME,
            "title": SIGNATURE_TITLE,
            "company": SIGNATURE_COMPANY,
            "email": SIGNATURE_EMAIL,
            "phone": SIGNATURE_PHONE,
            "website": SIGNATURE_WEBSITE,
        }
    return _signature_lines(sig)


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(company\s+limited\s+by\s+(shares|guarantee)|limited|ltd\.?|plc|dac|clg|ulc|llp|llc|inc\.?|pte\.?\s*ltd\.?|sdn\.?\s*bhd\.?)\b\.?",
    re.IGNORECASE,
)


def _display_name(company_name: str) -> str:
    """'EVERLEIGH EQUESTRIAN LIMITED' -> 'Everleigh Equestrian' for use
    inside email prose (legal suffixes read awkwardly mid-sentence)."""
    name = _LEGAL_SUFFIX_RE.sub("", company_name or "").strip(" -,.")
    if not name:
        return company_name or ""
    # Title-case all-caps names; leave mixed-case names alone.
    if name.isupper():
        name = name.title()
    return name


def _opening_hook(company_name: str, location: str, enriched: Dict[str, Any]) -> str:
    """Build a 1-2 line personalised opening from the lead's scraped website
    summary. Returns '' when there's nothing reliable to reference."""
    summary = (enriched.get("summary") or enriched.get("company_description") or "").strip()
    if not summary:
        return ""

    display = _display_name(company_name)
    if client is None:
        return (
            f"I came across {display} and noticed the work you do - "
            "it looks like a busy operation to keep on top of.\n"
        )

    prompt = (
        "You are writing the opening line(s) of a cold B2B email from an Irish "
        "bookkeeper (Stephen Darby, Atlas Professional Bookkeeping) to a company.\n\n"
        f"Recipient company: {display}\n"
        f"Location: {location or 'Ireland'}\n"
        f"What the company does (from their website): {summary[:600]}\n\n"
        "Write 1-2 short sentences (max ~35 words total) that go right after "
        "'I hope you're doing well.' - acknowledge what the company actually "
        "does and hint that their kind of business has real bookkeeping/VAT "
        "admin to stay on top of. Sound natural and specific, not flattering "
        "or salesy. Reference the company by name. No greeting, no questions, "
        "no emojis, no pricing. Irish/British English.\n\n"
        "Return ONLY the sentence(s), nothing else."
    )
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MINI_MODEL,
            messages=[
                {"role": "system", "content": "You write short, natural opening lines for B2B emails."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=120,
        )
        opening = (resp.choices[0].message.content or "").strip().strip('"')
        if not opening:
            return ""
        return opening + "\n"
    except Exception as e:
        logger.warning("Opening hook generation failed for %s: %s", company_name, e)
        return ""


def _assemble_body(company_name: str, user_id: int = 0, opening: str = "") -> str:
    body = (
        f"{INITIAL_BODY.format(opening=opening)}\n\n"
        f"{_build_signature(user_id)}\n\n"
        f"---\n{UNSUBSCRIBE}"
    )

    # Strip any accidental forbidden words
    body = re.sub(
        r"\b(" + "|".join(FORBIDDEN_WORDS) + r")\b",
        "",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


# ---------------------------------------------------------------------------
# Sample-based generation (user provides exact template, AI tweaks per company)
# ---------------------------------------------------------------------------
def _generate_from_sample(
    lead: sqlite3.Row,
    sample_email: str,
    instructions: str,
    ai_context: str,
) -> Dict[str, str]:
    """Generate an email by taking the user's sample template and asking the
    AI to make only minimal per-company adjustments. Falls back to the sample
    as-is (with company name substituted) if the OpenAI call fails."""
    company_name = (lead["company_name"] or "your organisation").strip()
    industry = lead["industry"] or "other"
    location = (lead["location"] or "").strip()

    fallback_body = sample_email.replace("[Company]", company_name).replace("{company_name}", company_name)
    fallback_subject = f"Re: {company_name}"

    if client is None:
        return {"subject": fallback_subject, "body": fallback_body}

    instr_block = ""
    if instructions.strip():
        instr_block = f"\nADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n"

    context_block = ""
    if ai_context.strip():
        context_block = f"\nSENDER ACCOUNT BRIEF:\n{ai_context.strip()}\n"

    prompt = (
        "You are personalising an outbound B2B email for a specific recipient company.\n"
        "Below is the EXACT email template from the sender. Your job is to personalise\n"
        "it for the recipient company while keeping the core message intact.\n\n"
        f"Recipient company: {company_name}\n"
        f"Industry: {industry}\n"
        f"Location: {location}\n"
        f"{context_block}"
        f"{instr_block}"
        "RULES:\n"
        "- Keep the overall structure, tone, and core content of the sample.\n"
        "- Replace any [Company] or {company_name} placeholders with the recipient company name.\n"
        "- IMPORTANT: Add 2-3 personalised lines near the opening that specifically reference\n"
        f"  the recipient's industry ({industry}) and how the offering is relevant to them.\n"
        "  For example, mention industry-specific challenges, use cases, or benefits.\n"
        "  These lines should feel natural and tailored, not generic.\n"
        "- In the closing paragraph, mention the recipient company by name when inviting\n"
        "  them to a demo or discussion.\n"
        "- Do NOT invent facts about the company that aren't in the sample or brief.\n"
        "- Do NOT add emojis, markdown, or bullet points unless they're in the sample.\n"
        "- If the instructions say to change something, change ONLY that.\n"
        "- If the instructions say to NOT touch something, leave it EXACTLY as-is.\n"
        "- Return ONLY the final email body text (no subject line, no commentary).\n\n"
        f"SAMPLE EMAIL TEMPLATE:\n---\n{sample_email.strip()}\n---\n"
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a professional B2B email writer. You follow templates closely but add genuine personalisation for each recipient company based on their industry and niche."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=1600,
        )
        body = (resp.choices[0].message.content or "").strip()
        if not body:
            body = fallback_body

        # Also generate a subject line — check instructions for TITLE: prefix
        subject = ""
        if instructions.strip():
            for line in instructions.strip().split("\n"):
                line = line.strip()
                if line.upper().startswith("TITLE:"):
                    subject = line[6:].strip()
                    # Replace placeholders
                    subject = subject.replace("[Company]", company_name).replace("{company_name}", company_name)
                    # If no placeholder was replaced, append company name
                    if company_name.lower() not in subject.lower():
                        subject = f"{subject} – {company_name}"
                    subject = " ".join(subject.split())
                    break
        if not subject:
            subject_prompt = (
                f"Based on this email, write a concise professional subject line (max 80 chars).\n"
                f"Company: {company_name}\n"
                f"Industry: {industry}\n"
            )
            if instructions.strip():
                subject_prompt += f"Instructions: {instructions.strip()[:500]}\n"
            subject_prompt += (
                f"Return ONLY the subject line text, nothing else.\n\n"
                f"Email:\n{body[:800]}"
            )
            subject_resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You write concise B2B email subject lines."},
                    {"role": "user", "content": subject_prompt},
                ],
                temperature=0.4,
                max_tokens=100,
            )
            subject = (subject_resp.choices[0].message.content or "").strip().strip('"').strip("'")
        if not subject:
            subject = fallback_subject

        subject = " ".join(subject.split())  # strip newlines/extra whitespace
        return {"subject": subject, "body": body}
    except Exception as e:
        logger.warning("Sample-based generation failed, using template as-is: %s", e)
        return {"subject": fallback_subject, "body": fallback_body}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _get_or_generate(lead: sqlite3.Row) -> Dict[str, str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM emails WHERE lead_id = ?", (lead["id"],))
    cached = cur.fetchone()
    conn.close()
    if cached:
        return {"subject": cached["subject"], "body": cached["body"]}

    enriched = _load_enriched(lead)
    raw_industry = lead["industry"] or enriched.get("industry", "other") or "other"
    industry = _normalise_industry(raw_industry)
    company_name = (lead["company_name"] or "your organisation").strip()
    location = (lead["location"] or "").strip()

    subject = _build_subject(company_name)

    ai_context = ""
    sample_email = ""
    email_instructions = ""
    try:
        lead_user_id = lead["user_id"]
    except (KeyError, IndexError):
        lead_user_id = None
    if lead_user_id:
        try:
            from settings import get_ai_context, get_sample_email, get_email_instructions
            ai_context = get_ai_context(lead_user_id)
            sample_email = get_sample_email(lead_user_id)
            email_instructions = get_email_instructions(lead_user_id)
        except Exception:
            pass

    # If the user provided a sample email template, use that path (AI makes
    # only minimal per-company tweaks per the instructions).
    if sample_email and sample_email.strip():
        result = _generate_from_sample(lead, sample_email, email_instructions, ai_context)
        subject = result["subject"]
        body = result["body"]
    else:
        subject = _build_subject(company_name)
        opening = _opening_hook(company_name, location, enriched)
        body = _assemble_body(company_name, lead_user_id or 0, opening)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO emails (lead_id, subject, body, generated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lead_id) DO UPDATE SET subject=excluded.subject, body=excluded.body, generated_at=excluded.generated_at
        """,
        (
            lead["id"],
            subject,
            body,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    return {"subject": subject, "body": body}


def generate_for_lead(lead: sqlite3.Row) -> Dict[str, str]:
    return _get_or_generate(lead)


def preview_emails(n: int = 10) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM leads WHERE status = 'enriched' AND email IS NOT NULL ORDER BY RANDOM() LIMIT ?",
        (n,),
    )
    leads = cur.fetchall()
    conn.close()

    if not leads:
        print("No enriched leads found. Run `python main.py enrich` first.")
        return

    for lead in leads:
        email = generate_for_lead(lead)
        print("=" * 60)
        print(f"To: {lead['email']}")
        print(f"Subject: {email['subject']}")
        print("-" * 60)
        print(email["body"])
        print("\n")
