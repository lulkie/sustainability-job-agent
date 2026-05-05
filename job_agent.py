"""
Sustainability Job Agent for Lucas Switsers de Roeck
=====================================================
Scrapes LinkedIn, Indeed, and VDAB for sustainability roles in Belgium,
then uses Claude AI to score and filter matches.

No numpy or jobspy required — works on Python 3.14+

Usage:
    python job_agent.py              # Run once, opens HTML report
    python job_agent.py --schedule   # Run every 3 days automatically
"""

import json
import os
import re
import csv
import time
import datetime
import argparse
import webbrowser
import schedule
import requests
from pathlib import Path
from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv

load_dotenv()  # loads .env file from the same folder

# ─── Configuration ───────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RUN_EVERY_DAYS = 3
OUTPUT_DIR = Path("reports")
OUTPUT_DIR.mkdir(exist_ok=True)
SEEN_JOBS_FILE = Path("seen_jobs.json")
MIN_SCORE = 6

CANDIDATE_PROFILE = """
Name: Lucas Switsers de Roeck
Location: Leuven, Belgium

Education:
- MSc Sustainable Development, KU Leuven (2024–2026) — major: space & society
- MSc Environmental Sciences, University of Antwerp (2024–2025) — cum laude; thesis on CSRD reporting quality
- MSc Applied Economic Sciences (Marketing), University of Antwerp (2023–2024) — cum laude

Work experience (~1 year total):
- Vice-President, I2Impact (2025–2026): led 30-student team, sustainability events, international projects
- Project Student Senegal, Humasol (2025–2026): photovoltaic construction research
- Junior Sustainability Manager, BOMA nv (2024): CSRD implementation, sustainability analysis
- Sustainability Associate, University of Antwerp Climate Team (2023–2024)

Key skills: CSRD, ESG, sustainability consulting, environmental science, marketing, behavior change
Languages: Dutch (native), English (fluent), French (professional)
Seniority target: junior to medior only (max ~3–4 years experience required)
Geography: Belgium only
"""

