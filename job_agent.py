"""
Sustainability Job Agent for Lucas Switsers de Roeck
=====================================================
Scrapes LinkedIn, Indeed, VDAB, Jobat, Stepstone, Actiris, EU/UN portals,
dedicated climate/impact job boards, and internship-focused boards
(Studentjob.be, ErasmusIntern, Idealist, ReliefWeb, Devex) for sustainability
roles AND internships in Belgium, scores with Claude AI, and pushes results
to Notion — with internships clearly tagged separately from paid vacancies.

Changes from the previous version (see CHANGES.md for the full rationale):
  1. Two-tier scraping engine: every site is first read as plain HTML and
     checked for the standard schema.org "JobPosting" structured data most
     job boards embed for Google/SEO (reliable, doesn't depend on guessing
     CSS class names). If that comes back empty — almost always because the
     site is a JavaScript single-page app (VDAB, Glassdoor, Climatebase,
     StepStone's modern UI, etc.) — the page is re-rendered with a headless
     Chromium browser (Playwright) and re-scanned the same way. This is why
     only LinkedIn was returning results before: it's one of the few sites
     here whose guest search still server-renders plain HTML.
  2. Title/role relevance filter + seniority filter (EXCLUDE_TITLE_KEYWORDS,
     ROLE_KEYWORDS, SENIOR_BLOCKLIST, MAX_YEARS_EXPERIENCE) applied BEFORE
     jobs reach Claude, so obviously-mismatched postings never make it into
     Notion. Claude's own seniority_ok/location_ok verdicts are now actually
     used to filter (previously computed but ignored).
  3. Near-duplicate postings (same company + same role, crossposted or
     reworded across boards) are merged into a single row instead of
     appearing as separate list entries.

Requires: requests, beautifulsoup4, python-dotenv, anthropic
Optional: playwright (+ `playwright install chromium`) — enables the
JS-rendering fallback in point 1. Without it the agent still runs, using
only the static-HTML + structured-data path.
"""
import json
import os
import re
import time
import datetime
import urllib.parse
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from company_scraper import run_company_scraper, maybe_discover_new_companies, is_internship

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

RUN_EVERY_DAYS = 3
SEEN_JOBS_FILE = Path("seen_jobs.json")
MIN_SCORE = 6
# Internship postings tend to have thinner descriptions to score against,
# so a slightly lower bar keeps genuinely relevant ones from being dropped.
MIN_SCORE_INTERNSHIP = 5
# Candidate profile says "max ~3-4 years experience required" — used by
# is_too_much_experience() below.
MAX_YEARS_EXPERIENCE = 4
# Set to False to skip the headless-browser fallback entirely (keeps the
# agent dependency-light, at the cost of missing every JS-only site).
ENABLE_PLAYWRIGHT_FALLBACK = True
# How many rows a duplicate-title cluster search compares within one company
# before treating two postings as "the same role" (0-1, higher = stricter).
DUPLICATE_TITLE_THRESHOLD = 0.45

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

# ── Seniority filtering ────────────────────────────────────────────────────
SENIOR_BLOCKLIST = [
    "director", "head of", "chief", "vice president",
    "senior manager", "principal", "10 years", "10+ years",
    "+10 jaar", "+15 jaar", "15 years",
]

# ── Title/role relevance filtering ─────────────────────────────────────────
# A posting must match at least one of these (in title or description) to be
# considered on-topic. Deliberately broad and multilingual (EN/NL/FR) since
# keyword searches on Belgian boards mix languages.
ROLE_KEYWORDS = [
    "sustainab", "esg", "csrd", "csr", "environment", "climate", "carbon",
    "decarboni", "net zero", "net-zero", "circular economy", "biodiversity",
    "renewable", "energy transition", "green deal", "greenhouse gas", "ghg",
    "life cycle assessment", "tcfd", "taxonomy", "conservation",
    "duurzaam", "milieu", "klimaat", "energietransitie",
    "circulaire economie", "biodiversiteit", "ecologisch",
    "durabilité", "environnement", "climat", "transition énergétique",
    "économie circulaire", "rse", "responsabilité sociétale",
    "impact", "policy officer", "beleidsmedewerker", "conseiller",
]
# No category/source is exempt from the role-relevance check below — this
# used to skip it for EU/Policy and UN System postings on the assumption
# that reaching those portals was itself a form of targeting, but that was
# exactly what let generic EU/UN admin roles with no sustainability content
# through. Marketing, project-management, policy, and consulting roles ARE
# all wanted — but only when they carry a genuine sustainability/ESG/climate
# mandate, which is what a ROLE_KEYWORDS match in the title/description
# confirms regardless of which board or portal a posting came from.

# Belgian job-board keyword search for "milieu"/"environment" frequently
# surfaces unrelated blue-collar postings (waste collection, cleaning,
# warehouse, driving) that share the keyword but nothing else. These are
# excluded outright regardless of score.
EXCLUDE_TITLE_KEYWORDS = [
    "milieustraat", "containerpark", "recyclagepark", "afvalophaler",
    "poetshulp", "poetsvrouw", "schoonmaakster", "schoonmaak",
    "chauffeur", "vrachtwagenchauffeur", "magazijnier", "orderpicker",
    "heftruckchauffeur", "productiearbeider", "productiemedewerker",
    "kassier", "kassierster", "verkoper(ster)", "kok ", "keukenmedewerker",
    "bewaker", "beveiligingsagent", "monteur", "installateur elektriciteit",
]

