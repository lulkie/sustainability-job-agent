"""
Sustainability Job Agent for Lucas Switsers de Roeck
=====================================================
Scrapes LinkedIn, Indeed, and VDAB for sustainability roles in Belgium,
scores with Claude AI, and pushes results to Notion.

No numpy or jobspy required — works on Python 3.14+
"""

import json
import os
import re
import time
import datetime
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from company_scraper import run_company_scraper, maybe_discover_new_companies

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

RUN_EVERY_DAYS = 3
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

# ─── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_linkedin(query: str) -> list[dict]:
    jobs = []
    try:
        url = "https://www.linkedin.com/jobs/search/"
        params = {
            "keywords": query,
            "location": "Belgium",
            "f_TPR": "r604800",
            "f_E": "1,2",
            "start": 0,
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.job-search-card, li.jobs-search-results__list-item, div.base-card")
        for card in cards[:20]:
            title_el = card.select_one("h3.base-search-card__title, h3, .job-title")
            company_el = card.select_one("h4.base-search-card__subtitle, h4, .job-listing-company-name")
            location_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a[href*='/jobs/view/'], a[href*='linkedin.com/jobs']")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            link = link_el["href"].split("?")[0] if link_el else ""
            if title and link:
                jobs.append({"id": link + title, "title": title, "company": company,
                             "location": location, "url": link, "description": "",
                             "date_posted": "", "source": "LinkedIn"})
        print(f"  → {len(jobs)} from LinkedIn")
    except Exception as e:
        print(f"  [LinkedIn error]: {e}")
    return jobs


def scrape_indeed(query: str) -> list[dict]:
    jobs = []
    try:
        url = "https://be.indeed.com/jobs"
        params = {"q": query, "l": "Belgium", "fromage": str(RUN_EVERY_DAYS * 2), "sort": "date"}
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.job_seen_beacon, div.jobsearch-SerpJobCard, td.resultContent")
        for card in cards[:20]:
            title_el = card.select_one("h2.jobTitle span, h2 a span, .jobTitle")
            company_el = card.select_one("span.companyName, [data-testid='company-name']")
            location_el = card.select_one("div.companyLocation, [data-testid='text-location']")
            link_el = card.select_one("a[href*='/rc/clk'], h2 a")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://be.indeed.com" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": location, "url": href, "description": "",
                             "date_posted": "", "source": "Indeed"})
        print(f"  → {len(jobs)} from Indeed")
    except Exception as e:
        print(f"  [Indeed error]: {e}")
    return jobs


def scrape_vdab(keyword: str) -> list[dict]:
    jobs = []
    try:
        api_url = "https://www.vdab.be/vindeenjob/api/jobs"
        params = {"trefwoord": keyword, "sort": "publicatieDatum", "pagina": 1, "aantalPerPagina": 25}
        resp = requests.get(api_url, params=params, headers=HEADERS, timeout=15)
        if resp.status_code == 200 and "application/json" in resp.headers.get("Content-Type", ""):
            data = resp.json()
            for j in data.get("vacatures", []):
                title = j.get("functiebenaming", "")
                company = j.get("bedrijfsnaam", "")
                city = j.get("gemeente", "")
                url = j.get("vacatureUrl", "")
                if url and not url.startswith("http"):
                    url = "https://www.vdab.be" + url
                if title:
                    jobs.append({"id": url + title, "title": title, "company": company,
                                 "location": f"{city}, Belgium", "url": url,
                                 "description": j.get("omschrijving", "")[:2000],
                                 "date_posted": j.get("publicatieDatum", ""), "source": "VDAB"})
        else:
            # HTML fallback
            url = f"https://www.vdab.be/vindeenjob/vacatures?trefwoord={requests.utils.quote(keyword)}"
            resp = requests.get(url, headers=HEADERS, timeout=8)
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select("article, li.search-result-item")[:20]:
                title_el = card.select_one("h2, h3, .job-title")
                company_el = card.select_one(".company-name, .bedrijfsnaam")
                link_el = card.select_one("a[href]")
                title = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else ""
                href = link_el["href"] if link_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.vdab.be" + href
                if title:
                    jobs.append({"id": href + title, "title": title, "company": company,
                                 "location": "Belgium", "url": href, "description": "",
                                 "date_posted": "", "source": "VDAB"})
        print(f"  → {len(jobs)} from VDAB")
    except Exception as e:
        print(f"  [VDAB error]: {e}")
    return jobs


