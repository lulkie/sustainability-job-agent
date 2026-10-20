"""
extra_sources.py  —  Sources that use official data connections
================================================================
These come on top of every site job_agent.py already scrapes. They don't
depend on guessing a page's layout, so they don't break when a site changes.

  * Adzuna (Belgium): a job search engine with a free API.
    Needs ADZUNA_APP_ID and ADZUNA_APP_KEY; skipped when they're missing.
  * Google Jobs (Belgium) through SerpApi. Needs SERPAPI_KEY; skipped when
    it's missing. Uses only a few searches per weekly run.
  * Company job boards (Greenhouse, Lever, Ashby, Workable, Recruitee,
    Teamtailor) read through their public feeds. company_scraper.py uses
    these automatically for any company in companies.json whose careers
    page runs on one of them.

Every job comes out in the same shape as the scrapers in job_agent.py.
"""
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Weekly run, so a little more than a week back.
ADZUNA_MAX_DAYS_OLD = 8
ADZUNA_RESULTS_PER_QUERY = 20
ADZUNA_QUERIES = [
    # English
    "sustainability", "ESG", "CSRD", "sustainability consultant", "sustainability analyst",
    "environmental", "climate", "energy transition", "circular economy",
    "carbon footprint", "sustainability internship",
    # Dutch
    "duurzaamheid", "duurzaamheidsadviseur", "milieu adviseur", "milieucoördinator",
    "klimaat", "energietransitie", "circulaire economie", "MVO",
    "stage duurzaamheid",
    # French
    "développement durable", "environnement", "RSE", "transition énergétique",
]

SERPAPI_QUERIES = [
    "junior sustainability",
    "ESG CSRD analyst",
    "duurzaamheid medewerker",
    "sustainability internship",
]

# Used to keep company-board and Google results to Belgium or remote.
BELGIUM_WORDS = [
    "belgium", "belgië", "belgie", "belgique", "belgien", "flanders", "vlaanderen", "wallonia",
    "wallonie", "brussels", "brussel", "bruxelles", "antwerp", "antwerpen", "anvers", "ghent",
    "gent", "gand", "leuven", "louvain", "mechelen", "malines", "liège", "liege", "luik",
    "namur", "namen", "charleroi", "mons", "hasselt", "genk", "bruges", "brugge", "kortrijk",
    "courtrai", "aalst", "zaventem", "diegem", "vilvoorde", "wavre", "ottignies", "mortsel",
    "sint-niklaas", "roeselare", "ostend", "oostende", "geel", "turnhout", "lier", "halle",
    "zwijnaarde", "heverlee", "haasrode", "machelen", "waterloo", "nivelles", "tienen",
]
REMOTE_WORDS = ["remote", "anywhere", "europe", "emea", "benelux", "eu "]


def is_belgium_or_remote(location: str) -> bool:
    text = f" {(location or '').lower()} "
    if not text.strip():
        return True   # unknown: let the scorer judge it
    return any(w in text for w in BELGIUM_WORDS) or any(w in text for w in REMOTE_WORDS)


# ─── Small helpers ─────────────────────────────────────────────────────────────
def html_to_text(raw) -> str:
    if not raw:
        return ""
    return " ".join(BeautifulSoup(str(raw), "html.parser").get_text(" ").split())


def get_json(url, params=None):
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            print(f"    [{urllib.parse.urlparse(url).netloc}] {e}")
            time.sleep(2)
            continue
        if resp.status_code == 429:
            time.sleep(5)
            continue
        if resp.status_code != 200:
            print(f"    [{urllib.parse.urlparse(url).netloc}] HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def make_job(url, title, company="", location="", description="", date_posted="",
             source="", category="General") -> dict:
    title = " ".join((title or "").split())
    return {
        "id": (url or "") + title,
        "title": title,
        "company": " ".join((company or "").split()),
        "location": " ".join((location or "").split()),
        "url": url or "",
        "description": (description or "")[:6000],
        "date_posted": str(date_posted or ""),
        "source": source,
        "category": category,
        "is_internship": False,
    }


def pretty_slug(slug: str) -> str:
    return slug.split(".")[0].replace("-", " ").replace("_", " ").title()


