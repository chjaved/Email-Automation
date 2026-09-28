"""Lead enrichment from website and social links."""
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_MINI_MODEL,
    SCRAPE_DELAY_SECONDS,
    SCRAPE_TIMEOUT_SECONDS,
    USER_AGENTS,
)
from db import get_conn

logger = logging.getLogger(__name__)

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


@dataclass
class ScrapedData:
    url: str
    ok: bool
    title: str = ""
    meta_description: str = ""
    text: str = ""
    links: List[str] = None  # type: ignore[assignment]
    status_code: int = 0
    error: str = ""

    def __post_init__(self):
        if self.links is None:
            self.links = []


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\x00-\x7F]+", " ", text)  # strip emojis/non-ascii for LLM cost
    return text.strip()


def _respect_robots(base_url: str, target: str) -> bool:
    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser(robots_url)
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch("*", target)
    except Exception as e:
        logger.debug("robots.txt check failed for %s: %s", target, e)
        return True


def _pick_agent() -> Dict[str, str]:
    return {"User-Agent": random.choice(USER_AGENTS), "Accept": "text/html,application/xhtml+xml"}


def fetch_page(url: str) -> ScrapedData:
    if not url or not url.startswith("http"):
        return ScrapedData(url=url, ok=False, error="invalid url")

    try:
        if not _respect_robots(url, url):
            return ScrapedData(url=url, ok=False, error="blocked by robots.txt")

        resp = requests.get(
            url,
            headers=_pick_agent(),
            timeout=SCRAPE_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return ScrapedData(url=url, ok=False, error=str(e), status_code=getattr(e.response, "status_code", 0) if hasattr(e, "response") else 0)

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""
        meta = soup.find("meta", attrs={"name": "description"})
        meta_description = meta.get("content", "") if meta else ""
        text = _clean_text(soup.get_text(separator=" ", strip=True))
        links = [a.get("href", "") for a in soup.find_all("a", href=True)]
        return ScrapedData(
            url=url,
            ok=True,
            title=title,
            meta_description=meta_description,
            text=text,
            links=links,
            status_code=resp.status_code,
        )
    except Exception as e:
        return ScrapedData(url=url, ok=False, error=str(e))


def discover_about_url(base: str, page: ScrapedData) -> Optional[str]:
    if not page.ok:
        return None
    for link in page.links:
        href = link.strip().lower()
        if any(x in href for x in ["/about", "/about-us", "/aboutus", "/who-we-are"]):
            return urljoin(base, link)
    # Fallback common paths
    for path in ["/about", "/about-us", "/about.php"]:
        candidate = urljoin(base, path)
        if candidate != base:
            return candidate
    return None


def _call_openai(prompt: str, model: str, json_mode: bool = False, temperature: float = 0.0) -> str:
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    messages = [
        {"role": "system", "content": "You are a helpful research assistant."},
        {"role": "user", "content": prompt},
    ]

    response_format = {"type": "json_object"} if json_mode else None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            response_format=response_format,  # type: ignore[arg-type]
            max_tokens=500,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("OpenAI call failed: %s", e)
        raise


def classify_industry(company_name: str, context: str) -> str:
    allowed = ["construction", "hotel", "restaurant", "manufacturing", "other"]
    prompt = (
        f"Classify the company '{company_name}' into exactly one of these industries: "
        f"{', '.join(allowed)}. Use only the provided context.\n\n"
        f"Context: {context[:4000]}\n\n"
        "Return only the single lower-case industry word."
    )
    result = _call_openai(prompt, OPENAI_MINI_MODEL, json_mode=False, temperature=0.0).strip().lower().rstrip(".")
    if result not in allowed:
        result = "other"
    return result


def generate_summary(company_name: str, context: str) -> str:
    prompt = (
        f"Write a concise 2-3 sentence company summary for '{company_name}' based only on the text below. "
        "Mention what the company does, the services or products it appears to offer, and any signals about company size or hiring needs.\n\n"
        f"Text: {context[:4000]}\n\n"
        "Return only the summary."
    )
    return _call_openai(prompt, OPENAI_MINI_MODEL, json_mode=False, temperature=0.3).strip()


def fallback_enrich(company_name: str) -> Dict[str, Any]:
    allowed = ["construction", "hotel", "restaurant", "manufacturing", "other"]
    prompt = (
        f"The company '{company_name}' has no website. "
        f"Infer its industry from exactly one of {', '.join(allowed)} and write a 2-3 sentence likely summary. "
        "Return JSON with keys 'industry' and 'summary'."
    )
    content = _call_openai(prompt, OPENAI_MINI_MODEL, json_mode=True, temperature=0.3)
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.DOTALL)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        data = {}

    industry = data.get("industry", "other").strip().lower()
    if industry not in allowed:
        industry = "other"
    summary = data.get("summary", f"{company_name} is a business that may benefit from workforce solutions.").strip()
    return {
        "industry": industry,
        "summary": summary,
        "source": "company_name_only",
    }