def scrape_jobat(query: str) -> list[dict]:
    """Scrape Jobat.be — biggest Belgian job board."""
    jobs = []
    try:
        url = "https://www.jobat.be/en/jobs"
        params = {"q": query, "r": "BE", "sort": "date"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article.job, div.job-card, li.job-item, div[class*='vacancy']")[:20]:
            title_el = card.select_one("h2, h3, .job-title, [class*='title']")
            company_el = card.select_one(".company, .employer, [class*='company']")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.jobat.be" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Jobat"})
        print(f"  → {len(jobs)} from Jobat")
    except Exception as e:
        print(f"  [Jobat error]: {e}")
    return jobs


def scrape_stepstone(query: str) -> list[dict]:
    """Scrape Stepstone.be."""
    jobs = []
    try:
        url = f"https://www.stepstone.be/jobs/{requests.utils.quote(query)}/in-belgium"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article[data-at='job-item'], div.job-ad, li[class*='job']")[:20]:
            title_el = card.select_one("h2, h3, [data-at='job-item-title'], .job-title")
            company_el = card.select_one("[data-at='job-item-company-name'], .company-name")
            location_el = card.select_one("[data-at='job-item-location'], .location")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.stepstone.be" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": location, "url": href, "description": "",
                             "date_posted": "", "source": "Stepstone"})
        print(f"  → {len(jobs)} from Stepstone")
    except Exception as e:
        print(f"  [Stepstone error]: {e}")
    return jobs


def scrape_euroclimatejobs() -> list[dict]:
    """Scrape EuroClimateJobs for Belgium."""
    jobs = []
    try:
        url = "https://www.euroclimatejobs.com/jobs/belgium"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.job, article.job, li.job-listing, tr.job-row")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a[href*='job']")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.euroclimatejobs.com" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "EuroClimateJobs"})
        print(f"  → {len(jobs)} from EuroClimateJobs")
    except Exception as e:
        print(f"  [EuroClimateJobs error]: {e}")
    return jobs


def scrape_eurobrussels() -> list[dict]:
    """Scrape EuroBrussels for environment/sustainability jobs."""
    jobs = []
    try:
        url = "https://www.eurobrussels.com/jobs/environment"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.job, article, li.job-item, div[class*='vacancy']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a.job-link")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.eurobrussels.com" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Brussels, Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "EuroBrussels"})
        print(f"  → {len(jobs)} from EuroBrussels")
    except Exception as e:
        print(f"  [EuroBrussels error]: {e}")
    return jobs


def scrape_brussels_sustainability_club() -> list[dict]:
    """Scrape Brussels Sustainability Club job board."""
    jobs = []
    try:
        url = "https://brusselssustainabilityclub.com/jobs/"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.job, div[class*='job']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Brussels, Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Brussels Sustainability Club"})
        print(f"  → {len(jobs)} from Brussels Sustainability Club")
    except Exception as e:
        print(f"  [Brussels Sustainability Club error]: {e}")
    return jobs


def scrape_actiris(keyword: str) -> list[dict]:
    """Scrape Actiris — Brussels regional employment service."""
    jobs = []
    try:
        url = "https://www.actiris.brussels/en/citizens/find-a-job/"
        params = {"q": keyword}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.vacancy, div[class*='offer']")[:20]:
            title_el = card.select_one("h2, h3, .job-title, .offer-title")
            company_el = card.select_one(".company, .employer")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.actiris.brussels" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Brussels, Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Actiris"})
        print(f"  → {len(jobs)} from Actiris")
    except Exception as e:
        print(f"  [Actiris error]: {e}")
    return jobs


def scrape_glassdoor(query: str) -> list[dict]:
    """Scrape Glassdoor for Belgium sustainability jobs."""
    jobs = []
    try:
        url = "https://www.glassdoor.com/Job/belgium-sustainability-jobs-SRCH_IL.0,7_IN25_KO8,22.htm"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("li[data-test='jobListing'], div.jobCard, article.job")[:20]:
            title_el = card.select_one("[data-test='job-title'], .job-title, h2, h3")
            company_el = card.select_one("[data-test='employer-name'], .employer-name, .company")
            location_el = card.select_one("[data-test='emp-location'], .location")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else "Belgium"
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.glassdoor.com" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": location, "url": href, "description": "",
                             "date_posted": "", "source": "Glassdoor"})
        print(f"  → {len(jobs)} from Glassdoor")
    except Exception as e:
        print(f"  [Glassdoor error]: {e}")
    return jobs


