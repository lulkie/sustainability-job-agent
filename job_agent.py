"""
company_scraper.py  —  Direct careers-page scraper + weekly auto-discovery
===========================================================================
Drop this file into your sustainability_job_agent_v3 folder alongside job_agent.py.
 
What it does:
  1. Loads companies.json and scrapes each company's careers page for open roles
  2. Scores every matching job with Claude (same logic as job_agent.py)
  3. Once a week, asks Claude to discover ~10 new companies and appends them to
     companies.json automatically (fully silent, logged to company_discovery.log)
 
Usage (standalone test):
    python company_scraper.py
 
Integrated in job_agent.py — call at the bottom of your main() function:
    from company_scraper import run_company_scraper, maybe_discover_new_companies
    company_jobs = run_company_scraper()
    maybe_discover_new_companies()
    # Then merge company_jobs into your normal scored_jobs list
 
Dependencies (already in your venv):
    pip install requests beautifulsoup4 anthropic
"""
 
import json
import time
import datetime
import logging
import os
import re
from pathlib import Path
 
import requests
from bs4 import BeautifulSoup
import anthropic
 
# ─── Config ──────────────────────────────────────────────────────────────────
 
COMPANIES_FILE = Path(__file__).parent / "companies.json"
DISCOVERY_LOG  = Path(__file__).parent / "company_discovery.log"
SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
 
# Keywords that flag a job as potentially relevant before sending to Claude
QUICK_FILTER_KEYWORDS = [
    "sustainab", "esg", "csrd", "climate", "environment", "circular",
    "carbon", "emission", "green", "renewable", "energy transition",
    "reporting", "impact", "biodiversity", "net zero", "scope",
    "lca", "life cycle", "waste", "water", "supply chain",
    "intern", "trainee", "stage", "stagiair",
]
 
# ─── Internship detection ────────────────────────────────────────────────────
# Shared with job_agent.py (imported from there) so every source — scraped
# job boards and direct company careers pages alike — gets tagged the same way.
 
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
 
    Checked in order: an explicit flag already set at scrape time (e.g. a
    search that specifically targeted internships), then the job title
    (checked against a broader pattern list including the ambiguous "stage"),
    then the description (checked against a narrower list to avoid false
    positives from unrelated uses of "stage" like "final stage").
    """
    if job.get("is_internship"):
        return True
    if _INTERNSHIP_TITLE_RE.search(job.get("title", "") or ""):
        return True
    if _INTERNSHIP_DESC_RE.search(job.get("description", "") or ""):
        return True
    return False
 
CANDIDATE_PROFILE = """
Lucas Switsers de Roeck — sustainability professional based in Leuven, Belgium.
 
Education:
- MSc Sustainable Development, KU Leuven (2024-2026), major: space & society
- MSc Environmental Sciences, University of Antwerp (2024-2025), cum laude
  Thesis: CSRD reporting quality on financial performance
- MSc Applied Economic Sciences / Marketing, University of Antwerp (2023-2024), cum laude
  Exchange: Hanken School of Economics, Finland
 
Experience:
- VP I2Impact: led 30-person team, international sustainability projects (Peru, Nicaragua, Benin, Uganda)
- Project student Humasol: photovoltaic project in Senegal
- Junior sustainability manager BOMA nv: market analysis, CSRD implementation
- Sustainability associate, University of Antwerp climate team
 
Skills: CSRD reporting, ESG analysis, behavior change communication, sustainability strategy,
environmental science, marketing, Dutch (native), English (fluent), French (professional)
 
Target: Junior/medior sustainability roles in Belgium or remote EU, in ESG, CSRD,
environmental management, sustainability communications, or energy transition.
"""
 
# ─── Helpers ─────────────────────────────────────────────────────────────────
 
def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        print(f"[company_scraper] companies.json not found at {COMPANIES_FILE}")
        return []
    with open(COMPANIES_FILE) as f:
        data = json.load(f)
    return data.get("companies", [])
 
 
def save_companies(companies: list[dict]):
    existing = {}
    if COMPANIES_FILE.exists():
        with open(COMPANIES_FILE) as f:
            existing = json.load(f)
    existing["companies"] = companies
    existing["meta"]["total_companies"] = len(companies)
    existing["meta"]["last_updated"] = datetime.date.today().isoformat()
    with open(COMPANIES_FILE, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
 
 
def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()
 
 
def save_seen_jobs(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)
 
 
def quick_match(text: str) -> bool:
    """Fast keyword pre-filter before calling Claude."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in QUICK_FILTER_KEYWORDS)
 
 
# ─── Step 1: Scrape a single careers page ────────────────────────────────────
 