def extract_features(text: str) -> Dict[str, Any]:
    services: List[str] = []
    size_signals: List[str] = []
    hiring_mentions: List[str] = []

    lowered = text.lower()
    if any(w in lowered for w in ["construction", "contractor", "builder", "civil", "project"]):
        services.append("construction / building")
    if any(w in lowered for w in ["hotel", "resort", "accommodation", "rooms", "guest"]):
        services.append("hospitality / accommodation")
    if any(w in lowered for w in ["restaurant", "cafe", "food & beverage", "f&b", "dining"]):
        services.append("food & beverage")
    if any(w in lowered for w in ["manufacturing", "factory", "production", "assembly", "industrial"]):
        services.append("manufacturing / production")

    if any(w in lowered for w in ["hiring", "recruitment", "vacancy", "job", "career", "join us", "we are looking for"]):
        hiring_mentions.append("hiring signals detected")
    if any(w in lowered for w in ["staffing", "manpower", "workforce", "labour", "labor"]):
        hiring_mentions.append("staffing/manpower mention")

    if any(w in lowered for w in ["over 100", "more than 100", "100+", "large team", "500 employees", "hundreds of"]):
        size_signals.append("possible larger organisation")
    elif any(w in lowered for w in ["small team", "boutique", "family-run", "startup", "growing"]):
        size_signals.append("small / growing team")

    return {
        "services_mentioned": services,
        "size_signals": size_signals,
        "hiring_mentions": hiring_mentions,
    }


def enrich_lead(lead: sqlite3.Row) -> Dict[str, Any]:
    company_name = lead["company_name"] or ""
    website = lead["website"] or ""
    logger.info("Enriching %s at %s", company_name, website or "no website")

    if not website:
        data = fallback_enrich(company_name)
        return data

    # Fetch homepage
    home = fetch_page(website)
    time.sleep(SCRAPE_DELAY_SECONDS)

    about_url = discover_about_url(website, home) if home.ok else None
    about = ScrapedData(url=about_url or "", ok=False)
    if about_url:
        about = fetch_page(about_url)
        time.sleep(SCRAPE_DELAY_SECONDS)

    combined_text_parts: List[str] = []
    if home.ok:
        combined_text_parts.append(home.title)
        combined_text_parts.append(home.meta_description)
        combined_text_parts.append(home.text)
    if about.ok:
        combined_text_parts.append(about.title)
        combined_text_parts.append(about.meta_description)
        combined_text_parts.append(about.text)

    full_text = "\n".join(combined_text_parts)

    if not full_text.strip():
        logger.warning("No text fetched for %s, falling back to company name", company_name)
        data = fallback_enrich(company_name)
        data["website_errors"] = [home.error, about.error]
        return data

    features = extract_features(full_text)
    industry = classify_industry(company_name, full_text)
    summary = generate_summary(company_name, full_text)

    return {
        "industry": industry,
        "summary": summary,
        "company_description": (home.meta_description or home.title or about.meta_description or "")[:500],
        "services_mentioned": features.get("services_mentioned", []),
        "size_signals": features.get("size_signals", []),
        "hiring_mentions": features.get("hiring_mentions", []),
        "scraped_pages": [p for p in [home.url, about.url] if p],
        "source": "website_scraping",
    }


def run_enrichment() -> int:
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is not set. Skipping enrichment.")
        return 0

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE status = 'new' ORDER BY id")
    leads = cur.fetchall()
    conn.close()

    if not leads:
        logger.info("No new leads to enrich.")
        return 0

    total = len(leads)
    enriched = 0
    for i, lead in enumerate(leads, 1):
        try:
            data = enrich_lead(lead)
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE leads
                SET industry = COALESCE(NULLIF(?, ''), industry),
                    enriched_data = ?,
                    status = 'enriched'
                WHERE id = ?
                """,
                (
                    data.get("industry", lead["industry"] or "other"),
                    json.dumps(data, ensure_ascii=False),
                    lead["id"],
                ),
            )
            conn.commit()
            conn.close()
            enriched += 1
            print(f"Enriched {i}/{total}: {lead['company_name']}")
        except Exception as e:
            logger.error("Failed to enrich lead %s: %s", lead["company_name"], e)
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE leads SET status = 'enrichment_failed' WHERE id = ?",
                (lead["id"],),
            )
            conn.commit()
            conn.close()

    print(f"Enrichment complete: {enriched}/{total}")
    return enriched