# Employer exclusions — regardless of how well the *role* matches, postings
# at these kinds of employers are never wanted. Keyword matching only
# catches generic terms in the company name (e.g. "Defense Systems bv");
# it will NOT catch a named contractor like "Lockheed Martin" or "Thales",
# since their names don't contain the word "defense". Add specific company
# names you come across to EXCLUDE_EMPLOYER_NAMES below — or, for anything
# that slips through into Notion, check its "Reject Company" box there,
# which filters that employer out of all future runs the same way.
EXCLUDE_EMPLOYER_KEYWORDS = [
    "defense", "defence", "weapons", "arms manufacturing", "munitions",
    "military equipment", "ammunition",
    "tobacco", "cigarette", "vaping",
    "casino", "gambling", "betting", "lottery",
]
EXCLUDE_EMPLOYER_NAMES = [
    # Add specific companies keyword-matching above won't catch, e.g.:
    # "lockheed martin", "thales", "bae systems", "rheinmetall", "dassault",
    # "philip morris", "british american tobacco", "japan tobacco",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ─── Scraping engine ──────────────────────────────────────────────────────────
# Two extraction strategies, tried in order, on whatever HTML we have:
#   1. schema.org JobPosting structured data (<script type="application/ld+json">)
#      — the format most job boards embed for Google's job-search indexing.
#      Stable across redesigns because it's driven by SEO, not layout.
#   2. Generic CSS-card scanning as a fallback for sites that don't publish
#      structured data — deliberately broad selectors since we can't verify
#      every site's exact markup ahead of time.
# If BOTH come back empty on the plain HTML, and Playwright is available,
# the page is re-rendered with headless Chromium (handles JS-only SPAs like
# VDAB) and both strategies are retried on the rendered DOM.

_playwright_ctx = {"pw": None, "browser": None, "warned": False}


def _get_browser():
    if _playwright_ctx["browser"] is not None:
        return _playwright_ctx["browser"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not _playwright_ctx["warned"]:
            print("  [Playwright not installed] JS-rendered sites will return 0 results. "
                  "Run: pip install playwright && playwright install chromium")
            _playwright_ctx["warned"] = True
        return None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        _playwright_ctx["pw"] = pw
        _playwright_ctx["browser"] = browser
        return browser
    except Exception as e:
        if not _playwright_ctx["warned"]:
            print(f"  [Playwright] could not launch Chromium ({e}). "
                  f"Try: playwright install chromium")
            _playwright_ctx["warned"] = True
        return None


def close_browser():
    if _playwright_ctx["browser"] is not None:
        try:
            _playwright_ctx["browser"].close()
            _playwright_ctx["pw"].stop()
        except Exception:
            pass
        _playwright_ctx["browser"] = None
        _playwright_ctx["pw"] = None


def render_page(url: str, wait_selector: str | None = None, wait_ms: int = 4000) -> str:
    """Render a JS-heavy page with headless Chromium and return the final HTML."""
    browser = _get_browser()
    if browser is None:
        return ""
    html = ""
    context = None
    try:
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        for text in ["Accept", "Aanvaarden", "Accepteren", "Accepter",
                     "I agree", "Alles accepteren", "OK"]:
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.I))
                if btn.count() > 0:
                    btn.first.click(timeout=1200)
                    break
            except Exception:
                pass
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=8000)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
        html = page.content()
    except Exception as e:
        print(f"    [Playwright] render failed for {url}: {e}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
    return html


def _parse_jsonld_jobpostings(soup: BeautifulSoup, source_name: str,
                               default_location: str, internship: bool) -> list[dict]:
    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for it in items:
            if isinstance(it, dict) and "@graph" in it and isinstance(it["@graph"], list):
                expanded.extend(it["@graph"])
            else:
                expanded.append(it)
        for item in expanded:
            if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                continue
            title = (item.get("title") or "").strip()
            org = item.get("hiringOrganization")
            company = org.get("name", "") if isinstance(org, dict) else (org or "")
            loc_field = item.get("jobLocation")
            location = ""
            if isinstance(loc_field, list) and loc_field:
                loc_field = loc_field[0]
            if isinstance(loc_field, dict):
                addr = loc_field.get("address", {})
                if isinstance(addr, dict):
                    location = ", ".join(filter(None, [
                        addr.get("addressLocality", ""), addr.get("addressCountry", "")]))
            url = item.get("url") or ""
            desc_html = item.get("description", "") or ""
            description = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)[:2000]
            date_posted = item.get("datePosted", "")
            if title and url:
                jobs.append({"id": url + title, "title": title, "company": company,
                             "location": location or default_location, "url": url,
                             "description": description, "date_posted": date_posted,
                             "source": source_name, "is_internship": internship})
    return jobs


GENERIC_CARD_SELECTORS = [
    "div[class*='job-card']", "article[class*='job']", "li[class*='job']",
    "div[class*='vacancy']", "div[class*='vacature']", "div[class*='offer']",
    "div[class*='listing']", "li.job-item", "div.job", "article",
]
GENERIC_TITLE_SELECTOR = "h1, h2, h3, [class*='title']"
GENERIC_COMPANY_SELECTOR = "[class*='company'], [class*='employer'], [class*='organization']"


def _parse_css_cards(soup: BeautifulSoup, base_url: str, source_name: str,
                      default_location: str, internship: bool,
                      card_selector: str | None = None) -> list[dict]:
    jobs = []
    selectors = [card_selector] if card_selector else GENERIC_CARD_SELECTORS
    for sel in selectors:
        cards = soup.select(sel)
        if not cards:
            continue
        for card in cards[:30]:
            title_el = card.select_one(GENERIC_TITLE_SELECTOR) or (card if card.name in ("h1", "h2", "h3") else None)
            company_el = card.select_one(GENERIC_COMPANY_SELECTOR)
            link_el = card if (card.name == "a" and card.get("href")) else card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else (
                card.get_text(strip=True)[:120] if not title_el and card.name == "a" else "")
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if href and not href.startswith("http"):
                href = base_url.rstrip("/") + "/" + href.lstrip("/")
            if title and href and len(title) < 150:
                jobs.append({"id": href + title, "title": title, "company": company,
                             "location": default_location, "url": href, "description": "",
                             "date_posted": "", "source": source_name, "is_internship": internship})
        if jobs:
            break
    return jobs