def scrape_epso() -> list[dict]:
    """Scrape EPSO — EU institutions official job board."""
    jobs = []
    try:
        url = "https://epso.europa.eu/en/job-opportunities/open-for-application"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.ecl-content-item, div[class*='job']")[:30]:
            title_el = card.select_one("h2, h3, .ecl-content-item__title, a")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://epso.europa.eu" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": "EU Institution",
                             "location": "Brussels, Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "EPSO"})
        print(f"  → {len(jobs)} from EPSO")
    except Exception as e:
        print(f"  [EPSO error]: {e}")
    return jobs


def scrape_euraxess() -> list[dict]:
    """Scrape Euraxess — EU research and policy jobs."""
    jobs = []
    try:
        url = "https://euraxess.ec.europa.eu/jobs/search?f[0]=field_job_country:Belgium"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.views-row, div[class*='job']")[:30]:
            title_el = card.select_one("h2, h3, .field-title, a")
            company_el = card.select_one(".field-organisation, .company, .employer")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else "EU/Research"
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://euraxess.ec.europa.eu" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Euraxess"})
        print(f"  → {len(jobs)} from Euraxess")
    except Exception as e:
        print(f"  [Euraxess error]: {e}")
    return jobs


def scrape_un_careers() -> list[dict]:
    """Scrape UN Careers portal for sustainability/environment roles."""
    jobs = []
    institutions = [
        ("UNDP", "https://jobs.undp.org/cj_view_jobs.cfm?job_country=Belgium"),
        ("UNEP", "https://www.unep.org/about-un-environment/employment-opportunities"),
        ("UNICEF", "https://www.unicef.org/careers/search?location=Belgium"),
        ("WFP", "https://career5.successfactors.eu/career?company=C0000168410P&site=Belgium"),
        ("ILO", "https://jobs.ilo.org/job-search-results/?locations=Belgium"),
        ("UNESCO", "https://careers.unesco.org/go/International-Professional-Posts/3803102/"),
        ("IOM", "https://careers.iom.int/vacancies?country%5B%5D=BE"),
        ("UNFPA", "https://www.unfpa.org/jobs"),
    ]
    for org_name, url in institutions:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select("article, div.job, li.job, tr[class*='job'], div[class*='vacancy'], li[class*='result']")[:15]:
                title_el = card.select_one("h2, h3, td.title, .job-title, a")
                link_el = card.select_one("a[href]")
                title = title_el.get_text(strip=True) if title_el else ""
                href = link_el["href"] if link_el else url
                if href and not href.startswith("http"):
                    href = url.split("/")[0] + "//" + url.split("/")[2] + href
                if title and href:
                    jobs.append({"id": href + title, "title": title, "company": org_name,
                                 "location": "Brussels, Belgium", "url": href, "description": "",
                                 "date_posted": "", "source": "UN Careers"})
        except Exception as e:
            print(f"  [UN {org_name} error]: {e}")
        time.sleep(1)
    # Also search LinkedIn for UN roles
    un_orgs = ["UNDP", "UNEP", "UNICEF", "WFP", "ILO", "UNESCO", "UN Women",
               "UNFPA", "UN-Habitat", "IFAD", "UNIDO", "OHCHR", "IOM", "UNU"]
    try:
        for org in un_orgs[:6]:  # limit to avoid rate limiting
            url = "https://www.linkedin.com/jobs/search/"
            params = {"keywords": f"{org} Belgium sustainability environment", "location": "Belgium", "f_TPR": "r2592000"}
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select("div.base-card")[:5]:
                title_el = card.select_one("h3.base-search-card__title")
                company_el = card.select_one("h4.base-search-card__subtitle")
                link_el = card.select_one("a[href*='/jobs/view/']")
                title = title_el.get_text(strip=True) if title_el else ""
                company = company_el.get_text(strip=True) if company_el else org
                href = link_el["href"].split("?")[0] if link_el else ""
                if title and href:
                    jobs.append({"id": href + title, "title": title, "company": company,
                                 "location": "Belgium", "url": href, "description": "",
                                 "date_posted": "", "source": "LinkedIn"})
            time.sleep(2)
    except Exception as e:
        print(f"  [UN LinkedIn error]: {e}")
    print(f"  → {len(jobs)} from UN System")
    return jobs


