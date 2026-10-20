"""
deep_scoring.py  —  Step 2 of scoring, and choosing what becomes "Suggested"
============================================================================
Step 1 (in job_agent.py) is the cheap Haiku screen over every new posting.
Step 2, here, gives the most promising ones a second, careful look with a
stronger model on the full posting: what the role really needs, the evidence
for each need in your profile, and five sub-scores. The total is computed in
code from fixed weights, so scores stay comparable from week to week.

select_suggested() then picks 10 to 15 jobs for "Suggested": always at least
10 (when there are that many good matches), and up to 15 when the week has
more strong matches (Fit Score 8+). Up to 3 "Wildcard" picks fit inside that
maximum of 15.
"""
import datetime
import email.utils
import json
import os
import re
import time

import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_DEEP = "claude-sonnet-5-5"

MAX_DEEP_SCORES_PER_RUN = 40      # cost control

SUGGESTED_MIN = 10
SUGGESTED_MAX = 15
STRONG_SCORE = 8.0                # a week with more strong matches than 10 gets up to 15
WILDCARDS_MAX = 3
MIN_SCORE_WILDCARD = 7.0
QUEUE_CAP = 40                    # new rows added to Queue per run

WEIGHTS = {                       # must add up to 1.0
    "skills_fit": 0.25,
    "direction_fit": 0.25,
    "growth_onboarding": 0.10,
    "taste_match": 0.10,
    "feasibility": 0.20,
    "freshness": 0.10,
}

TRACKS = ["ESG / CSRD", "Environmental management", "Climate & energy",
          "Policy & NGO", "Sustainability comms", "Wildcard"]

_client = None