def generic_scrape(source_name: str, url: str, base_url: str, default_location: str = "Belgium",
                    card_selector: str | None = None, wait_selector: str | None = None,
                    internship: bool = False, wait_ms: int = 4000) -> list[dict]:
    """Fetch `url`, try structured-data then CSS-card extraction; if both
    come back empty, re-render with headless Chromium and try again."""
    jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = _parse_jsonld_jobpostings(soup, source_name, default_location, internship)
        if not jobs:
            jobs = _parse_css_cards(soup, base_url, source_name, default_location, internship, card_selector)
    except Exception as e:
        print(f"  [{source_name} error - static fetch]: {e}")

    recovered_via_js = False
    if not jobs and ENABLE_PLAYWRIGHT_FALLBACK:
        html = render_page(url, wait_selector=wait_selector, wait_ms=wait_ms)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            jobs = _parse_jsonld_jobpostings(soup, source_name, default_location, internship)
            if not jobs:
                jobs = _parse_css_cards(soup, base_url, source_name, default_location, internship, card_selector)
            recovered_via_js = bool(jobs)

    label = source_name + (" (internship)" if internship else "")
    if jobs:
        suffix = " [recovered via JS rendering]" if recovered_via_js else ""
        print(f"  -> {len(jobs)} from {label}{suffix}")
    else:
        print(f"  -> 0 from {label} — site structure may have changed, or it's blocking "
              f"automated requests. If this stays at 0, share the page's HTML so the "
              f"selectors can be corrected.")
    return jobs


# ─── Site-specific scrapers ────────────────────────────────────────────────────
# LinkedIn keeps its own function: its guest job search still server-renders
# plain HTML (which is why it was the only thing working before), and its
# f_E experience-level filter distinguishes internships from entry-level at
# the source rather than relying only on keyword guessing afterwards.
def scrape_linkedin(query: str, internship: bool = False) -> list[dict]:
    jobs = []
    try:
        url = "https://www.linkedin.com/jobs/search/"
        params = {
            "keywords": query,
            "location": "Belgium",
            "f_TPR": "r604800",
            "f_E": "1" if internship else "1,2",
            "start": 0,
        }
        full_url = url + "?" + urllib.parse.urlencode(params)
        resp = requests.get(full_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = _parse_jsonld_jobpostings(soup, "LinkedIn", "Belgium", internship)
        if not jobs:
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
                                 "date_posted": "", "source": "LinkedIn",
                                 "is_internship": internship})
        if not jobs and ENABLE_PLAYWRIGHT_FALLBACK:
            html = render_page(full_url, wait_selector="div.base-card, li.jobs-search-results__list-item")
            if html:
                soup = BeautifulSoup(html, "html.parser")
                jobs = _parse_jsonld_jobpostings(soup, "LinkedIn", "Belgium", internship)
                if not jobs:
                    cards = soup.select("div.job-search-card, li.jobs-search-results__list-item, div.base-card")
                    for card in cards[:20]:
                        title_el = card.select_one("h3.base-search-card__title, h3, .job-title")
                        company_el = card.select_one("h4.base-search-card__subtitle, h4")
                        link_el = card.select_one("a[href*='/jobs/view/']")
                        title = title_el.get_text(strip=True) if title_el else ""
                        company = company_el.get_text(strip=True) if company_el else ""
                        link = link_el["href"].split("?")[0] if link_el else ""
                        if title and link:
                            jobs.append({"id": link + title, "title": title, "company": company,
                                         "location": "Belgium", "url": link, "description": "",
                                         "date_posted": "", "source": "LinkedIn",
                                         "is_internship": internship})
        print(f"  -> {len(jobs)} from LinkedIn" + (" (internship)" if internship else ""))
    except Exception as e:
        print(f"  [LinkedIn error]: {e}")
    return jobs


def scrape_indeed(query: str, internship: bool = False) -> list[dict]:
    params = {"q": query, "l": "Belgium", "fromage": str(RUN_EVERY_DAYS * 2), "sort": "date"}
    if internship:
        params["jt"] = "internship"
    url = "https://be.indeed.com/jobs?" + urllib.parse.urlencode(params)
    return generic_scrape("Indeed", url, "https://be.indeed.com",
                           card_selector="div.job_seen_beacon, div.jobsearch-SerpJobCard, td.resultContent",
                           wait_selector="div.job_seen_beacon, td.resultContent",
                           internship=internship)


def scrape_vdab(keyword: str, internship: bool = False) -> list[dict]:
    """VDAB's search results only appear after client-side JS runs a query —
    a plain fetch shows an empty 'start your search' shell (confirmed by
    inspection), so this always needs the Playwright render path."""
    url = "https://www.vdab.be/vindeenjob/vacatures?" + urllib.parse.urlencode({"trefwoord": keyword})
    return generic_scrape("VDAB", url, "https://www.vdab.be",
                           wait_selector="[class*='vacature'], [class*='job'], [class*='result']",
                           wait_ms=6000, internship=internship)


def scrape_jobat(query: str, internship: bool = False) -> list[dict]:
    url = "https://www.jobat.be/en/jobs?" + urllib.parse.urlencode({"q": query, "r": "BE", "sort": "date"})
    return generic_scrape("Jobat", url, "https://www.jobat.be",
                           wait_selector="article, [class*='job']", internship=internship)


def scrape_stepstone(query: str, internship: bool = False) -> list[dict]:
    url = f"https://www.stepstone.be/jobs/{urllib.parse.quote(query)}/in-belgium"
    return generic_scrape("Stepstone", url, "https://www.stepstone.be",
                           card_selector="article[data-at='job-item'], div.job-ad, li[class*='job']",
                           wait_selector="article, [data-at='job-item']", internship=internship)


def scrape_actiris(keyword: str, internship: bool = False) -> list[dict]:
    url = "https://www.actiris.brussels/en/citizens/find-a-job/?" + urllib.parse.urlencode({"q": keyword})
    return generic_scrape("Actiris", url, "https://www.actiris.brussels",
                           default_location="Brussels, Belgium",
                           wait_selector="article, [class*='offer']", internship=internship)


def scrape_glassdoor(query: str) -> list[dict]:
    url = "https://www.glassdoor.com/Job/belgium-sustainability-jobs-SRCH_IL.0,7_IN25_KO8,22.htm"
    return generic_scrape("Glassdoor", url, "https://www.glassdoor.com",
                           card_selector="li[data-test='jobListing'], div.jobCard, article.job",
                           wait_selector="li[data-test='jobListing']")