def scrape_eu_institutions() -> list[dict]:
    """Scrape EU institution career portals beyond EPSO."""
    jobs = []
    portals = [
        ("European Commission", "https://epso.europa.eu/en/job-opportunities/open-for-application"),
        ("European Parliament", "https://www.europarl.europa.eu/at-your-service/en/work-with-us/temporary-agents"),
        ("EEA", "https://www.eea.europa.eu/about-us/jobs"),
        ("Eurofound", "https://www.eurofound.europa.eu/en/about/careers"),
        ("EU-OSHA", "https://osha.europa.eu/en/about-eu-osha/jobs-and-trainees"),
        ("EIGE", "https://eige.europa.eu/about/jobs-and-traineeships"),
        ("FRA", "https://fra.europa.eu/en/about-fra/jobs"),
        ("ETF", "https://www.etf.europa.eu/en/about-etf/jobs"),
        ("CINEA", "https://cinea.ec.europa.eu/about/jobs_en"),
        ("EESC", "https://www.eesc.europa.eu/en/about/work-us/job-opportunities"),
    ]
    for org_name, url in portals:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for card in soup.select("article, div.job, li.ecl-content-item, tr[class*='job'], div[class*='vacancy'], li[class*='result']")[:15]:
                title_el = card.select_one("h2, h3, td.title, .ecl-content-item__title, .job-title, a")
                link_el = card.select_one("a[href]")
                title = title_el.get_text(strip=True) if title_el else ""
                href = link_el["href"] if link_el else url
                if href and not href.startswith("http"):
                    href = "https://" + url.split("/")[2] + href
                if title and href:
                    jobs.append({"id": href + title, "title": title, "company": org_name,
                                 "location": "Brussels, Belgium", "url": href, "description": "",
                                 "date_posted": "", "source": "EU Institutions"})
        except Exception as e:
            print(f"  [EU {org_name} error]: {e}")
        time.sleep(1)
    print(f"  → {len(jobs)} from EU Institutions")
    return jobs


