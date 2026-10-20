"""
company_scraper.py  —  Direct careers-page scraper + weekly auto-discovery
===========================================================================
What it does:
  1. Loads companies.json and reads each company's careers page for open roles.
     When a company's jobs run on a supported job board (Greenhouse, Lever,
     Ashby, Workable, Recruitee, Teamtailor), its official feed is used instead,
     which gives clean titles, locations and full descriptions. The board is
     found automatically, from the careers link or from links on the page, and
     remembered in companies.json as "ats".
  2. Returns the listings UNSCORED. job_agent.py runs them through exactly the
     same filters, two-step scoring and Notion columns as every other source.
  3. Once a week, asks Claude to discover ~10 new companies and appends them to
     companies.json (logged to company_discovery.log).

Usage (standalone test, prints what it finds):
    python company_scraper.py
"""

import datetime
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import anthropic
import requests
from bs4 import BeautifulSoup

import extra_sources

# ─── Config ──────────────────────────────────────────────────────────────────

COMPANIES_FILE = Path(__file__).parent / "companies.json"
DISCOVERY_LOG = Path(__file__).parent / "company_discovery.log"
DISCOVERY_STATE_FILE = Path(__file__).parent / "discovery_state.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_DISCOVERY = "claude-sonnet-5-5"

# Title keywords that flag a careers-page link as worth a closer look. Applied
# to the TITLE only: the company's own description says "sustainability" for
# every company on the list, so it can't tell one job from another.
QUICK_FILTER_KEYWORDS = [
    "sustainab", "esg", "csrd", "csr", "climate", "environment", "circular",
    "carbon", "emission", "green", "renewable", "energy transition", "decarboni",
    "reporting", "impact", "biodiversity", "net zero", "lca", "life cycle",
    "duurzaam", "milieu", "klimaat", "durabilit", "environnement", "rse",
    "intern", "trainee", "stage", "stagiair", "graduate", "junior",
]

# ─── Internship detection ────────────────────────────────────────────────────
# Shared with job_agent.py (imported from there) so every source gets tagged
# the same way.

_INTERNSHIP_TITLE_RE = re.compile(
    r"\bintern(?:ship)?\b|\btrainee(?:ship)?\b|\bstagiair\w*\b|\bstagiaire\b|"
    r"\bstage\b|\bafstudeerstage\b|\bwerkplekleren\b|\bwork\s*placement\b",
    re.IGNORECASE,
)
_INTERNSHIP_DESC_RE = re.compile(
    r"(?<!not )(?<!not an )(?<!no )(?<!isn't an )"
    r"\bintern(?:ship)?\b|\btrainee(?:ship)?\b|\bstagiair\w*\b|\bstagiaire\b",
    re.IGNORECASE,
)


def is_internship(job: dict) -> bool:
    """Heuristic: does this listing look like an internship/traineeship/stage?

    Checked in order: an explicit flag already set at scrape time, then the job
    title (broader pattern list including the ambiguous "stage"), then the
    description (narrower list, to avoid false positives like "final stage").
    """
    if job.get("is_internship"):
        return True
    if _INTERNSHIP_TITLE_RE.search(job.get("title", "") or ""):
        return True
    if _INTERNSHIP_DESC_RE.search(job.get("description", "") or ""):
        return True
    return False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Link texts on careers pages that are navigation, not jobs.
_NAV_TITLES = re.compile(
    r"^(jobs?|careers?|vacatures?|vacancies|all jobs|view all( jobs)?|see all( jobs)?|"
    r"search jobs|job search|open positions|apply|learn more|read more|werken bij|"
    r"join us|our jobs|job offers|offres d'emploi|emplois?)$", re.IGNORECASE)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        print(f"[company_scraper] companies.json not found at {COMPANIES_FILE}")
        return []
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("companies", [])