def scrape_epso() -> list[dict]:
    url = "https://epso.europa.eu/en/job-opportunities/open-for-application"
    return generic_scrape("EPSO", url, "https://epso.europa.eu",
                           default_location="Brussels, Belgium",
                           wait_selector="article, [class*='job']")


def scrape_euraxess() -> list[dict]:
    url = "https://euraxess.ec.europa.eu/jobs/search?f[0]=field_job_country:Belgium"
    return generic_scrape("Euraxess", url, "https://euraxess.ec.europa.eu",
                           wait_selector="article, .views-row")


def scrape_un_careers() -> list[dict]:
    jobs = []
    institutions = [
        ("UNDP", "https://jobs.undp.org/cj_view_jobs.cfm?job_country=Belgium"),
        ("UNEP", "https://www.unep.org/about-un-environment/employment-opportunities"),
        ("UNICEF", "https://www.unicef.org/careers/search?location=Belgium"),
        ("WFP", "https://career5.successfactors.eu/career?company=C0000168410P&site=Belgium"),
        ("ILO", "https://jobs.ilo.org/job-search-results/?locations=Belgium"),
        ("UNESCO", "https://careers.unesco.org/go/International-Professional-Posts/3803102/"),
        # careers.iom.int no longer resolves (confirmed by DNS failure in a
        # live run) — IOM moved to an Oracle Cloud recruiting portal, which
        # is itself a JS-heavy app and will need the Playwright fallback.
        ("IOM", "https://fa-evlj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"),
        ("UNFPA", "https://www.unfpa.org/jobs"),
    ]
    for org_name, url in institutions:
        parsed = urllib.parse.urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        found = generic_scrape(f"UN Careers ({org_name})", url, base_url,
                                default_location="Brussels, Belgium",
                                wait_selector="article, tr, li")
        for j in found:
            j["source"] = "UN Careers"
            j["company"] = j.get("company") or org_name
        jobs.extend(found)
        time.sleep(1)
    # Also search LinkedIn for UN roles (kept from the original approach,
    # limited to avoid rate-limiting a single source too hard)
    un_orgs = ["UNDP", "UNEP", "UNICEF", "WFP", "ILO", "UNESCO", "UN Women"]
    for org in un_orgs:
        found = scrape_linkedin(f"{org} Belgium sustainability environment")
        for j in found:
            j["company"] = j.get("company") or org
        jobs.extend(found)
        time.sleep(2)
    print(f"  -> {len(jobs)} from UN System (total)")
    return jobs


def scrape_eu_institutions() -> list[dict]:
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
        parsed = urllib.parse.urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        found = generic_scrape(f"EU Institutions ({org_name})", url, base_url,
                                default_location="Brussels, Belgium",
                                wait_selector="article, tr, li")
        for j in found:
            j["source"] = "EU Institutions"
            j["company"] = j.get("company") or org_name
        jobs.extend(found)
        time.sleep(1)
    print(f"  -> {len(jobs)} from EU Institutions (total)")
    return jobs


def scrape_studentjob(query: str) -> list[dict]:
    url = "https://www.studentjob.be/en/vacatures?" + urllib.parse.urlencode({"keyword": query})
    return generic_scrape("Studentjob.be", url, "https://www.studentjob.be",
                           wait_selector="article, [class*='vacature']", internship=True)


def scrape_erasmusintern() -> list[dict]:
    url = ("https://erasmusintern.org/internship-search?"
           + urllib.parse.urlencode({"field_country_target": "Belgium"}))
    return generic_scrape("ErasmusIntern", url, "https://erasmusintern.org",
                           wait_selector=".views-row, article", internship=True)


def scrape_idealist(query: str) -> list[dict]:
    url = "https://www.idealist.org/en/jobs?" + urllib.parse.urlencode({"q": query, "loc": "Belgium"})
    return generic_scrape("Idealist", url, "https://www.idealist.org",
                           wait_selector="article, [class*='listing']")


def scrape_reliefweb() -> list[dict]:
    url = "https://reliefweb.int/jobs?" + urllib.parse.urlencode({"search": "Belgium"})
    return generic_scrape("ReliefWeb", url, "https://reliefweb.int",
                           wait_selector="article, [class*='job']")


def scrape_devex(query: str) -> list[dict]:
    url = "https://www.devex.com/jobs/search?" + urllib.parse.urlencode({"query": query, "location": "Belgium"})
    return generic_scrape("Devex", url, "https://www.devex.com",
                           wait_selector="article, [class*='job-listing']")


# ── One-URL boutique climate/impact boards — data-driven instead of ~20
# near-identical hand-written functions, all routed through the same
# structured-data-first / JS-render-fallback engine above. ──────────────────
BOUTIQUE_SITES = [
    {"name": "inClimate", "url": "https://inclimate.org/jobs",
     "base_url": "https://inclimate.org", "location": "Europe", "category": "General"},
    {"name": "Green Jobs Network", "url": "https://www.greenjobs.com/jobs/?location=Europe",
     "base_url": "https://www.greenjobs.com", "location": "Europe", "category": "General"},
    # carbonremoval.jobs no longer resolves (confirmed by DNS failure in a
    # live run) — the board appears to have moved/rebranded to cdrjobs.earth.
    {"name": "Carbon Removal Jobs", "url": "https://www.cdrjobs.earth/",
     "base_url": "https://www.cdrjobs.earth", "location": "Belgium", "category": "General"},
    {"name": "Koolenindustries", "url": "https://koolenindustries.com/jobs",
     "base_url": "https://koolenindustries.com", "location": "Europe", "category": "General"},
    {"name": "EuroClimateJobs", "url": "https://www.euroclimatejobs.com/jobs/belgium",
     "base_url": "https://www.euroclimatejobs.com", "location": "Belgium", "category": "General"},
    {"name": "EuroBrussels", "url": "https://www.eurobrussels.com/jobs/environment",
     "base_url": "https://www.eurobrussels.com", "location": "Brussels, Belgium", "category": "EU/Policy"},
    {"name": "Brussels Sustainability Club", "url": "https://brusselssustainabilityclub.com/jobs/",
     "base_url": "https://brusselssustainabilityclub.com", "location": "Brussels, Belgium", "category": "General"},
    {"name": "Greenjobs.nl", "url": "https://greenjobs.nl/en/sustainable-jobs/?location=Belgium",
     "base_url": "https://greenjobs.nl", "location": "Belgium", "category": "General"},
    {"name": "Impactpool", "url": "https://www.impactpool.org/countries/Belgium",
     "base_url": "https://www.impactpool.org", "location": "Belgium", "category": "UN System"},
    {"name": "BeImpact", "url": "https://www.be-impact.org/jobs",
     "base_url": "https://www.be-impact.org", "location": "Belgium", "category": "General"},
    {"name": "Climatebase", "url": "https://climatebase.org/jobs?l=Belgium&q=sustainability",
     "base_url": "https://climatebase.org", "location": "Belgium", "category": "General"},
    {"name": "Terra Incognita", "url": "https://www.terraincognita.be/jobs",
     "base_url": "https://www.terraincognita.be", "location": "Belgium", "category": "General"},
]