def get_rejected_companies() -> set:
    """Read companies marked as rejected in Notion."""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        return set()
    rejected = set()
    try:
        url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        payload = {
            "filter": {
                "property": "Reject Company",
                "checkbox": {"equals": True}
            },
            "page_size": 100,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        data = resp.json()
        for page in data.get("results", []):
            company_prop = page.get("properties", {}).get("Company", {})
            rich_text = company_prop.get("rich_text", [])
            if rich_text:
                rejected.add(rich_text[0]["text"]["content"].lower().strip())
    except Exception as e:
        print(f"  [Notion] Could not fetch rejected companies: {e}")
    return rejected


def fetch_description(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in ["div.description__text", "div.jobsearch-jobDescriptionText",
                         "div#jobDescriptionText", "div.job-description"]:
            el = soup.select_one(selector)
            if el:
                return el.get_text(separator=" ", strip=True)[:2000]
        main = soup.select_one("main, article")
        if main:
            return main.get_text(separator=" ", strip=True)[:2000]
    except Exception:
        pass
    return ""


# ─── AI Scoring ───────────────────────────────────────────────────────────────

def score_jobs_with_claude(jobs: list[dict]) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        print("  [Warning] No ANTHROPIC_API_KEY — skipping AI scoring.")
        for j in jobs:
            j.update({"score": 7, "reasoning": "AI scoring skipped", "match_highlights": [],
                      "seniority_ok": True, "spontaneous_worthy": False})
        return jobs

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    scored = []
    for i in range(0, len(jobs), 5):
        batch = jobs[i:i + 5]
        job_list_text = ""
        for idx, j in enumerate(batch):
            job_list_text += f"""
---
JOB {idx + 1}:
Title: {j['title']}
Company: {j['company']}
Location: {j['location']}
Description: {j['description'][:600] or '(none)'}
"""
        prompt = f"""You are a career advisor helping a sustainability candidate find jobs in Belgium.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

JOBS TO EVALUATE:
{job_list_text}

Respond ONLY with a valid JSON array. Each element:
- "job_index": integer (1-based)
- "score": integer 1-10
- "reasoning": string (2-3 sentences)
- "match_highlights": list of up to 3 short strings
- "seniority_ok": boolean
- "location_ok": boolean
- "spontaneous_worthy": boolean
- "deadline": string in YYYY-MM-DD format if an application deadline is mentioned, otherwise ""

Return ONLY the JSON array."""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            for s in json.loads(raw):
                idx = s["job_index"] - 1
                if 0 <= idx < len(batch):
                    batch[idx].update({
                        "score": s.get("score", 5),
                        "reasoning": s.get("reasoning", ""),
                        "match_highlights": s.get("match_highlights", []),
                        "seniority_ok": s.get("seniority_ok", True),
                        "spontaneous_worthy": s.get("spontaneous_worthy", False),
                        "deadline": s.get("deadline", ""),
                    })
        except Exception as e:
            print(f"  [Claude error]: {e}")
            for j in batch:
                j.setdefault("score", 5)
                j.setdefault("reasoning", "Scoring failed")
                j.setdefault("match_highlights", [])
                j.setdefault("seniority_ok", True)
                j.setdefault("spontaneous_worthy", False)
        scored.extend(batch)
        time.sleep(1)
    return scored


# ─── Notion Integration ───────────────────────────────────────────────────────

def push_to_notion(jobs: list[dict], spontaneous: list[dict]):
    """Push scored jobs to a Notion database."""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        print("  [Notion] No credentials set — skipping Notion push.")
        return

    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    all_to_push = [(j, False) for j in jobs if j.get("score", 0) >= MIN_SCORE]
    all_to_push += [(j, True) for j in spontaneous]

    # Reverse so highest scored jobs end up at top of Notion
    all_to_push = sorted(all_to_push, key=lambda x: x[0].get("score", 0))

    pushed = 0
    for job, is_spont in all_to_push:
        highlights = " | ".join(job.get("match_highlights", []))
        job_type = "Spontaneous" if is_spont else "Vacancy"
        score = job.get("score", 0)

        properties = {
            "Job Title": {
                "title": [{"text": {"content": job.get("title", "")}}]
            },
            "Company": {
                "rich_text": [{"text": {"content": job.get("company", "")}}]
            },
            "Location": {
                "rich_text": [{"text": {"content": job.get("location", "")}}]
            },
            "Source": {
                "select": {"name": job.get("source", "Other")}
            },
            "Score": {
                "number": score
            },
            "Type": {
                "select": {"name": job_type}
            },
            "URL": {
                "url": job.get("url", "") or None
            },
            "Category": {
                "select": {"name": job.get("category", "General")}
            },
            "Reasoning": {
                "rich_text": [{"text": {"content": job.get("reasoning", "")[:2000]}}]
            },
            "Highlights": {
                "rich_text": [{"text": {"content": highlights[:2000]}}]
            },
            "Date Found": {
                "date": {"start": datetime.datetime.now().strftime("%Y-%m-%d")}
            },
            "Status": {
                "select": {"name": "New"}
            },
        }

        # Add deadline if Claude extracted one
        deadline = job.get("deadline", "")
        if deadline:
            properties["Deadline"] = {"date": {"start": deadline}}

        payload = {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": properties,
        }

        try:
            resp = requests.post("https://api.notion.com/v1/pages",
                                 headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                pushed += 1
            else:
                print(f"  [Notion] Failed to push '{job.get('title')}': {resp.text[:200]}")
        except Exception as e:
            print(f"  [Notion error]: {e}")
        time.sleep(0.4)

    print(f"  [Notion] Pushed {pushed} jobs to your database.")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()

def save_seen_jobs(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(list(seen)))

def scrape_greenjobs() -> list[dict]:
    """Scrape Greenjobs.nl for Belgian sustainability jobs."""
    jobs = []
    try:
        url = "https://greenjobs.nl/en/sustainable-jobs/?location=Belgium"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.job-item, div[class*='vacancy']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://greenjobs.nl" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Greenjobs.nl"})
        print(f"  → {len(jobs)} from Greenjobs.nl")
    except Exception as e:
        print(f"  [Greenjobs error]: {e}")
    return jobs


def scrape_impactjob() -> list[dict]:
    """Scrape Impactjob.be for Belgian impact/sustainability jobs."""
    jobs = []
    try:
        url = "https://www.impactjob.be/jobs"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.job, div[class*='job']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.impactjob.be" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Impactjob.be"})
        print(f"  → {len(jobs)} from Impactjob.be")
    except Exception as e:
        print(f"  [Impactjob error]: {e}")
    return jobs


def scrape_climatebase() -> list[dict]:
    """Scrape Climatebase for Belgium climate jobs."""
    jobs = []
    try:
        url = "https://climatebase.org/jobs?l=Belgium&q=sustainability"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("div.job, article, li.job-item, a[href*='/jobs/']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, .title")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://climatebase.org" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Climatebase"})
        print(f"  → {len(jobs)} from Climatebase")
    except Exception as e:
        print(f"  [Climatebase error]: {e}")
    return jobs


def scrape_terraincognita() -> list[dict]:
    """Scrape Terra Incognita — Belgian NGO and sustainability jobs."""
    jobs = []
    try:
        url = "https://www.terraincognita.be/jobs"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.job, div[class*='job']")[:30]:
            title_el = card.select_one("h2, h3, .job-title, a")
            company_el = card.select_one(".company, .employer")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = "https://www.terraincognita.be" + href
            if title and href:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": "Belgium", "url": href, "description": "",
                             "date_posted": "", "source": "Terra Incognita"})
        print(f"  → {len(jobs)} from Terra Incognita")
    except Exception as e:
        print(f"  [Terra Incognita error]: {e}")
    return jobs