SENIOR_BLOCKLIST = [
    "director", "head of", "chief", "vice president",
    "senior manager", "principal", "10 years", "10+ years",
    "+10 jaar", "+15 jaar", "15 years",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── Scraper: LinkedIn (public search, no login) ─────────────────────────────

def scrape_linkedin(query: str) -> list[dict]:
    """Scrape LinkedIn public job search."""
    jobs = []
    try:
        url = "https://www.linkedin.com/jobs/search/"
        params = {
            "keywords": query,
            "location": "Belgium",
            "f_TPR": "r604800",   # past week
            "f_E": "1,2",         # internship + entry level
            "start": 0,
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select("div.job-search-card, li.jobs-search-results__list-item, div.base-card")
        for card in cards[:20]:
            title_el = card.select_one("h3.base-search-card__title, h3, .job-title")
            company_el = card.select_one("h4.base-search-card__subtitle, h4, .job-listing-company-name")
            location_el = card.select_one("span.job-search-card__location, .job-search-card__location")
            link_el = card.select_one("a[href*='/jobs/view/'], a[href*='linkedin.com/jobs']")

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            link = link_el["href"].split("?")[0] if link_el else ""

            if title and link:
                jobs.append({
                    "id": link + title,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": link,
                    "description": "",
                    "date_posted": "",
                    "source": "LinkedIn",
                })
        print(f"  → {len(jobs)} results from LinkedIn")
    except Exception as e:
        print(f"  [LinkedIn error]: {e}")
    return jobs


# ─── Scraper: Indeed ─────────────────────────────────────────────────────────

def scrape_indeed(query: str) -> list[dict]:
    """Scrape Indeed Belgium."""
    jobs = []
    try:
        url = "https://be.indeed.com/jobs"
        params = {
            "q": query,
            "l": "Belgium",
            "fromage": str(RUN_EVERY_DAYS * 2),  # days old
            "sort": "date",
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select("div.job_seen_beacon, div.jobsearch-SerpJobCard, td.resultContent")
        for card in cards[:20]:
            title_el = card.select_one("h2.jobTitle span, h2 a span, .jobTitle")
            company_el = card.select_one("span.companyName, .companyName, [data-testid='company-name']")
            location_el = card.select_one("div.companyLocation, .companyLocation, [data-testid='text-location']")
            link_el = card.select_one("a[href*='/rc/clk'], a[id^='job_'], h2 a")

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://be.indeed.com" + href

            if title and href:
                jobs.append({
                    "id": href + title,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": href,
                    "description": "",
                    "date_posted": "",
                    "source": "Indeed",
                })
        print(f"  → {len(jobs)} results from Indeed")
    except Exception as e:
        print(f"  [Indeed error]: {e}")
    return jobs


# ─── Scraper: VDAB ───────────────────────────────────────────────────────────

def scrape_vdab(keyword: str) -> list[dict]:
    """Scrape VDAB (Belgian public employment service)."""
    jobs = []
    try:
        # Try JSON API first
        api_url = "https://www.vdab.be/vindeenjob/api/jobs"
        params = {
            "trefwoord": keyword,
            "sort": "publicatieDatum",
            "pagina": 1,
            "aantalPerPagina": 25,
        }
        resp = requests.get(api_url, params=params, headers=HEADERS, timeout=15)

        if resp.status_code == 200 and "application/json" in resp.headers.get("Content-Type", ""):
            data = resp.json()
            vacatures = data.get("vacatures", data.get("jobs", []))
            for j in vacatures:
                title = j.get("functiebenaming", j.get("title", ""))
                company = j.get("bedrijfsnaam", j.get("company", ""))
                city = j.get("gemeente", j.get("city", ""))
                url = j.get("vacatureUrl", j.get("url", ""))
                if url and not url.startswith("http"):
                    url = "https://www.vdab.be" + url
                if title:
                    jobs.append({
                        "id": url + title,
                        "title": title,
                        "company": company,
                        "location": f"{city}, Belgium",
                        "url": url,
                        "description": j.get("omschrijving", "")[:2000],
                        "date_posted": j.get("publicatieDatum", ""),
                        "source": "VDAB",
                    })
        else:
            # Fallback: scrape HTML
            url = f"https://www.vdab.be/vindeenjob/vacatures?trefwoord={requests.utils.quote(keyword)}&sort=publicatieDatum"
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select("article, li.search-result-item, div.vacancy-card")[:20]:
                title_el = card.select_one("h2, h3, .job-title, .vacancy-title")
                company_el = card.select_one(".company-name, .employer, .bedrijfsnaam")
                link_el = card.select_one("a[href]")
                title = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else ""
                href = link_el["href"] if link_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.vdab.be" + href
                if title:
                    jobs.append({
                        "id": href + title,
                        "title": title,
                        "company": company,
                        "location": "Belgium",
                        "url": href,
                        "description": "",
                        "date_posted": "",
                        "source": "VDAB",
                    })

        print(f"  → {len(jobs)} results from VDAB")
    except Exception as e:
        print(f"  [VDAB error]: {e}")
    return jobs


# ─── Scraper: EuroClimateJobs ─────────────────────────────────────────────────

def scrape_euroclimate() -> list[dict]:
    """Scrape EuroClimateJobs for Belgium postings."""
    jobs = []
    try:
        url = "https://www.euroclimateobs.eu/jobs"
        params = {"location": "Belgium", "keywords": "sustainability"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.job-listing, article.job, li.job-item")[:15]:
            title_el = card.select_one("h2, h3, .job-title")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if title and href:
                jobs.append({
                    "id": href + title,
                    "title": title,
                    "company": company,
                    "location": "Belgium",
                    "url": href,
                    "description": "",
                    "date_posted": "",
                    "source": "EuroClimateJobs",
                })
        print(f"  → {len(jobs)} results from EuroClimateJobs")
    except Exception as e:
        print(f"  [EuroClimateJobs error]: {e}")
    return jobs


# ─── Fetch job description ────────────────────────────────────────────────────

def fetch_description(url: str) -> str:
    """Try to fetch the job description from the listing page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try common description containers
        for selector in [
            "div.description__text", "div.jobsearch-jobDescriptionText",
            "div#jobDescriptionText", "div.job-description",
            "section.job-description", "div[class*='description']",
        ]:
            el = soup.select_one(selector)
            if el:
                return el.get_text(separator=" ", strip=True)[:2000]
        # Fallback: grab main content
        main = soup.select_one("main, article, #main-content")
        if main:
            return main.get_text(separator=" ", strip=True)[:2000]
    except Exception:
        pass
    return ""


# ─── AI scoring ──────────────────────────────────────────────────────────────

def score_jobs_with_claude(jobs: list[dict]) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        print("  [Warning] ANTHROPIC_API_KEY not set — skipping AI scoring, showing all jobs.")
        for j in jobs:
            j["score"] = 7
            j["reasoning"] = "AI scoring skipped (no API key set)"
            j["match_highlights"] = []
            j["seniority_ok"] = True
            j["spontaneous_worthy"] = False
        return jobs

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    scored = []
    BATCH = 5

    for i in range(0, len(jobs), BATCH):
        batch = jobs[i:i + BATCH]
        job_list_text = ""
        for idx, j in enumerate(batch):
            job_list_text += f"""
---
JOB {idx + 1}:
Title: {j['title']}
Company: {j['company']}
Location: {j['location']}
Description: {j['description'][:600] if j['description'] else '(no description available)'}
URL: {j['url']}
"""
        prompt = f"""You are a career advisor helping a sustainability candidate find jobs in Belgium.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

JOBS TO EVALUATE:
{job_list_text}

Respond ONLY with a valid JSON array. Each element must have:
- "job_index": integer (1-based)
- "score": integer 1-10 (10 = perfect match)
- "reasoning": string (2-3 sentences)
- "match_highlights": list of up to 3 short strings
- "seniority_ok": boolean (false if too senior)
- "location_ok": boolean (false if not Belgium)
- "spontaneous_worthy": boolean (true if company is great fit even if this job isn't perfect)

Score below 6 if: too senior, not in Belgium, or unrelated to sustainability.
Return ONLY the JSON array, no other text."""

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            scores = json.loads(raw)
            for s in scores:
                idx = s["job_index"] - 1
                if 0 <= idx < len(batch):
                    batch[idx]["score"] = s.get("score", 5)
                    batch[idx]["reasoning"] = s.get("reasoning", "")
                    batch[idx]["match_highlights"] = s.get("match_highlights", [])
                    batch[idx]["seniority_ok"] = s.get("seniority_ok", True)
                    batch[idx]["spontaneous_worthy"] = s.get("spontaneous_worthy", False)
        except Exception as e:
            print(f"  [Claude scoring error]: {e}")
            for j in batch:
                j.setdefault("score", 5)
                j.setdefault("reasoning", "Scoring failed")
                j.setdefault("match_highlights", [])
                j.setdefault("seniority_ok", True)
                j.setdefault("spontaneous_worthy", False)

        scored.extend(batch)
        time.sleep(1)

    return scored


# ─── Helpers ─────────────────────────────────────────────────────────────────

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()


def save_seen_jobs(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(list(seen)))


def is_too_senior(job: dict) -> bool:
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    return any(kw.lower() in text for kw in SENIOR_BLOCKLIST)


def deduplicate(jobs: list[dict]) -> list[dict]:
    seen_ids, result = set(), []
    for j in jobs:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            result.append(j)
    return result


# ─── HTML report ─────────────────────────────────────────────────────────────

def generate_html_report(jobs: list[dict], spontaneous: list[dict]) -> Path:
    date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    output_path = OUTPUT_DIR / f"jobs_{date_str}.html"
    jobs_sorted = sorted(jobs, key=lambda j: j.get("score", 0), reverse=True)

    def job_card(j, is_spont=False):
        score = j.get("score", "?")
        color = "#22c55e" if score >= 8 else "#f59e0b" if score >= 6 else "#ef4444"
        highlights = "".join(f"<li>{h}</li>" for h in j.get("match_highlights", []))
        spont_tag = '<span class="spont-tag">💌 Spontaneous Application</span>' if is_spont else ""
        return f"""
        <div class="card">
          <div class="card-top">
            <div>
              <div class="title"><a href="{j['url']}" target="_blank">{j['title']}</a></div>
              <div class="meta">{j['company']} &middot; {j['location']} &middot; <span class="source">{j['source']}</span></div>
            </div>
            <div class="score-box" style="background:{color}">{score}/10</div>
          </div>
          {spont_tag}
          {'<ul class="highlights">' + highlights + '</ul>' if highlights else ''}
          <div class="reasoning">{j.get('reasoning','')}</div>
          <a href="{j['url']}" target="_blank" class="btn">View Job →</a>
        </div>"""

    good_cards = "".join(job_card(j) for j in jobs_sorted if j.get("score", 0) >= MIN_SCORE)
    spont_cards = "".join(job_card(j, is_spont=True) for j in spontaneous)
    good_count = sum(1 for j in jobs_sorted if j.get("score", 0) >= MIN_SCORE)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sustainability Jobs Belgium</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0fdf4;color:#1e293b}}
  .header{{background:linear-gradient(135deg,#064e3b,#059669);color:white;padding:2rem}}
  .header h1{{font-size:1.7rem;font-weight:700}}
  .header p{{opacity:.85;margin-top:.3rem}}
  .stats{{display:flex;gap:1rem;margin-top:1rem;flex-wrap:wrap}}
  .stat{{background:rgba(255,255,255,.15);padding:.4rem 1rem;border-radius:8px;font-size:.88rem}}
  .container{{max-width:860px;margin:2rem auto;padding:0 1rem}}
  .section{{font-size:1.1rem;font-weight:700;color:#064e3b;margin:2rem 0 1rem;padding-bottom:.4rem;border-bottom:2px solid #bbf7d0}}
  .card{{background:white;border-radius:12px;padding:1.25rem;margin-bottom:1rem;box-shadow:0 1px 4px rgba(0,0,0,.08);border:1px solid #e2e8f0}}
  .card:hover{{box-shadow:0 4px 12px rgba(0,0,0,.12)}}
  .card-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}}
  .title a{{font-size:1.05rem;font-weight:600;color:#0f172a;text-decoration:none}}
  .title a:hover{{color:#059669}}
  .meta{{font-size:.85rem;color:#64748b;margin-top:.2rem}}
  .source{{background:#f1f5f9;padding:1px 7px;border-radius:4px;font-size:.78rem}}
  .score-box{{background:#22c55e;color:white;font-weight:700;padding:.3rem .7rem;border-radius:8px;white-space:nowrap;font-size:.95rem}}
  .highlights{{margin:.7rem 0 .3rem 1.2rem;color:#065f46;font-size:.87rem}}
  .highlights li{{margin-bottom:.15rem}}
  .reasoning{{color:#475569;font-size:.87rem;margin-top:.5rem;line-height:1.5}}
  .btn{{display:inline-block;margin-top:.9rem;background:#059669;color:white;padding:.35rem .9rem;border-radius:6px;text-decoration:none;font-size:.85rem;font-weight:500}}
  .btn:hover{{background:#047857}}
  .spont-tag{{display:inline-block;margin:.5rem 0;background:#dbeafe;color:#1d4ed8;font-size:.78rem;padding:2px 10px;border-radius:4px;font-weight:600}}
  .empty{{color:#94a3b8;font-style:italic;padding:.5rem 0}}
  footer{{text-align:center;color:#94a3b8;font-size:.8rem;padding:2rem}}
</style>
</head>
<body>
<div class="header">
  <h1>🌿 Sustainability Jobs — Belgium</h1>
  <p>Personalised report for Lucas Switsers de Roeck</p>
  <div class="stats">
    <div class="stat">📅 {datetime.datetime.now().strftime("%d %B %Y")}</div>
    <div class="stat">🔍 {len(jobs_sorted)} jobs scanned</div>
    <div class="stat">✅ {good_count} strong matches</div>
    <div class="stat">🏢 {len(spontaneous)} spontaneous leads</div>
  </div>
</div>
<div class="container">
  <div class="section">✅ Strong Matches (score ≥ {MIN_SCORE}/10)</div>
  {good_cards or '<p class="empty">No strong matches this run — try again in a few days.</p>'}
  {'<div class="section">🏢 Worth a Spontaneous Application</div>' + spont_cards if spont_cards else ''}
</div>
<footer>Generated by Sustainability Job Agent &middot; Next run in {RUN_EVERY_DAYS} days</footer>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path


def save_csv(jobs: list[dict], path: Path):
    if not jobs:
        return
    fields = ["score", "title", "company", "location", "source", "date_posted", "url", "reasoning"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(jobs, key=lambda j: j.get("score", 0), reverse=True))


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_agent():
    print(f"\n{'='*55}")
    print(f"  Sustainability Job Agent — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    seen = load_seen_jobs()
    all_jobs = []

    linkedin_queries = [
        "sustainability consultant Belgium",
        "ESG CSRD analyst Belgium",
        "environmental project manager Belgium",
        "duurzaamheid adviseur",
        "climate policy Belgium",
    ]
    indeed_queries = [
        "sustainability officer Belgium",
        "ESG reporting Belgium",
        "circular economy Belgium",
        "sustainability communications Belgium",
    ]
    vdab_keywords = ["duurzaamheid", "ESG", "milieu", "CSRD", "klimaat"]

    for q in linkedin_queries:
        print(f"[LinkedIn] '{q}'")
        all_jobs.extend(scrape_linkedin(q))
        time.sleep(3)

    for q in indeed_queries:
        print(f"[Indeed]   '{q}'")
        all_jobs.extend(scrape_indeed(q))
        time.sleep(3)

    for k in vdab_keywords:
        print(f"[VDAB]     '{k}'")
        all_jobs.extend(scrape_vdab(k))
        time.sleep(2)

    print(f"[EuroClimate]")
    all_jobs.extend(scrape_euroclimate())

    # Deduplicate
    all_jobs = deduplicate(all_jobs)
    print(f"\n[Filter] {len(all_jobs)} unique jobs total")

    # Remove already seen
    new_jobs = [j for j in all_jobs if j["id"] not in seen]
    print(f"[Filter] {len(new_jobs)} new (unseen) jobs")

    # Remove too senior
    new_jobs = [j for j in new_jobs if not is_too_senior(j)]
    print(f"[Filter] {len(new_jobs)} after seniority filter")

    if not new_jobs:
        print("\n[Done] No new jobs found this run.")
        return

    # Fetch descriptions for better scoring (first 30 only to stay fast)
    print(f"\n[Fetch] Getting job descriptions (up to 30)...")
    for j in new_jobs[:30]:
        if not j["description"]:
            j["description"] = fetch_description(j["url"])
            time.sleep(1)

    # Score with Claude
    print(f"\n[Claude] Scoring {len(new_jobs)} jobs...")
    scored = score_jobs_with_claude(new_jobs)

    # Find spontaneous candidates
    spontaneous = []
    seen_companies = set()
    for j in scored:
        if j.get("spontaneous_worthy") and j.get("score", 0) < MIN_SCORE:
            c = j.get("company", "")
            if c and c not in seen_companies:
                seen_companies.add(c)
                spontaneous.append(j.copy())

    # Save seen jobs
    save_seen_jobs(seen | {j["id"] for j in scored})

    # Generate report
    date_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    report_path = generate_html_report(scored, spontaneous)
    csv_path = OUTPUT_DIR / f"jobs_{date_str}.csv"
    save_csv(scored, csv_path)

    good = sum(1 for j in scored if j.get("score", 0) >= MIN_SCORE)
    print(f"\n[Done] {good} strong matches + {len(spontaneous)} spontaneous leads")
    print(f"[Report] {report_path.resolve()}")

    webbrowser.open(report_path.resolve().as_uri())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", action="store_true", help=f"Run every {RUN_EVERY_DAYS} days")
    args = parser.parse_args()

    if args.schedule:
        print(f"[Scheduler] Running every {RUN_EVERY_DAYS} days. Press Ctrl+C to stop.")
        run_agent()
        schedule.every(RUN_EVERY_DAYS).days.do(run_agent)
        while True:
            schedule.run_pending()
            time.sleep(3600)
    else:
        run_agent()


if __name__ == "__main__":
    main()