def scrape_boutique_sites() -> list[dict]:
    jobs = []
    for site in BOUTIQUE_SITES:
        print(f"[{site['name']}]")
        found = generic_scrape(site["name"], site["url"], site["base_url"],
                                default_location=site["location"],
                                wait_selector="article, [class*='job']")
        for j in found:
            j["category"] = site["category"]
        jobs.extend(found)
        time.sleep(2)
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
    text = ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in ["div.description__text", "div.jobsearch-jobDescriptionText",
                         "div#jobDescriptionText", "div.job-description"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator=" ", strip=True)[:2000]
                break
        if not text:
            main = soup.select_one("main, article")
            if main:
                text = main.get_text(separator=" ", strip=True)[:2000]
    except Exception:
        pass
    # Many description pages are also JS-rendered; if the static fetch came
    # back too thin to score against, retry once with headless Chromium.
    if len(text) < 100 and ENABLE_PLAYWRIGHT_FALLBACK:
        html = render_page(url, wait_ms=3000)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for selector in ["div.description__text", "div.jobsearch-jobDescriptionText",
                             "div#jobDescriptionText", "div.job-description", "main", "article"]:
                el = soup.select_one(selector)
                if el:
                    candidate = el.get_text(separator=" ", strip=True)[:2000]
                    if len(candidate) > len(text):
                        text = candidate
                    break
    return text


# ─── Title / seniority / relevance filters ─────────────────────────────────────
def is_too_much_experience(job: dict) -> bool:
    """Return True if the job explicitly requires more than MAX_YEARS_EXPERIENCE years."""
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    patterns = [
        r'(\d+)\s*\+?\s*years?\s*(of\s*)?(experience|exp)',
        r'(\d+)\s*\+?\s*jaar\s*(ervaring|werkervaring)',
        r'minimum\s*(\d+)\s*(years?|jaar)',
        r'at\s*least\s*(\d+)\s*years?',
        r'minstens\s*(\d+)\s*jaar',
        r'(\d+)\s*to\s*\d+\s*years?\s*(of\s*)?experience',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            years = int(match[0]) if match[0].isdigit() else 0
            if years > MAX_YEARS_EXPERIENCE:
                return True
    return False


def is_too_senior(job: dict) -> bool:
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    return any(kw.lower() in text for kw in SENIOR_BLOCKLIST)


def is_offtopic_title(job: dict) -> bool:
    """Cheap title-only check applied before description fetch — catches the
    blue-collar/unrelated postings that keyword search on 'milieu' etc. pulls in."""
    title = job.get("title", "").lower()
    return any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS)


def is_excluded_employer(job: dict) -> bool:
    """Hard employer exclusion, independent of role fit — e.g. a perfectly
    on-topic ESG role at a defense contractor is still excluded."""
    company = (job.get("company", "") or "").lower()
    if not company:
        return False
    if any(name in company for name in EXCLUDE_EMPLOYER_NAMES):
        return True
    return any(kw in company for kw in EXCLUDE_EMPLOYER_KEYWORDS)


def has_role_relevance(job: dict) -> bool:
    """Final relevance check once a description is available. Applies to
    EVERY posting regardless of source — including EU institution and UN
    agency portals, which are otherwise a common source of generic
    admin/policy roles with no actual sustainability content."""
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    return any(kw in text for kw in ROLE_KEYWORDS)


# ─── AI Scoring ───────────────────────────────────────────────────────────────
def score_jobs_with_claude(jobs: list[dict]) -> list[dict]:
    if not ANTHROPIC_API_KEY:
        print("  [Warning] No ANTHROPIC_API_KEY — skipping AI scoring.")
        for j in jobs:
            j.update({"score": 7, "reasoning": "AI scoring skipped", "match_highlights": [],
                      "seniority_ok": True, "location_ok": True, "spontaneous_worthy": False})
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
Likely type so far: {"Internship/traineeship" if j.get('is_internship') else "Unknown — please judge from title/description"}
Description: {j['description'][:600] or '(none)'}
"""
        prompt = f"""You are a career advisor helping a sustainability candidate find jobs AND internships in Belgium.
CANDIDATE PROFILE:
{CANDIDATE_PROFILE}
JOBS TO EVALUATE:
{job_list_text}
Respond ONLY with a valid JSON array. Each element:
- "job_index": integer (1-based)
- "score": integer 1-10 (score internships on relevance/fit the same way you would a job — do not penalize just for being an internship)
- "reasoning": string (2-3 sentences)
- "match_highlights": list of up to 3 short strings
- "seniority_ok": boolean — false if this role clearly needs more seniority/experience than the candidate has
- "location_ok": boolean — false if this role is clearly not based in or reachable from Belgium
- "spontaneous_worthy": boolean
- "is_internship": boolean — true if this posting is an internship, traineeship, "stage", or similar unpaid/training placement rather than a regular paid position
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
                        "location_ok": s.get("location_ok", True),
                        "spontaneous_worthy": s.get("spontaneous_worthy", False),
                        "deadline": s.get("deadline", ""),
                    })
                    batch[idx]["is_internship"] = bool(batch[idx].get("is_internship")) or bool(s.get("is_internship", False))
        except Exception as e:
            print(f"  [Claude error]: {e}")
            for j in batch:
                j.setdefault("score", 5)
                j.setdefault("reasoning", "Scoring failed")
                j.setdefault("match_highlights", [])
                j.setdefault("seniority_ok", True)
                j.setdefault("location_ok", True)
                j.setdefault("spontaneous_worthy", False)
        scored.extend(batch)
        time.sleep(1)
    return scored