def scrape_careers_page(company: dict) -> list[dict]:
    """
    Fetch the company's careers page and extract job listings.
    Returns a list of raw job dicts (not yet scored).
    """
    url = company["careers_url"]
    name = company["name"]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
    }
 
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [{name}] Fetch error: {e}")
        return []
 
    soup = BeautifulSoup(resp.text, "html.parser")
 
    # Remove nav, footer, scripts to reduce noise
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()
 
    jobs = []
 
    # Strategy 1: look for common job-listing patterns
    job_selectors = [
        "a[href*='job']", "a[href*='career']", "a[href*='vacancy']",
        "a[href*='vacature']", "a[href*='position']", "a[href*='opening']",
        "li.job", "div.job", "article.job", "div.vacancy",
        "tr.job-row", "[class*='job-item']", "[class*='job-card']",
        "[class*='vacancy']", "[class*='position-item']",
    ]
 
    seen_hrefs = set()
    for selector in job_selectors:
        for el in soup.select(selector)[:50]:
            title = el.get_text(separator=" ", strip=True)[:120]
            href = el.get("href", "") if el.name == "a" else ""
            if el.name != "a":
                link = el.find("a", href=True)
                href = link["href"] if link else ""
                if not title and link:
                    title = link.get_text(strip=True)[:120]
 
            if not title or len(title) < 5:
                continue
            if href in seen_hrefs:
                continue
 
            # Make absolute URL
            if href and not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(url, href)
 
            seen_hrefs.add(href)
 
            job = {
                "id": f"{name}::{href or title}",
                "title": title,
                "company": name,
                "location": company.get("hq", ""),
                "url": href or url,
                "description": company.get("why_relevant", ""),
                "sector": company.get("sector", ""),
                "source": "Direct (company site)",
                "date_posted": "",
            }
            job["is_internship"] = is_internship(job)
            jobs.append(job)
 
    # Deduplicate by title similarity
    unique = {j["title"].lower()[:60]: j for j in jobs}
    result = list(unique.values())
 
    print(f"  [{name}] Found {len(result)} candidate listings on careers page")
    return result
 
 
# ─── Step 2: Score jobs with Claude ──────────────────────────────────────────
 
def score_jobs_with_claude(jobs: list[dict]) -> list[dict]:
    """
    Filter by keyword first, then score remaining jobs with Claude.
    Returns only jobs with score >= 6.
    """
    if not client:
        print("  [Claude] No API key — returning keyword-filtered jobs unscored")
        return [j for j in jobs if quick_match(j["title"] + " " + j.get("description", ""))]
 
    # Pre-filter with keywords to avoid unnecessary API calls
    candidates = [j for j in jobs if quick_match(j["title"] + " " + j.get("description", ""))]
    print(f"  [Claude] {len(candidates)} jobs passed keyword filter, scoring with Claude...")
 
    if not candidates:
        return []
 
    # Batch into groups of 10 to keep prompt size manageable
    scored = []
    for i in range(0, len(candidates), 10):
        batch = candidates[i:i+10]
        job_list = "\n".join(
            f"{idx+1}. [{j['company']}] {j['title']} | {j.get('sector','')}"
            for idx, j in enumerate(batch)
        )
 
        prompt = f"""You are a job-fit scorer for a sustainability professional.
 
Candidate profile:
{CANDIDATE_PROFILE}
 
Score each job from 1–10 for fit. Return ONLY a JSON array like:
[{{"index": 1, "score": 8, "reason": "short reason"}}, ...]
 
Jobs to score:
{job_list}
 
Rules:
- Score 8–10: strong match (sustainability/ESG/CSRD/environment role, entry or junior level)
- Score 6–7: partial match (adjacent role where sustainability background adds value)  
- Score 1–5: poor match (unrelated, senior-only, or purely technical/IT)
- Only return the JSON array, no other text."""
 
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            # Strip markdown fences if present
            raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
            scores = json.loads(raw)
 
            for item in scores:
                idx = item["index"] - 1
                if 0 <= idx < len(batch) and item["score"] >= 6:
                    job = batch[idx].copy()
                    job["score"] = item["score"]
                    job["score_reason"] = item.get("reason", "")
                    scored.append(job)
 
        except Exception as e:
            print(f"  [Claude] Scoring error: {e} — keeping keyword-matched jobs")
            scored.extend(batch)
 
        time.sleep(0.5)  # be polite to the API
 
    return scored
 
 
# ─── Step 3: Run full company scraper ────────────────────────────────────────
 
