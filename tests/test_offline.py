"""
Offline checks: no API keys, no network, nothing written to Notion.
Run from the repo folder:   python -m tests.test_offline
"""
import datetime
import os
import sys
from pathlib import Path

os.environ.update({"ANTHROPIC_API_KEY": "", "NOTION_API_KEY": "", "NOTION_DATABASE_ID": "",
                   "ADZUNA_APP_ID": "", "SERPAPI_KEY": "", "DRY_RUN": "1"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deep_scoring as ds          # noqa: E402
import extra_sources as xs         # noqa: E402
import notion_learning as nl       # noqa: E402

failures = []


def check(name, condition):
    print(("  ok   " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


def pool(scores, wild=()):
    return [{"score": s, "wild": False} for s in scores] + [{"score": s, "wild": True} for s in wild]


print("Suggested: 10 to 15")
check("12 good, none strong -> 10", len(ds.select_suggested(pool([7] * 12))) == 10)
check("20 good, 13 strong -> 13", len(ds.select_suggested(pool([9] * 13 + [7] * 7))) == 13)
check("30 strong -> capped at 15", len(ds.select_suggested(pool([9] * 30))) == 15)
check("only 4 good -> 4", len(ds.select_suggested(pool([7] * 4))) == 4)
check("10 main + 3 wildcards -> 13", len(ds.select_suggested(pool([7] * 12, wild=[8, 8, 8, 8]))) == 13)
check("15 strong leaves no room for wildcards",
      sum(p["wild"] for p in ds.select_suggested(pool([9] * 15, wild=[9]))) == 0)
check("weak wildcards are skipped", len(ds.select_suggested(pool([7] * 10, wild=[6.5]))) == 10)
check("best ones come first", ds.select_suggested(pool([6, 9, 7]))[0]["score"] == 9)

print("Scores")
job = {"date_posted": datetime.date.today().isoformat()}
total = ds.compute_total({"skills_fit": 8, "direction_fit": 10, "growth_onboarding": 6,
                          "taste_match": 5, "feasibility": 9}, job)
check(f"weighted total is sensible ({total})", 8.0 <= total <= 8.6)
check("weights add up to 1", abs(sum(ds.WEIGHTS.values()) - 1) < 1e-9)
check("unknown date -> freshness 5", ds.freshness_score({"date_posted": ""}) == 5)
check("Lever milliseconds date", ds.to_date(1760000000000) is not None)
check("RSS date", ds.to_date("Mon, 13 Oct 2025 09:00:00 +0000") == datetime.date(2025, 10, 13))
result = {"subscores": {"skills_fit": 7}, "track": "Wildcard", "one_line": "Analyst role.",
          "why_it_fits": ["CSRD at BOMA"], "gaps": ["SQL"], "is_internship": True, "deadline": "2026-11-30"}
applied = ds.apply_result({"title": "x", "date_posted": ""}, result)
check("deep result copied onto job", applied["track"] == "Wildcard" and applied["is_internship"]
      and applied["deadline"] == "2026-11-30" and applied["match_highlights"] == ["CSRD at BOMA"]
      and "Gaps: SQL" in applied["reasoning"])

print("Job boards")
check("greenhouse link", xs.detect_ats("https://job-boards.greenhouse.io/acme/jobs/123")["slug"] == "acme")
check("lever eu link", xs.detect_ats("https://jobs.eu.lever.co/acme/abc")["eu"] is True)
check("workable link", xs.detect_ats("https://apply.workable.com/acme/")["type"] == "workable")
check("recruitee link", xs.detect_ats("https://acme.recruitee.com/o/analyst")["slug"] == "acme")
check("teamtailor link", xs.detect_ats("https://acme.teamtailor.com/jobs")["type"] == "teamtailor")
check("workday is not supported", xs.detect_ats("https://danone.wd3.myworkdayjobs.com/x") is None)
html = '<html><body><iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe></body></html>'
check("board found inside a careers page", (xs.find_ats_in_html(html) or {}).get("slug") == "acme")
check("Belgian location kept", xs.is_belgium_or_remote("Gent, Oost-Vlaanderen"))
check("remote kept", xs.is_belgium_or_remote("Remote (Europe)"))
check("New York dropped", not xs.is_belgium_or_remote("New York, NY"))

print("Notion helpers")
link = "https://www.notion.so/Job-Agent-Profile-2ebd1ac0402e8101b6beeaee550c7d18?pvs=4"
check("page id from a full link", nl.clean_page_id(link) == "2ebd1ac0402e8101b6beeaee550c7d18")
check("page id with dashes", nl.clean_page_id("2ebd1ac0-402e-8101-b6be-eaee550c7d18")
      == "2ebd1ac0402e8101b6beeaee550c7d18")
rows = [{"company": "Umicore", "title": "ESG analyst", "status": "To Apply", "created": "2026-10-02"},
        {"company": "Bank", "title": "Teller", "status": "Not interested", "why_not": "no sustainability",
         "created": "2026-10-01"},
        {"company": "X", "title": "Y", "status": "New", "created": "2026-10-03"}]
taste = nl.taste_block(rows)
check("taste has yes and no", "Umicore | ESG analyst" in taste and "why not: no sustainability" in taste
      and "X | Y" not in taste)
check("url normalising ignores tracking", nl.normalize_url("https://www.x.be/job/1?utm_source=a")
      == nl.normalize_url("http://x.be/job/1"))
check("no Notion keys -> fallback profile", nl.load_profile("FALLBACK") == "FALLBACK")

print("Whole run, with every website and Claude replaced by fakes")
import company_scraper   # noqa: E402
import job_agent         # noqa: E402

for name in dir(job_agent):
    if name.startswith("scrape_"):
        setattr(job_agent, name, lambda *a, **k: [])
job_agent.render_page = lambda *a, **k: ""
job_agent.time.sleep = lambda s: None   # the real run pauses between sites
job_agent.fetch_description = lambda url: "Junior sustainability analyst supporting CSRD reporting."
job_agent.SEEN_JOBS_FILE = Path(os.devnull)
job_agent.load_seen_jobs = lambda: set()
fake = [xs.make_job(f"https://x.be/job/{i}", f"Junior ESG analyst {i} (m/f/x) team {chr(65 + i)}",
                    f"Company {i}", "Brussels", "", "", "Adzuna") for i in range(14)]
fake.append(xs.make_job("https://x.be/job/99", "Vrachtwagenchauffeur", "Truck nv", "Gent", "", "", "Adzuna"))
job_agent.extra_sources.collect_extra = lambda: [dict(j) for j in fake]
job_agent.run_company_scraper = lambda: []
job_agent.maybe_discover_new_companies = lambda: None


def fake_screen(jobs, profile, taste):
    for i, j in enumerate(jobs):
        j.update({"score": 6 + (i % 4), "reasoning": "fake", "match_highlights": [],
                  "seniority_ok": True, "location_ok": True, "spontaneous_worthy": False})
    return jobs


job_agent.score_jobs_with_claude = fake_screen
captured = {}
job_agent.push_to_notion = lambda entries, promotions=(): captured.update(entries=entries)
job_agent.NOTION_API_KEY = "x"   # pretend Notion is set up; pushes are captured above
job_agent.run_agent()
entries = captured.get("entries", [])
statuses = [s for _, s, _ in entries]
check("truck driver filtered out", not any("chauffeur" in j["title"].lower() for j, _, _ in entries))
check(f"10-15 Suggested ({statuses.count('Suggested')})", 10 <= statuses.count("Suggested") <= 15)
check(f"rest goes to Queue ({statuses.count('Queue')})", statuses.count("Suggested") + statuses.count("Queue") == 14)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("All checks passed")