# ─── Notion Integration ───────────────────────────────────────────────────────
def ensure_notion_schema() -> dict:
    """Make sure the Notion database has the extra properties this version
    relies on: 'Employment Type' (Paid Job / Internship) and 'Also Posted On'
    (links to the other listings a merged duplicate cluster came from).
    Idempotent — safe to call on every run. Returns which properties are
    confirmed to exist, so callers can skip setting an unknown one — sending
    an unrecognized property to Notion fails the ENTIRE page, not just that
    field. If the integration can't alter the schema, this just logs and
    moves on; the title-prefix still makes internships obvious."""
    available = {"Employment Type": False, "Also Posted On": False}
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        return available
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    try:
        resp = requests.get(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
                             headers=headers, timeout=15)
        resp.raise_for_status()
        existing_props = resp.json().get("properties", {})
        patch_props = {}
        if "Employment Type" in existing_props:
            available["Employment Type"] = True
        else:
            patch_props["Employment Type"] = {
                "select": {"options": [
                    {"name": "💼 Paid Job", "color": "green"},
                    {"name": "🎓 Internship", "color": "purple"},
                ]}
            }
        if "Also Posted On" in existing_props:
            available["Also Posted On"] = True
        else:
            patch_props["Also Posted On"] = {"rich_text": {}}
        if patch_props:
            patch_resp = requests.patch(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
                                         headers=headers, json={"properties": patch_props}, timeout=15)
            if patch_resp.status_code == 200:
                for name in patch_props:
                    available[name] = True
                    print(f"  [Notion] Added '{name}' property to database schema.")
            else:
                print(f"  [Notion] Could not add {list(patch_props)} automatically "
                      f"(add these properties manually if you want them): {patch_resp.text[:200]}")
    except Exception as e:
        print(f"  [Notion] Could not verify/update database schema: {e}")
    return available


def push_to_notion(jobs: list[dict], spontaneous: list[dict]):
    """Push scored jobs to a Notion database."""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        print("  [Notion] No credentials set — skipping Notion push.")
        return
    schema_available = ensure_notion_schema()
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    def _threshold(j: dict) -> int:
        return MIN_SCORE_INTERNSHIP if j.get("is_internship") else MIN_SCORE

    all_to_push = [(j, False) for j in jobs if j.get("score", 0) >= _threshold(j)]
    all_to_push += [(j, True) for j in spontaneous]
    # Reverse so highest scored jobs end up at top of Notion
    all_to_push = sorted(all_to_push, key=lambda x: x[0].get("score", 0))
    pushed = 0
    for job, is_spont in all_to_push:
        highlights = " | ".join(job.get("match_highlights", []))
        job_type = "Spontaneous" if is_spont else "Vacancy"
        score = job.get("score", 0)
        is_intern = bool(job.get("is_internship"))
        title_text = job.get("title", "")
        if is_intern:
            title_text = f"🎓 [INTERNSHIP] {title_text}"
        properties = {
            "Job Title": {"title": [{"text": {"content": title_text}}]},
            "Company": {"rich_text": [{"text": {"content": job.get("company", "")}}]},
            "Location": {"rich_text": [{"text": {"content": job.get("location", "")}}]},
            "Source": {"select": {"name": job.get("source", "Other")}},
            "Score": {"select": {"name": "⭐⭐⭐ Excellent" if score >= 8 else "⭐⭐ Good" if score >= 6 else "⭐ Moderate"}},
            "Type": {"select": {"name": job_type}},
            "URL": {"url": job.get("url", "") or None},
            "Category": {"select": {"name": job.get("category", "General")}},
            "Reasoning": {"rich_text": [{"text": {"content": job.get("reasoning", "")[:2000]}}]},
            "Highlights": {"rich_text": [{"text": {"content": highlights[:2000]}}]},
            "Date Found": {"date": {"start": datetime.datetime.now().strftime("%Y-%m-%d")}},
            "Status": {"select": {"name": "New"}},
        }
        if schema_available.get("Employment Type"):
            properties["Employment Type"] = {
                "select": {"name": "🎓 Internship" if is_intern else "💼 Paid Job"}
            }
        duplicates = job.get("duplicate_listings", [])
        if duplicates and schema_available.get("Also Posted On"):
            rich = []
            for i, d in enumerate(duplicates[:8]):
                if i > 0:
                    rich.append({"text": {"content": " · "}})
                link = {"url": d["url"]} if d.get("url") else None
                rich.append({"text": {"content": d.get("source", "another board") or "another board",
                                       "link": link}})
            properties["Also Posted On"] = {"rich_text": rich}
        deadline = job.get("deadline", "")
        if deadline:
            properties["Deadline"] = {"date": {"start": deadline}}
        payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
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


def deduplicate(jobs: list[dict]) -> list[dict]:
    """Exact dedup pass: same URL+title scraped twice (e.g. appearing on two
    pages of the same search). Cheap first pass before the fuzzy merge below."""
    seen_ids, result = set(), []
    for j in jobs:
        if j["id"] not in seen_ids:
            seen_ids.add(j["id"])
            result.append(j)
    return result


_COMPANY_SUFFIXES = {
    "nv", "bv", "bvba", "sa", "sprl", "vzw", "asbl", "cvba", "cv", "vof",
    "gmbh", "ltd", "llc", "inc", "plc", "group", "belgium", "belgie", "belgique",
}
_TITLE_STOPWORDS = {
    "the", "a", "an", "and", "of", "at", "for", "in", "to", "with", "or",
    "de", "het", "van", "voor", "bij", "en", "la", "le", "les", "des", "du", "pour", "dans",
}