def run_company_scraper() -> list[dict]:
    """
    Main entry point. Scrapes all companies in companies.json,
    scores matches, filters out seen jobs.
    Returns list of new scored jobs.
    """
    companies = load_companies()
    seen = load_seen_jobs()
    all_jobs = []
 
    print(f"\n[company_scraper] Scraping {len(companies)} companies...")
 
    for company in companies:
        print(f"\n  Scraping {company['name']}...")
        raw_jobs = scrape_careers_page(company)
 
        # Remove already-seen jobs
        new_jobs = [j for j in raw_jobs if j["id"] not in seen]
        if not new_jobs:
            print(f"  [{company['name']}] No new listings since last run")
            continue
 
        scored = score_jobs_with_claude(new_jobs)
 
        for job in scored:
            seen.add(job["id"])
 
        all_jobs.extend(scored)
        time.sleep(1)  # polite crawl delay between companies
 
    save_seen_jobs(seen)
    print(f"\n[company_scraper] Done. {len(all_jobs)} new relevant jobs found across company sites.")
    return all_jobs
 
 
# ─── Step 4: Weekly auto-discovery of new companies ──────────────────────────
 
DISCOVERY_STATE_FILE = Path(__file__).parent / "discovery_state.json"
 
def _load_discovery_state() -> dict:
    if DISCOVERY_STATE_FILE.exists():
        with open(DISCOVERY_STATE_FILE) as f:
            return json.load(f)
    return {"last_run": None}
 
 
def _save_discovery_state():
    with open(DISCOVERY_STATE_FILE, "w") as f:
        json.dump({"last_run": datetime.date.today().isoformat()}, f)
 
 
def _log_discovery(message: str):
    with open(DISCOVERY_LOG, "a") as f:
        f.write(f"[{datetime.datetime.now().isoformat()}] {message}\n")
 
 
def maybe_discover_new_companies():
    """
    Checks if a week has passed since last discovery run.
    If so, asks Claude to suggest 10 new companies and appends them to companies.json.
    Fully silent — logs to company_discovery.log.
    """
    state = _load_discovery_state()
    last_run = state.get("last_run")
 
    if last_run:
        days_since = (datetime.date.today() - datetime.date.fromisoformat(last_run)).days
        if days_since < 7:
            print(f"[company_scraper] Auto-discovery skipped ({days_since} days since last run, next in {7 - days_since} days)")
            return
 
    if not client:
        print("[company_scraper] Auto-discovery skipped — no ANTHROPIC_API_KEY set")
        return
 
    print("[company_scraper] Running weekly company auto-discovery...")
    _log_discovery("Starting weekly auto-discovery run")
 
    companies = load_companies()
    existing_names = {c["name"].lower() for c in companies}
 
    prompt = f"""You help a sustainability professional find relevant companies to apply to.
 
The candidate (Lucas, based in Leuven, Belgium) is looking for sustainability jobs at:
- Large companies with CSRD reporting obligations (>500 employees, EU-listed)
- High-emitting industries actively building sustainability teams (energy, chemicals, steel,
  aviation, shipping, food & beverage, logistics, cement, agriculture, automotive)
- Companies that recently made net-zero commitments, faced ESG scrutiny, or were fined
  for environmental violations (they tend to hire sustainability staff quickly)
- Belgian companies or multinationals with significant Belgian operations
 
Already in the list (do NOT suggest these again):
{", ".join(sorted(existing_names))}
 
Suggest exactly 10 NEW companies. For each, respond ONLY with a JSON array:
[
  {{
    "name": "Company Name",
    "careers_url": "https://exact-url-to-their-jobs-page",
    "sector": "Sector",
    "hq": "City, Country",
    "why_relevant": "One sentence: why this company needs sustainability talent"
  }},
  ...
]
 
Requirements:
- careers_url must be a real, working URL to their actual jobs/careers page
- Vary the sectors and countries (Belgium, Netherlands, Germany, France preferred)
- Mix pure sustainability companies with high-emitting companies in transition
- Only return the JSON array, no other text"""
 
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        new_companies = json.loads(raw)
 
        # Filter out any that already exist
        added = []
        for c in new_companies:
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
 
    if args.force_discover:
        # Reset state to force a discovery run
        if DISCOVERY_STATE_FILE.exists():
            DISCOVERY_STATE_FILE.unlink()
 
    if args.discover_only:
        maybe_discover_new_companies()
    else:
        jobs = run_company_scraper()
        maybe_discover_new_companies()
 
        if jobs:
            print(f"\n{'='*60}")
            print(f"TOP MATCHES FROM COMPANY CAREERS PAGES")
            print(f"{'='*60}")
            for j in sorted(jobs, key=lambda x: x.get("score", 0), reverse=True):
                print(f"\n  [{j.get('score','?')}/10] {j['title']}")
                print(f"  Company : {j['company']} ({j.get('sector','')})")
                print(f"  URL     : {j['url']}")
                print(f"  Reason  : {j.get('score_reason','')}")
        else:
            print("\nNo new matching jobs found on company careers pages this run.")