def _ask(system: str, user: str, max_tokens: int = 3000) -> str:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(4):
        try:
            resp = _client.messages.create(model=MODEL_DEEP, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": user}])
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            wait = 10 * (attempt + 1)
            print(f"    [Claude] {type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            print(f"    [Claude] {e.status_code}: {str(e)[:200]}")
            return ""
    return ""


def parse_json_object(text: str):
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


DEEP_SYSTEM = """You are a sharp career advisor scoring one job posting for one candidate.

CANDIDATE PROFILE:
{profile}

HIS TASTE SO FAR (learn from these, do not copy them):
{taste}

HOW TO SCORE
1. List up to 6 things the role really needs, including unstated ones. For each, find concrete evidence in his profile (a job, project, thesis, course or skill) or say there is none.
2. Give sub-scores from 1 to 10:
   - skills_fit: how well those needs are evidenced in his profile.
   - direction_fit: does the role carry a genuine sustainability mandate? 10 = the core of the job is sustainability, ESG, CSRD or climate reporting, environmental management or sustainability strategy. 9 = climate or environmental policy (EU institutions, government, NGOs), energy transition, circular economy, or sustainability communications and behaviour change. 7 = consulting, project, analyst, marketing or communications role with a clear sustainability component. 4 = a generic role at a company that happens to be green. 1 = unrelated, or only shares a keyword (waste collection, cleaning, warehouse, driving, purely technical or IT work).
   - growth_onboarding: 10 = graduate or trainee programme, internship with real ownership, or explicit structured training and mentoring, or a large organisation with internal mobility. 5 = no information. 2 = solo role, "hit the ground running", no team.
   - taste_match: similarity to roles he said yes to versus no to. 5 when there are no examples yet.
   - feasibility: could he be hired today, as a recent graduate with about one year of relevant experience. 10 = explicitly entry level, junior, graduate, trainee or internship, asking 0 to 2 years. 7 = asks 2 to 3 years. 4 = asks 3 to 4 years, or quietly assumes experience he does not have. 1 = senior, 5+ years, management of a team, or a hard credential he lacks (engineering degree, law degree). Dutch is native, English fluent and French professional: daily work in German, or native-level French for client work, is a small penalty. Be strict: a role he cannot realistically get is worth nothing to him.
3. Track: one of "ESG / CSRD", "Environmental management", "Climate & energy", "Policy & NGO", "Sustainability comms", or "Wildcard" (outside his stated directions, but a smart fit for his transferable skills).
4. Location: he is based in Leuven and wants roles in Belgium, or remote within the EU. Set location_ok to false if the role is clearly elsewhere and not remote.
5. Flag internships. Extract a deadline only if the posting states one.

Write reasons as specific evidence ("implemented CSRD at BOMA"), never generic praise. Be honest: a weak fit gets low scores.

Return ONLY this JSON:
{{
  "needs": [{{"need": "...", "evidence": "... or none", "met": "yes|partly|no"}}],
  "subscores": {{"skills_fit": 0, "direction_fit": 0, "growth_onboarding": 0, "taste_match": 0, "feasibility": 0}},
  "track": "ESG / CSRD|Environmental management|Climate & energy|Policy & NGO|Sustainability comms|Wildcard",
  "one_line": "<what the job is, max 20 words>",
  "why_it_fits": ["...", "...", "..."],
  "gaps": ["..."],
  "red_flags": ["..."],
  "is_internship": false,
  "location_ok": true,
  "deadline": "YYYY-MM-DD or empty"
}}"""


def deep_score(job: dict, profile: str, taste: str):
    if not ANTHROPIC_API_KEY:
        return None
    system = DEEP_SYSTEM.format(profile=profile, taste=taste)
    user = (f"TITLE: {job.get('title', '')}\nCOMPANY: {job.get('company', '')}\n"
            f"LOCATION: {job.get('location', '')}\nPOSTED: {job.get('date_posted') or 'unknown'}\n\n"
            f"POSTING:\n{(job.get('description') or '(no description available)')[:7000]}")
    result = parse_json_object(_ask(system, user))
    if not isinstance(result, dict) or not isinstance(result.get("subscores"), dict):
        print(f"    [Deep score] could not read the answer for {job.get('title')} @ {job.get('company')}")
        return None
    return result


# ─── Freshness and total ───────────────────────────────────────────────────────
def to_date(value):
    """ISO strings, epoch milliseconds (Lever) and RFC 822 dates (RSS)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            seconds = number / 1000 if number > 1e11 else number
            return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).date()
        text = str(value).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return datetime.date.fromisoformat(text[:10])
        parsed = email.utils.parsedate_to_datetime(text)
        return parsed.date() if parsed else None
    except (ValueError, TypeError, OverflowError):
        return None


def days_old(value):
    d = to_date(value)
    return None if d is None else (datetime.date.today() - d).days


def freshness_score(job: dict) -> int:
    age = days_old(job.get("date_posted"))
    if age is None:
        return 5
    for limit, score in ((2, 10), (5, 8), (10, 6), (20, 4)):
        if age <= limit:
            return score
    return 2


def compute_total(subscores: dict, job: dict) -> float:
    total = 0.0
    for key in ("skills_fit", "direction_fit", "growth_onboarding", "taste_match", "feasibility"):
        try:
            value = float(subscores.get(key, 5))
        except (TypeError, ValueError):
            value = 5.0
        total += max(1.0, min(10.0, value)) * WEIGHTS[key]
    total += freshness_score(job) * WEIGHTS["freshness"]
    return round(total, 1)


def apply_result(job: dict, result: dict) -> dict:
    """Copy the deep score onto the job, in the fields push_to_notion writes."""
    job["deep_scored"] = True
    job["fit_score"] = compute_total(result["subscores"], job)
    job["score"] = job["fit_score"]
    track = result.get("track", "")
    job["track"] = track if track in TRACKS else ""
    why = [str(x) for x in (result.get("why_it_fits") or []) if x][:3]
    gaps = [str(x) for x in (result.get("gaps") or []) if x][:3]
    flags = [str(x) for x in (result.get("red_flags") or []) if x][:3]
    parts = [result.get("one_line", "")]
    if job["track"] == "Wildcard":
        parts.insert(0, "Wildcard pick.")
    if gaps:
        parts.append("Gaps: " + "; ".join(gaps) + ".")
    if flags:
        parts.append("Watch out: " + "; ".join(flags) + ".")
    job["reasoning"] = " ".join(p for p in parts if p).strip()
    job["match_highlights"] = why or job.get("match_highlights", [])
    job["is_internship"] = bool(job.get("is_internship")) or bool(result.get("is_internship"))
    if result.get("location_ok") is False:
        job["location_ok"] = False
    deadline = str(result.get("deadline", ""))
    if not job.get("deadline") and re.match(r"^\d{4}-\d{2}-\d{2}$", deadline):
        job["deadline"] = deadline
    return job


# ─── Choosing Suggested ────────────────────────────────────────────────────────
def select_suggested(pool: list[dict]) -> list[dict]:
    """pool: entries {"score": float, "wild": bool, ...}. Returns the chosen
    entries: the top 10, extended up to 15 while there are strong matches
    (score >= STRONG_SCORE) left, then up to 3 wildcards in the room left."""
    main = sorted([p for p in pool if not p["wild"]], key=lambda p: -p["score"])
    wild = sorted([p for p in pool if p["wild"] and p["score"] >= MIN_SCORE_WILDCARD],
                  key=lambda p: -p["score"])
    strong = sum(1 for p in main if p["score"] >= STRONG_SCORE)
    n = min(SUGGESTED_MAX, max(SUGGESTED_MIN, strong))
    chosen = main[:n]
    room = SUGGESTED_MAX - len(chosen)
    chosen += wild[:min(WILDCARDS_MAX, room)]
    return chosen
