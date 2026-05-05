"""
Quick Notion test — scrapes only Brussels Sustainability Club
and pushes one job to Notion to verify the integration works.
"""

import json
import os
import re
import time
import datetime
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def scrape_brussels_sustainability_club() -> list[dict]:
    jobs = []
    try:
        url = "https://brusselssustainabilityclub.com/jobs/"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.select("article, div.job, li.job, div[class*='job']")[:3]:
            title_el = card.select_one("h2, h3, .job-title, a")
            company_el = card.select_one(".company, .employer, .organization")
            link_el = card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            href = link_el["href"] if link_el else ""
            if title and href:
                jobs.append({"title": title, "company": company,
                             "location": "Brussels, Belgium", "url": href,
                             "source": "Brussels Sustainability Club",
                             "score": 8, "reasoning": "Test entry",
                             "match_highlights": ["Test highlight"]})
        print(f"  → {len(jobs)} jobs scraped")
    except Exception as e:
        print(f"  [Error]: {e}")
    return jobs

def push_to_notion(jobs):
    if not NOTION_API_KEY:
        print("  [Notion] No API key!")
        return

    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    # Auto-find the Job applications database
    db_id = NOTION_DATABASE_ID
    try:
        search_resp = requests.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json={"filter": {"property": "object", "value": "database"}},
            timeout=15
        )
        results = search_resp.json().get("results", [])
        print(f"  [Notion] Can see {len(results)} databases:")
        for r in results:
            title = r.get("title", [{}])
            name = title[0].get("plain_text", "Untitled") if title else "Untitled"
            print(f"    - {name}: {r['id']}")
            if "job" in name.lower() or "application" in name.lower():
                db_id = r["id"]
                print(f"  [Notion] Using: {name} ({db_id})")
    except Exception as e:
        print(f"  [Notion] Search error: {e}")

    for job in jobs[:1]:  # Only push 1 job for testing
        payload = {
            "parent": {"database_id": db_id},
            "properties": {
                "Job Title": {"title": [{"text": {"content": job.get("title", "TEST")}}]},
                "Company": {"rich_text": [{"text": {"content": job.get("company", "")}}]},
                "Location": {"rich_text": [{"text": {"content": job.get("location", "")}}]},
                "Source": {"select": {"name": job.get("source", "Other")}},
                "Score": {"select": {"name": "⭐⭐⭐ Excellent"}},
                "Type": {"select": {"name": "Vacancy"}},
                "URL": {"url": job.get("url", None)},
                "Reasoning": {"rich_text": [{"text": {"content": job.get("reasoning", "")}}]},
                "Highlights": {"rich_text": [{"text": {"content": "Test"}}]},
                "Date Found": {"date": {"start": datetime.datetime.now().strftime("%Y-%m-%d")}},
                "Status": {"select": {"name": "New"}},
            },
        }
        try:
            resp = requests.post("https://api.notion.com/v1/pages",
                                 headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                print(f"  [Notion] ✅ Successfully pushed test job!")
            else:
                print(f"  [Notion] ❌ Failed: {resp.text[:300]}")
        except Exception as e:
            print(f"  [Notion] Error: {e}")

print("=== Notion Test ===")
print("[Scraping] Brussels Sustainability Club...")
jobs = scrape_brussels_sustainability_club()
print(f"[Pushing] Sending 1 job to Notion...")
push_to_notion(jobs)
print("[Done]")