def is_too_much_experience(job: dict) -> bool:
    """Return True if the job explicitly requires more than 3 years of experience."""
    import re
    text = (job.get("title", "") + " " + job.get("description", "")).lower()

    # Patterns like "4 years", "5+ years", "minimum 4 jaar", "10 years experience"
    patterns = [
        r'(\d+)\s*\+?\s*years?\s*(of\s*)?(experience|exp)',
        r'(\d+)\s*\+?\s*jaar\s*(ervaring|werkervaring)',
        r'minimum\s*(\d+)\s*(years?|jaar)',
        r'at\s*least\s*(\d+)\s*years?',
        r'minstens\s*(\d+)\s*jaar',
        r'(\d+)\s*to\s*\d+\s*years?\s*(of\s*)?experience',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            # Extract the first number from the match tuple
            years = int(match[0]) if match[0].isdigit() else 0
            if years > 3:
                return True
    return False


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


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_agent():
    print(f"\n{'='*55}")
    print(f"  Sustainability Job Agent — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    seen = load_seen_jobs()
    all_jobs = []

    linkedin_queries = ["sustainability consultant Belgium", "ESG CSRD analyst Belgium",
                        "environmental project manager Belgium", "duurzaamheid adviseur",
                        "climate policy Belgium", "NGO sustainability Belgium",
                        "environmental communications Belgium", "sustainability advocacy Belgium",
                        "corporate social responsibility Belgium"]
    vdab_keywords    = ["duurzaamheid", "ESG", "milieu", "CSRD", "klimaat",
                        "milieubeleid", "duurzame ontwikkeling"]
    jobat_queries    = ["sustainability", "ESG", "duurzaamheid", "milieu adviseur",
                        "NGO", "beleidsmedewerker milieu"]
    stepstone_queries= ["sustainability Belgium", "ESG Belgium", "duurzaamheid",
                        "environmental policy Belgium"]
    actiris_keywords = ["sustainability", "environment", "duurzaamheid", "ESG",
                        "politique environnementale", "NGO Brussels"]
    eu_queries       = ["sustainability European Commission", "environment European Commission",
                        "ESG EU institutions Belgium", "climate policy EU Brussels",
                        "environmental officer EU agency", "European Environment Agency",
                        "EU Green Deal policy officer"]

    for q in linkedin_queries:
        print(f"[LinkedIn] '{q}'")
        jobs = scrape_linkedin(q)
        for j in jobs: j["category"] = "General"
        all_jobs.extend(jobs)
        time.sleep(3)

    for k in vdab_keywords:
        print(f"[VDAB]     '{k}'")
        jobs = scrape_vdab(k)
        for j in jobs: j["category"] = "General"
        all_jobs.extend(jobs)
        time.sleep(2)

    for q in jobat_queries:
        print(f"[Jobat]    '{q}'")
        jobs = scrape_jobat(q)
        for j in jobs: j["category"] = "General"
        all_jobs.extend(jobs)
        time.sleep(2)

    for q in stepstone_queries:
        print(f"[Stepstone] '{q}'")
        jobs = scrape_stepstone(q)
        for j in jobs: j["category"] = "General"
        all_jobs.extend(jobs)
        time.sleep(2)

    for k in actiris_keywords:
        print(f"[Actiris]  '{k}'")
        jobs = scrape_actiris(k)
        for j in jobs: j["category"] = "General"
        all_jobs.extend(jobs)
        time.sleep(2)

    for q in eu_queries:
        print(f"[LinkedIn EU] '{q}'")
        jobs = scrape_linkedin(q)
        for j in jobs: j["category"] = "EU/Policy"
        all_jobs.extend(jobs)
        time.sleep(3)

    print(f"[EuroClimateJobs]")
    jobs = scrape_euroclimatejobs()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)
    print(f"[EuroBrussels]")
    jobs = scrape_eurobrussels()
    for j in jobs: j["category"] = "EU/Policy"
    all_jobs.extend(jobs)
    time.sleep(2)
    print(f"[Brussels Sustainability Club]")
    jobs = scrape_brussels_sustainability_club()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Glassdoor]")
    jobs = scrape_glassdoor("sustainability Belgium")
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Greenjobs]")
    jobs = scrape_greenjobs()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Impactjob]")
    jobs = scrape_impactjob()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Climatebase]")
    jobs = scrape_climatebase()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Terra Incognita]")
    jobs = scrape_terraincognita()
    for j in jobs: j["category"] = "General"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[EPSO]")
    jobs = scrape_epso()
    for j in jobs: j["category"] = "EU/Policy"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[Euraxess]")
    jobs = scrape_euraxess()
    for j in jobs: j["category"] = "EU/Policy"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[EU Institutions]")
    jobs = scrape_eu_institutions()
    for j in jobs: j["category"] = "EU/Policy"
    all_jobs.extend(jobs)
    time.sleep(2)

    print(f"[UN System]")
    jobs = scrape_un_careers()
    for j in jobs: j["category"] = "UN System"
    all_jobs.extend(jobs)
    time.sleep(2)

    all_jobs = deduplicate(all_jobs)

    print(f"\n[Notion] Loading rejected companies...")
    rejected_companies = get_rejected_companies()
    if rejected_companies:
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs if j.get("company", "").lower().strip() not in rejected_companies]
        print(f"[Filter] Removed {before - len(all_jobs)} jobs from {len(rejected_companies)} rejected companies")
    print(f"\n[Filter] {len(all_jobs)} unique jobs")

    new_jobs = [j for j in all_jobs if j["id"] not in seen]
    print(f"[Filter] {len(new_jobs)} new (unseen)")

    new_jobs = [j for j in new_jobs if not is_too_senior(j)]
    print(f"[Filter] {len(new_jobs)} after seniority filter")

    new_jobs = [j for j in new_jobs if not is_too_much_experience(j)]
    print(f"[Filter] {len(new_jobs)} after experience filter (max 3 years)")

    if not new_jobs:
        print("\n[Done] No new jobs this run.")
        return

    print(f"\n[Fetch] Getting descriptions for {len(new_jobs)} jobs...")
    for j in new_jobs:
        if not j["description"]:
            j["description"] = fetch_description(j["url"])
            time.sleep(0.8)

# --- AI scoring ---
    print(f"\n[Claude] Scoring {len(new_jobs)} jobs with AI...")
    scored_jobs = score_jobs_with_claude(new_jobs)

    # --- Company careers pages (direct scraping) ---
    print(f"\n[Company scraper] Scraping company careers pages...")
    company_jobs = run_company_scraper()
    scored_jobs.extend(company_jobs)

    # --- Weekly auto-discovery of new companies ---
    maybe_discover_new_companies()

    spontaneous = []
    seen_companies = set()
    for j in scored_jobs:
        if j.get("spontaneous_worthy") and j.get("score", 0) < MIN_SCORE:
            c = j.get("company", "")
            if c and c not in seen_companies:
                seen_companies.add(c)
                spontaneous.append(j.copy())
    save_seen_jobs(seen | {j["id"] for j in scored_jobs})
    good = sum(1 for j in scored_jobs if j.get("score", 0) >= MIN_SCORE)
    print(f"\n[Results] {good} strong matches + {len(spontaneous)} spontaneous leads")
    print(f"\n[Notion] Pushing to Notion...")
    push_to_notion(scored_jobs, spontaneous)

    print(f"\n[Done] Check your Notion database for new jobs!")


if __name__ == "__main__":
    run_agent()
