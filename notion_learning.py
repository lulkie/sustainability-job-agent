"""
notion_learning.py  —  Everything the agent reads back from Notion
===================================================================
  * Your profile, from the "Job Agent Profile" page (NOTION_PROFILE_PAGE_ID).
    The profile in job_agent.py is only a fallback when the page can't be read.
  * Your taste: rows you set to "To Apply" (or further along) count as yes,
    rows you set to "Not interested" count as no, with the "Why not" text.
  * The rows waiting in "Queue", so they can be promoted to "Suggested" or,
    after QUEUE_EXPIRY_DAYS, moved to Notion's trash (recoverable for 30 days).
  * The links already in the database, so a job is never added twice.

Nothing here raises: if Notion can't be reached, the agent carries on with
the fallback profile and no taste examples.
"""
import datetime
import os
import re
import time

import requests

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_PROFILE_PAGE_ID = os.environ.get("NOTION_PROFILE_PAGE_ID", "")
NOTION_VERSION = "2022-06-28"

# Property names in your database
PROP_TITLE = "Job Title"
PROP_COMPANY = "Company"
PROP_STATUS = "Status"
PROP_URL = "URL"
PROP_DATE_FOUND = "Date Found"
PROP_FIT_SCORE = "Fit Score"      # number, added automatically
PROP_WHY_NOT = "Why not"          # text, added automatically

STATUS_NEW = "New"
STATUS_SUGGESTED = "Suggested"
STATUS_QUEUE = "Queue"
STATUS_NOT_INTERESTED = "Not interested"
# Statuses that mean "I wanted this one". Rejected counts too: you wanted it,
# the company said no.
STATUSES_YES = {"To Apply", "Applied", "Interview", "Interviewed", "Offer", "Accepted", "Rejected"}

QUEUE_EXPIRY_DAYS = 14
TASTE_EXAMPLES_EACH = 20
RECENT_ROWS_TO_READ = 400


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def enabled() -> bool:
    return bool(NOTION_API_KEY and NOTION_DATABASE_ID)


def clean_page_id(raw: str) -> str:
    """Accept a bare id, an id with dashes, or a whole notion.so link."""
    compact = (raw or "").split("?")[0].replace("-", "")
    # The id is the last 32 hex characters; a page title before it may end in
    # hex-looking letters ("...Profile-2ebd..."), so take the run's tail.
    runs = re.findall(r"[0-9a-fA-F]{32,}", compact)
    return runs[-1][-32:].lower() if runs else ""


def _request(method: str, url: str, **kw):
    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers=_headers(), timeout=20, **kw)
        except requests.RequestException as e:
            print(f"  [Notion] {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        return resp
    return None


# ─── Profile page ──────────────────────────────────────────────────────────────
_TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                "numbered_list_item", "quote", "callout", "toggle", "to_do", "code"}


def _block_lines(block_id: str, depth: int = 0) -> list[str]:
    lines, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = _request("GET", f"https://api.notion.com/v1/blocks/{block_id}/children", params=params)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                print(f"  [Notion] Could not read profile page: {resp.text[:200]}")
            return lines
        data = resp.json()
        for block in data.get("results", []):
            kind = block.get("type", "")
            if kind in _TEXT_BLOCKS:
                text = "".join(t.get("plain_text", "") for t in block[kind].get("rich_text", []))
                if text.strip():
                    prefix = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
                              "bulleted_list_item": "- ", "numbered_list_item": "- ",
                              "to_do": "- "}.get(kind, "")
                    lines.append("  " * depth + prefix + text)
            if block.get("has_children") and depth < 3 and kind != "child_page":
                lines += _block_lines(block["id"], depth + 1)
        if not data.get("has_more"):
            return lines
        cursor = data.get("next_cursor")


def load_profile(fallback: str) -> str:
    page_id = clean_page_id(NOTION_PROFILE_PAGE_ID)
    if not (NOTION_API_KEY and page_id):
        print("[Profile] No NOTION_PROFILE_PAGE_ID set, using the profile in the code")
        return fallback
    text = "\n".join(_block_lines(page_id)).strip()
    if len(text) < 200:
        print("[Profile] Notion profile page is empty or unreadable, using the profile in the code")
        return fallback
    print(f"[Profile] Using your Notion profile page ({len(text)} characters)")
    return text


# ─── Reading rows ──────────────────────────────────────────────────────────────
def _plain(prop: dict) -> str:
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in prop.get(kind, []))
    if kind in ("select", "status"):
        return (prop.get(kind) or {}).get("name", "")
    if kind == "url":
        return prop.get("url") or ""
    if kind == "date":
        return (prop.get("date") or {}).get("start", "") or ""
    if kind == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    return ""