# ─── Recognising company job boards ────────────────────────────────────────────
def detect_ats(url: str):
    """Return {"type", "slug", "eu"} when the link is a supported job board."""
    if not url:
        return None
    p = urllib.parse.urlparse(url.strip())
    host = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]
    q = urllib.parse.parse_qs(p.query)
    if host.endswith("greenhouse.io"):
        if parts and parts[0] == "embed":
            slug = (q.get("for") or [None])[0]
        else:
            slug = parts[0] if parts else None
        return {"type": "greenhouse", "slug": slug, "eu": False} if slug else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return {"type": "lever", "slug": parts[0], "eu": host.startswith("jobs.eu")}
    if host == "jobs.ashbyhq.com" and parts:
        return {"type": "ashby", "slug": parts[0], "eu": False}
    if host == "apply.workable.com" and parts and parts[0] not in ("api", "j"):
        return {"type": "workable", "slug": parts[0], "eu": False}
    if host.endswith(".workable.com") and host not in ("www.workable.com", "apply.workable.com"):
        return {"type": "workable", "slug": host.split(".")[0], "eu": False}
    if host.endswith(".recruitee.com"):
        return {"type": "recruitee", "slug": host.split(".")[0], "eu": False}
    if host.endswith(".teamtailor.com") and host != "www.teamtailor.com":
        return {"type": "teamtailor", "slug": host.removeprefix("www."), "eu": False}
    return None