def save_companies(companies: list[dict]):
    existing = {"meta": {}}
    if COMPANIES_FILE.exists():
        with open(COMPANIES_FILE, encoding="utf-8") as f:
            existing = json.load(f)
    existing["companies"] = companies
    existing.setdefault("meta", {})
    existing["meta"]["total_companies"] = len(companies)
    existing["meta"]["last_updated"] = datetime.date.today().isoformat()
    with open(COMPANIES_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def quick_match(text: str) -> bool:
    """Fast keyword pre-filter before anything costs time or money."""
    text_lower = (text or "").lower()
    return any(kw in text_lower for kw in QUICK_FILTER_KEYWORDS)


# ─── Step 1: Read a single careers page ──────────────────────────────────────

def scrape_careers_page(company: dict) -> tuple[list[dict], dict | None]:
    """
    Fetch the company's careers page and extract job links.
    Returns (raw job dicts, a supported job board found on the page or None).
    """
    url = company["careers_url"]
    name = company["name"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [{name}] Fetch error: {e}")
        return [], None

    ats = extra_sources.find_ats_in_html(resp.text, resp.url or url)
    if ats:
        return [], ats

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()

    job_selectors = [
        "a[href*='job']", "a[href*='career']", "a[href*='vacancy']",
        "a[href*='vacature']", "a[href*='position']", "a[href*='opening']",
        "li.job", "div.job", "article.job", "div.vacancy",
        "tr.job-row", "[class*='job-item']", "[class*='job-card']",
        "[class*='vacancy']", "[class*='position-item']",
    ]

    jobs, seen_hrefs = [], set()
    for selector in job_selectors:
        for el in soup.select(selector)[:50]:
            title = el.get_text(separator=" ", strip=True)[:120]
            href = el.get("href", "") if el.name == "a" else ""
            if el.name != "a":
                link = el.find("a", href=True)
                href = link["href"] if link else ""
                if not title and link:
                    title = link.get_text(strip=True)[:120]

            if not title or len(title) < 5 or _NAV_TITLES.match(title.strip()):
                continue
            if href and not href.startswith("http"):
                href = urljoin(url, href)
            if not href or href in seen_hrefs or href.rstrip("/") == url.rstrip("/"):
                continue
            seen_hrefs.add(href)

            job = {
                "id": f"{name}::{href}",
                "title": title,
                "company": name,
                "location": company.get("hq", ""),
                "url": href,
                # Left empty on purpose: job_agent.py fetches the real posting.
                # (It used to hold the company blurb, which mentions
                # sustainability for every company and so let every link
                # through the relevance filter.)
                "description": "",
                "company_note": company.get("why_relevant", ""),
                "sector": company.get("sector", ""),
                "source": "Direct (company site)",
                "category": "Company site",
                "date_posted": "",
            }
            job["is_internship"] = is_internship(job)
            jobs.append(job)

    unique = {j["title"].lower()[:60]: j for j in jobs}
    return list(unique.values()), None


# ─── Step 2: Run over every company ──────────────────────────────────────────

def run_company_scraper() -> list[dict]:
    """
    Reads every company in companies.json. Returns raw, unscored listings that
    pass the title pre-filter; job_agent.py filters, scores and pushes them.
    """
    companies = load_companies()
    all_jobs, changed = [], False

    print(f"\n[company_scraper] Reading {len(companies)} companies...")

    for company in companies:
        name = company["name"]
        ats = company.get("ats") or extra_sources.detect_ats(company.get("careers_url", ""))
        if not ats:
            found, ats = scrape_careers_page(company)
            if ats:
                company["ats"] = ats
                changed = True
                print(f"  [{name}] Uses {ats['type']} — switching to its official job feed")
        if ats:
            found = extra_sources.fetch_board(ats, name)
            for j in found:
                j["category"] = "Company site"
                j["company_note"] = company.get("why_relevant", "")
                j["sector"] = company.get("sector", "")
                j["is_internship"] = is_internship(j)
            kept = [j for j in found if quick_match(j["title"] + " " + j["description"])]
        else:
            kept = [j for j in found if quick_match(j["title"])]
        print(f"  [{name}] {len(found)} listings, {len(kept)} worth a closer look")
        all_jobs.extend(kept)
        time.sleep(1)  # polite crawl delay between companies

    if changed:
        save_companies(companies)
    print(f"[company_scraper] Done. {len(all_jobs)} listings passed the title check.")
    return all_jobs


# ─── Step 3: Weekly auto-discovery of new companies ──────────────────────────

def _load_discovery_state() -> dict:
    if DISCOVERY_STATE_FILE.exists():
        with open(DISCOVERY_STATE_FILE) as f:
            return json.load(f)
    return {"last_run": None}


def _save_discovery_state():
    with open(DISCOVERY_STATE_FILE, "w") as f:
        json.dump({"last_run": datetime.date.today().isoformat()}, f)


def _log_discovery(message: str):
    with open(DISCOVERY_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] {message}\n")


def maybe_discover_new_companies():
    """
    Checks if a week has passed since the last discovery run. If so, asks
    Claude to suggest 10 new companies and appends them to companies.json.
    """
    state = _load_discovery_state()
    last_run = state.get("last_run")

    if last_run:
        days_since = (datetime.date.today() - datetime.date.fromisoformat(last_run)).days
        if days_since < 7:
            print(f"[company_scraper] Auto-discovery skipped ({days_since} days since last run, "
                  f"next in {7 - days_since} days)")
            return

    if not ANTHROPIC_API_KEY:
        print("[company_scraper] Auto-discovery skipped — no ANTHROPIC_API_KEY set")
        return

    print("[company_scraper] Running weekly company auto-discovery...")
    _log_discovery("Starting weekly auto-discovery run")

    companies = load_companies()
    existing_names = {c["name"].lower() for c in companies}

    prompt = f"""You help a sustainability professional find relevant companies to apply to.

The candidate (Lucas, based in Leuven, Belgium) is looking for junior sustainability jobs at:
- Large companies with CSRD reporting obligations (>500 employees, EU-listed)
- High-emitting industries actively building sustainability teams (energy, chemicals, steel,
  aviation, shipping, food & beverage, logistics, cement, agriculture, automotive)
- Companies that recently made net-zero commitments, faced ESG scrutiny, or were fined
  for environmental violations (they tend to hire sustainability staff quickly)
- Sustainability consultancies, ESG software companies and climate-tech companies
- Belgian companies or multinationals with significant Belgian operations

Already in the list (do NOT suggest these again):
{", ".join(sorted(existing_names))}

Suggest exactly 10 NEW companies. Respond ONLY with a JSON array:
[
  {{
    "name": "Company Name",
    "careers_url": "https://exact-url-to-their-jobs-page",
    "sector": "Sector",
    "hq": "City, Country",
    "why_relevant": "One sentence: why this company needs sustainability talent"
  }}
]

Requirements:
- careers_url must be a real, working URL to their actual jobs page. If the company
  publishes its jobs on Greenhouse, Lever, Ashby, Workable, Recruitee or Teamtailor,
  give that job-board link.
- The company must hire in Belgium (offices or operations there)
- Mix pure sustainability companies with high-emitting companies in transition
- Only return the JSON array, no other text"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=MODEL_DISCOVERY,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        new_companies = json.loads(raw[raw.find("["):raw.rfind("]") + 1])

        added = []
        for c in new_companies:
            if not isinstance(c, dict) or not c.get("name") or not c.get("careers_url"):
                continue
            if c["name"].lower() not in existing_names:
                companies.append(c)
                added.append(c["name"])
                existing_names.add(c["name"].lower())

        save_companies(companies)
        _save_discovery_state()

        log_msg = f"Added {len(added)} new companies: {', '.join(added)}"
        _log_discovery(log_msg)
        print(f"[company_scraper] Auto-discovery complete — {log_msg}")

    except Exception as e:
        err = f"Auto-discovery error: {e}"
        _log_discovery(err)
        print(f"[company_scraper] {err}")


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Company careers page scraper")
    parser.add_argument("--discover-only", action="store_true",
                        help="Only run weekly company discovery, skip scraping")
    parser.add_argument("--force-discover", action="store_true",
                        help="Force discovery run even if <7 days since last run")
    args = parser.parse_args()

    if args.force_discover and DISCOVERY_STATE_FILE.exists():
        DISCOVERY_STATE_FILE.unlink()

    if args.discover_only:
        maybe_discover_new_companies()
    else:
        jobs = run_company_scraper()
        for j in jobs:
            print(f"  {j['company']}: {j['title']}  ({j['url']})")