def row_from_page(page: dict) -> dict:
    props = page.get("properties", {})
    title_prop = props.get(PROP_TITLE) or next(
        (p for p in props.values() if p.get("type") == "title"), {})
    fit = props.get(PROP_FIT_SCORE, {})
    return {
        "id": page.get("id", ""),
        "title": _plain(title_prop),
        "company": _plain(props.get(PROP_COMPANY, {})),
        "status": _plain(props.get(PROP_STATUS, {})),
        "url": _plain(props.get(PROP_URL, {})),
        "date_found": _plain(props.get(PROP_DATE_FOUND, {})),
        "why_not": _plain(props.get(PROP_WHY_NOT, {})),
        "fit_score": fit.get("number") if fit.get("type") == "number" else None,
        "created": page.get("created_time", ""),
    }


def _query(body: dict, limit: int) -> list[dict]:
    rows, cursor = [], None
    while len(rows) < limit:
        payload = dict(body, page_size=min(100, limit - len(rows)))
        if cursor:
            payload["start_cursor"] = cursor
        resp = _request("POST", f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
                        json=payload)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                print(f"  [Notion] Query failed: {resp.text[:200]}")
            break
        data = resp.json()
        rows += [row_from_page(p) for p in data.get("results", [])]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


_status_kind_cache: list = []


def _status_kind() -> str:
    """'select' or 'status', whichever your Status column is."""
    if not _status_kind_cache:
        kind = "select"
        resp = _request("GET", f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}")
        if resp is not None and resp.status_code == 200:
            kind = resp.json().get("properties", {}).get(PROP_STATUS, {}).get("type", "select")
        _status_kind_cache.append(kind if kind in ("select", "status") else "select")
    return _status_kind_cache[0]


def load_rows() -> list[dict]:
    """The most recent rows (for taste and duplicate links) plus every Queue row."""
    if not enabled():
        return []
    recent = _query({"sorts": [{"timestamp": "created_time", "direction": "descending"}]},
                    RECENT_ROWS_TO_READ)
    kind = _status_kind()
    queue = _query({"filter": {"property": PROP_STATUS, kind: {"equals": STATUS_QUEUE}}}, 500)
    by_id = {r["id"]: r for r in recent}
    for r in queue:
        by_id.setdefault(r["id"], r)
    rows = list(by_id.values())
    print(f"[Notion] Read {len(rows)} rows ({sum(1 for r in rows if r['status'] == STATUS_QUEUE)} in Queue)")
    return rows


def existing_urls(rows: list[dict]) -> set:
    return {normalize_url(r["url"]) for r in rows if r.get("url")}


def normalize_url(url: str) -> str:
    url = (url or "").strip().lower().split("#")[0]
    url = re.sub(r"[?&](utm_[^=&]+|trk|trackingid|refid|src|source)=[^&]*", "", url)
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/?&")


# ─── Taste ─────────────────────────────────────────────────────────────────────
def taste_block(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r.get("created", ""), reverse=True)
    yes = [f"{r['company']} | {r['title']}" for r in rows
           if r.get("status") in STATUSES_YES and r.get("title")][:TASTE_EXAMPLES_EACH]
    no = [f"{r['company']} | {r['title']}" + (f" (why not: {r['why_not']})" if r.get("why_not") else "")
          for r in rows if r.get("status") == STATUS_NOT_INTERESTED and r.get("title")][:TASTE_EXAMPLES_EACH]
    return ("ROLES HE SAID YES TO:\n" + ("\n".join(f"- {x}" for x in yes) or "- (none yet)") +
            "\n\nROLES HE SAID NO TO:\n" + ("\n".join(f"- {x}" for x in no) or "- (none yet)"))


# ─── Changing rows ─────────────────────────────────────────────────────────────
def set_status(page_id: str, status: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"  [dry run] would set {page_id} to {status}")
        return True
    kind = _status_kind()
    resp = _request("PATCH", f"https://api.notion.com/v1/pages/{page_id}",
                    json={"properties": {PROP_STATUS: {kind: {"name": status}}}})
    return resp is not None and resp.status_code == 200


def expire_queue(rows: list[dict], dry_run: bool = False) -> int:
    """Queue rows older than QUEUE_EXPIRY_DAYS go to Notion's trash."""
    if not enabled():
        return 0
    cutoff = (datetime.date.today() - datetime.timedelta(days=QUEUE_EXPIRY_DAYS)).isoformat()
    old = [r for r in rows if r.get("status") == STATUS_QUEUE
           and (r.get("date_found") or r.get("created", "")[:10] or "9999") < cutoff]
    moved = 0
    for r in old:
        if dry_run:
            moved += 1
            continue
        resp = _request("PATCH", f"https://api.notion.com/v1/pages/{r['id']}", json={"archived": True})
        if resp is not None and resp.status_code == 200:
            moved += 1
            r["status"] = "(trashed)"
        time.sleep(0.35)
    if moved:
        print(f"[Queue] Moved {moved} Queue rows older than {QUEUE_EXPIRY_DAYS} days to Notion's trash")
    return moved