def find_ats_in_html(html: str, base_url: str = ""):
    """Look on a careers page for a link or embed pointing at a supported board."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag, attr in (("a", "href"), ("iframe", "src"), ("script", "src")):
        for el in soup.find_all(tag):
            link = el.get(attr) or ""
            if link:
                found = detect_ats(urllib.parse.urljoin(base_url, link))
                if found:
                    return found
    match = re.search(r"boards\.greenhouse\.io/embed/job_board/js\?for=([\w-]+)", html)
    if match:
        return {"type": "greenhouse", "slug": match.group(1), "eu": False}
    return None


# ─── Company boards ────────────────────────────────────────────────────────────
def _greenhouse(slug, company, eu):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"})
    out = []
    for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        out.append(make_job(j.get("absolute_url", ""), j.get("title", ""),
                            company or j.get("company_name") or pretty_slug(slug),
                            (j.get("location") or {}).get("name", ""),
                            html_to_text(j.get("content", "")),
                            j.get("first_published") or j.get("updated_at"),
                            "Company board (Greenhouse)"))
    return out


def _lever(slug, company, eu):
    base = "https://api.eu.lever.co" if eu else "https://api.lever.co"
    data = get_json(f"{base}/v0/postings/{slug}", {"mode": "json"})
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        lists = " ".join(f"{l.get('text', '')}: {html_to_text(l.get('content', ''))}"
                         for l in (j.get("lists") or []))
        desc = " ".join(filter(None, [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")]))
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote":
            loc = f"{loc} (Remote)".strip()
        out.append(make_job(j.get("hostedUrl", ""), j.get("text", ""), company or pretty_slug(slug),
                            loc, desc, j.get("createdAt"), "Company board (Lever)"))
    return out


def _ashby(slug, company, eu):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
        loc = j.get("location", "") or ""
        if j.get("isRemote"):
            loc = f"{loc} (Remote)".strip()
        out.append(make_job(j.get("jobUrl", ""), j.get("title", ""), company or pretty_slug(slug), loc,
                            j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                            j.get("publishedAt"), "Company board (Ashby)"))
    return out


def _workable(slug, company, eu):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", {"details": "true"})
    if not isinstance(data, dict):
        return []
    name = company or data.get("name") or pretty_slug(slug)
    out = []
    for j in data.get("jobs", []) or []:
        loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        if j.get("telecommuting"):
            loc = f"{loc} (Remote)".strip()
        url = j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode', '')}/"
        out.append(make_job(url, j.get("title", ""), name, loc,
                            html_to_text(j.get("full_description") or j.get("description") or ""),
                            j.get("published_on") or j.get("created_at"), "Company board (Workable)"))
    return out


def _recruitee(slug, company, eu):
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for o in (data or {}).get("offers", []) if isinstance(data, dict) else []:
        loc = o.get("location") or ", ".join(filter(None, [o.get("city"), o.get("country")]))
        out.append(make_job(o.get("careers_url") or o.get("url", ""), o.get("title", ""),
                            company or o.get("company_name") or pretty_slug(slug), loc,
                            html_to_text((o.get("description") or "") + " " + (o.get("requirements") or "")),
                            o.get("published_at") or o.get("created_at"), "Company board (Recruitee)"))
    return out


def _teamtailor(slug, company, eu):
    host = slug if "." in slug else f"{slug}.teamtailor.com"
    try:
        resp = requests.get(f"https://{host}/jobs.rss", headers=HEADERS, timeout=20)
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"    [teamtailor:{slug}] {e}")
        return []
    name = company or (root.findtext("./channel/title") or "").strip() or pretty_slug(slug)
    out = []
    for item in root.iterfind("./channel/item"):
        location = ", ".join(
            el.text.strip() for el in item.iter()
            if el.tag.rsplit("}", 1)[-1] == "city" and el.text and el.text.strip())
        out.append(make_job(item.findtext("link") or "", item.findtext("title") or "", name,
                            location, html_to_text(item.findtext("description") or ""),
                            item.findtext("pubDate"), "Company board (Teamtailor)"))
    return out


BOARD_READERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
                 "workable": _workable, "recruitee": _recruitee, "teamtailor": _teamtailor}


def fetch_board(ats: dict, company: str = "") -> list[dict]:
    """All open jobs on one company board, kept to Belgium or remote."""
    reader = BOARD_READERS.get((ats or {}).get("type"))
    if not reader or not ats.get("slug"):
        return []
    try:
        jobs = reader(ats["slug"], company, ats.get("eu", False))
    except Exception as e:  # one broken board must never stop the run
        print(f"    [{ats['type']}:{ats['slug']}] {e}")
        return []
    return [j for j in jobs if j["title"] and j["url"] and is_belgium_or_remote(j["location"])]


# ─── Search APIs ───────────────────────────────────────────────────────────────
def search_adzuna(what: str) -> list[dict]:
    params = {
        "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY, "what": what,
        "results_per_page": ADZUNA_RESULTS_PER_QUERY, "max_days_old": ADZUNA_MAX_DAYS_OLD,
        "sort_by": "date", "content-type": "application/json",
    }
    data = get_json("https://api.adzuna.com/v1/api/jobs/be/search/1", params)
    out = []
    for r in (data or {}).get("results", []) or []:
        out.append(make_job(r.get("redirect_url", ""), html_to_text(r.get("title", "")),
                            (r.get("company") or {}).get("display_name", ""),
                            (r.get("location") or {}).get("display_name", "") or "Belgium",
                            html_to_text(r.get("description", "")), r.get("created"), "Adzuna"))
    return out


_AGGREGATORS = ("linkedin.", "indeed.", "glassdoor.", "jooble.", "talent.com", "adzuna.")


def search_google_jobs(q: str) -> list[dict]:
    data = get_json("https://serpapi.com/search.json", {
        "engine": "google_jobs", "q": q, "location": "Belgium", "gl": "be", "hl": "en",
        "api_key": SERPAPI_KEY})
    out = []
    for r in (data or {}).get("jobs_results", []) or []:
        options = r.get("apply_options") or []
        direct = [o for o in options if not any(h in (o.get("link") or "") for h in _AGGREGATORS)]
        link = (direct or options or [{}])[0].get("link") or r.get("share_link") or ""
        via = (r.get("via") or "").replace("via ", "").strip()
        out.append(make_job(link, r.get("title", ""), r.get("company_name", ""),
                            r.get("location", ""), r.get("description", ""), "",
                            f"Google Jobs ({via})" if via else "Google Jobs"))
    return [j for j in out if j["url"] and is_belgium_or_remote(j["location"])]


def collect_extra() -> list[dict]:
    jobs = []
    if ADZUNA_APP_ID and ADZUNA_APP_KEY:
        print(f"[Adzuna] {len(ADZUNA_QUERIES)} searches in Belgium")
        for q in ADZUNA_QUERIES:
            found = search_adzuna(q)
            jobs += found
            time.sleep(1.2)
        print(f"  -> {len(jobs)} from Adzuna")
    else:
        print("[Adzuna] No ADZUNA_APP_ID / ADZUNA_APP_KEY set, skipping")
    if SERPAPI_KEY:
        before = len(jobs)
        print(f"[Google Jobs] {len(SERPAPI_QUERIES)} searches in Belgium")
        for q in SERPAPI_QUERIES:
            jobs += search_google_jobs(q)
            time.sleep(1)
        print(f"  -> {len(jobs) - before} from Google Jobs")
    else:
        print("[Google Jobs] No SERPAPI_KEY set, skipping")
    return jobs