def _normalize_company(name: str) -> str:
    name = (name or "").lower()
    name = re.sub(r"[^\w\s]", " ", name)
    tokens = [t for t in name.split() if t not in _COMPANY_SUFFIXES]
    return " ".join(tokens).strip()


def _title_tokens(title: str) -> set:
    title = (title or "").lower()
    title = re.sub(r"[^\w\s]", " ", title)
    # Drop single-character tokens too — Belgian postings routinely append
    # gender-neutral suffixes like "(m/f/x)" or "(h/f/x)", which would
    # otherwise dilute the similarity score between two titles that are
    # actually the same role.
    return {t for t in title.split() if len(t) > 1 and t not in _TITLE_STOPWORDS}


def _title_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def merge_duplicates(jobs: list[dict], threshold: float = DUPLICATE_TITLE_THRESHOLD) -> list[dict]:
    """Group postings that are the same role at the same company — crossposted
    to multiple boards, or reworded slightly between them — into one row
    instead of letting each copy crowd the list separately. The richest
    (longest-description) copy becomes the canonical row; the others are
    recorded as 'duplicate_listings' and surfaced via the 'Also Posted On'
    Notion column, so nothing is silently dropped."""
    buckets: dict[str, list[dict]] = {}
    for j in jobs:
        key = _normalize_company(j.get("company", ""))
        buckets.setdefault(key, []).append(j)

    merged: list[dict] = []
    for company_key, group in buckets.items():
        if not company_key:
            # Can't safely cluster postings with no parsed company name —
            # merging on title alone risks combining unrelated employers.
            merged.extend(group)
            continue
        clusters: list[list[dict]] = []
        cluster_tokens: list[set] = []
        for j in group:
            tok = _title_tokens(j.get("title", ""))
            placed = False
            for ci, ctok in enumerate(cluster_tokens):
                if _title_similarity(tok, ctok) >= threshold:
                    clusters[ci].append(j)
                    cluster_tokens[ci] = ctok | tok
                    placed = True
                    break
            if not placed:
                clusters.append([j])
                cluster_tokens.append(tok)
        for cluster in clusters:
            if len(cluster) == 1:
                merged.append(cluster[0])
                continue
            original = max(cluster, key=lambda j: len(j.get("description", "") or ""))
            others = [j for j in cluster if j is not original]
            canonical = original.copy()
            canonical["duplicate_listings"] = [
                {"source": o.get("source", ""), "url": o.get("url", ""), "title": o.get("title", "")}
                for o in others
            ]
            merged.append(canonical)
    return merged


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
    indeed_queries   = ["sustainability Belgium", "ESG CSRD Belgium", "environmental Belgium",
                        "duurzaamheid", "milieu adviseur"]

    linkedin_internship_queries = ["sustainability internship Belgium", "ESG internship Belgium",
                        "CSRD internship Belgium", "environmental internship Belgium",
                        "climate internship Belgium", "stage duurzaamheid",
                        "stage milieu", "stage CSRD", "sustainability trainee Belgium"]
    vdab_internship_keywords = ["stage duurzaamheid", "stage milieu", "stage CSRD",
                        "afstudeerstage duurzaamheid", "stage klimaat"]
    jobat_internship_queries = ["stage duurzaamheid", "internship sustainability",
                        "stage milieu", "trainee ESG", "stage NGO"]
    stepstone_internship_queries = ["internship sustainability Belgium", "stage duurzaamheid",
                        "traineeship ESG Belgium"]
    actiris_internship_keywords = ["stage duurzaamheid", "internship sustainability",
                        "stage environnement"]
    indeed_internship_queries = ["sustainability internship Belgium", "ESG internship Belgium",
                        "stage duurzaamheid"]
    studentjob_queries = ["duurzaamheid", "sustainability", "ESG", "milieu", "CSRD"]
    idealist_queries = ["sustainability", "environment", "climate"]
    devex_queries = ["sustainability", "environment", "climate"]

    def _extend(jobs, category):
        for j in jobs:
            j["category"] = category
        all_jobs.extend(jobs)

    for q in linkedin_queries:
        print(f"[LinkedIn] '{q}'")
        _extend(scrape_linkedin(q), "General")
        time.sleep(3)
    for q in linkedin_internship_queries:
        print(f"[LinkedIn Internship] '{q}'")
        _extend(scrape_linkedin(q, internship=True), "General")
        time.sleep(3)
    for k in vdab_keywords:
        print(f"[VDAB]     '{k}'")
        _extend(scrape_vdab(k), "General")
        time.sleep(2)
    for k in vdab_internship_keywords:
        print(f"[VDAB Internship] '{k}'")
        _extend(scrape_vdab(k, internship=True), "General")
        time.sleep(2)
    for q in jobat_queries:
        print(f"[Jobat]    '{q}'")
        _extend(scrape_jobat(q), "General")
        time.sleep(2)
    for q in jobat_internship_queries:
        print(f"[Jobat Internship] '{q}'")
        _extend(scrape_jobat(q, internship=True), "General")
        time.sleep(2)
    for q in stepstone_queries:
        print(f"[Stepstone] '{q}'")
        _extend(scrape_stepstone(q), "General")
        time.sleep(2)
    for q in stepstone_internship_queries:
        print(f"[Stepstone Internship] '{q}'")
        _extend(scrape_stepstone(q, internship=True), "General")
        time.sleep(2)
    for k in actiris_keywords:
        print(f"[Actiris]  '{k}'")
        _extend(scrape_actiris(k), "General")
        time.sleep(2)
    for k in actiris_internship_keywords:
        print(f"[Actiris Internship] '{k}'")
        _extend(scrape_actiris(k, internship=True), "General")
        time.sleep(2)
    for q in indeed_queries:
        print(f"[Indeed]   '{q}'")
        _extend(scrape_indeed(q), "General")
        time.sleep(2)
    for q in indeed_internship_queries:
        print(f"[Indeed Internship] '{q}'")
        _extend(scrape_indeed(q, internship=True), "General")
        time.sleep(2)
    for q in studentjob_queries:
        print(f"[Studentjob.be] '{q}'")
        _extend(scrape_studentjob(q), "General")
        time.sleep(2)
    print(f"[ErasmusIntern]")
    _extend(scrape_erasmusintern(), "General")
    time.sleep(2)
    for q in idealist_queries:
        print(f"[Idealist] '{q}'")
        _extend(scrape_idealist(q), "General")
        time.sleep(2)
    print(f"[ReliefWeb]")
    _extend(scrape_reliefweb(), "UN System")
    time.sleep(2)
    for q in devex_queries:
        print(f"[Devex] '{q}'")
        _extend(scrape_devex(q), "General")
        time.sleep(2)
    for q in eu_queries:
        print(f"[LinkedIn EU] '{q}'")
        _extend(scrape_linkedin(q), "EU/Policy")
        time.sleep(3)

    _extend(scrape_boutique_sites(), "General")  # category is overwritten per-site inside

    print(f"[Glassdoor]")
    _extend(scrape_glassdoor("sustainability Belgium"), "General")
    time.sleep(2)
    print(f"[EPSO]")
    _extend(scrape_epso(), "EU/Policy")
    time.sleep(2)
    print(f"[Euraxess]")
    _extend(scrape_euraxess(), "EU/Policy")
    time.sleep(2)
    print(f"[EU Institutions]")
    _extend(scrape_eu_institutions(), "EU/Policy")
    time.sleep(2)
    print(f"[UN System]")
    _extend(scrape_un_careers(), "UN System")
    time.sleep(2)

    all_jobs = deduplicate(all_jobs)
    for j in all_jobs:
        j["is_internship"] = is_internship(j)

    print(f"\n[Merge] Grouping crossposted/duplicate listings...")
    before_merge = len(all_jobs)
    all_jobs = merge_duplicates(all_jobs)
    print(f"[Merge] {before_merge} -> {len(all_jobs)} rows after merging duplicates")

    print(f"\n[Notion] Loading rejected companies...")
    rejected_companies = get_rejected_companies()
    if rejected_companies:
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs if j.get("company", "").lower().strip() not in rejected_companies]
        print(f"[Filter] Removed {before - len(all_jobs)} jobs from {len(rejected_companies)} rejected companies")

    print(f"\n[Filter] {len(all_jobs)} unique jobs")
    new_jobs = [j for j in all_jobs if j["id"] not in seen]
    print(f"[Filter] {len(new_jobs)} new (unseen)")

    new_jobs = [j for j in new_jobs if not is_offtopic_title(j)]
    print(f"[Filter] {len(new_jobs)} after off-topic title filter")

    new_jobs = [j for j in new_jobs if not is_excluded_employer(j)]
    print(f"[Filter] {len(new_jobs)} after excluded-employer filter")

    new_jobs = [j for j in new_jobs if j.get("is_internship") or not is_too_senior(j)]
    print(f"[Filter] {len(new_jobs)} after seniority filter")
    new_jobs = [j for j in new_jobs if j.get("is_internship") or not is_too_much_experience(j)]
    print(f"[Filter] {len(new_jobs)} after experience filter (max {MAX_YEARS_EXPERIENCE} years)")

    if not new_jobs:
        print("\n[Done] No new jobs this run.")
        return

    print(f"\n[Fetch] Getting descriptions for {len(new_jobs)} jobs...")
    for j in new_jobs:
        if not j["description"]:
            j["description"] = fetch_description(j["url"])
            time.sleep(0.8)
        if not j["is_internship"]:
            j["is_internship"] = is_internship(j)

    new_jobs = [j for j in new_jobs if has_role_relevance(j)]
    print(f"[Filter] {len(new_jobs)} after role/title relevance filter")
    if not new_jobs:
        print("\n[Done] No new jobs this run.")
        return

    print(f"\n[Claude] Scoring {len(new_jobs)} jobs with AI...")
    scored_jobs = score_jobs_with_claude(new_jobs)
    scored_jobs = [j for j in scored_jobs if j.get("seniority_ok", True) and j.get("location_ok", True)]
    print(f"[Filter] {len(scored_jobs)} after Claude's own seniority/location check")

    print(f"\n[Company scraper] Scraping company careers pages...")
    company_jobs = run_company_scraper()
    # These previously skipped every filter above (off-topic title,
    # excluded employer, role relevance) since they're added straight into
    # scored_jobs. Whatever company_scraper.py's own target list looks like,
    # the hard employer/role checks should still apply the same way they do
    # to every other source.
    before_company_filter = len(company_jobs)
    company_jobs = [j for j in company_jobs if not is_excluded_employer(j)]
    company_jobs = [j for j in company_jobs if has_role_relevance(j)]
    if before_company_filter:
        print(f"  [Company scraper] {before_company_filter} -> {len(company_jobs)} "
              f"after employer/relevance filters")
    scored_jobs.extend(company_jobs)

    maybe_discover_new_companies()

    spontaneous = []
    seen_companies = set()
    for j in scored_jobs:
        threshold = MIN_SCORE_INTERNSHIP if j.get("is_internship") else MIN_SCORE
        if j.get("spontaneous_worthy") and j.get("score", 0) < threshold:
            c = j.get("company", "")
            if c and c not in seen_companies:
                seen_companies.add(c)
                spontaneous.append(j.copy())

    save_seen_jobs(seen | {j["id"] for j in scored_jobs})

    good_jobs = sum(1 for j in scored_jobs if not j.get("is_internship") and j.get("score", 0) >= MIN_SCORE)
    good_interns = sum(1 for j in scored_jobs if j.get("is_internship") and j.get("score", 0) >= MIN_SCORE_INTERNSHIP)
    print(f"\n[Results] {good_jobs} strong paid-job matches + {good_interns} strong internship matches "
          f"+ {len(spontaneous)} spontaneous leads")

    print(f"\n[Notion] Pushing to Notion...")
    push_to_notion(scored_jobs, spontaneous)

    close_browser()
    print(f"\n[Done] Check your Notion database for new jobs!")


if __name__ == "__main__":
    run_agent()
