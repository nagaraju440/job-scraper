"""Fetchers for company career sites via their public job-board APIs.

Each fetcher returns a list of dicts in one shared schema:
  source, company, job_id, title, location, posted_at (ISO UTC or None),
  url, department, description (plain text)
All endpoints are public, keyless JSON feeds that the ATS vendors publish
for companies to embed their openings. Be polite: small delays, few calls.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone

import requests

UA = {"User-Agent": "Mozilla/5.0 (job-scraper; personal job search)",
      "Accept": "application/json"}
S = requests.Session()
S.headers.update(UA)


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _get(url, **kw):
    r = S.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Greenhouse
def greenhouse(company: str, token: str, **_):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                params={"content": "true"})
    out = []
    for j in data.get("jobs", []):
        out.append(dict(
            source="greenhouse", company=company, job_id=str(j.get("id")),
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted_at=j.get("first_published") or j.get("updated_at"),
            url=j.get("absolute_url", ""),
            department=", ".join(d.get("name", "") for d in j.get("departments", []) or []),
            description=_strip_html(j.get("content")),
        ))
    return out


# --------------------------------------------------------------------- Lever
def lever(company: str, token: str, region: str = "", **_):
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    data = _get(f"https://{host}/v0/postings/{token}", params={"mode": "json"})
    out = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", "") + "\n" + _strip_html(lst.get("content")))
        parts.append(j.get("additionalPlain") or "")
        ts = j.get("createdAt")
        out.append(dict(
            source="lever", company=company, job_id=j.get("id", ""),
            title=j.get("text", ""),
            location=", ".join(cats.get("allLocations") or [cats.get("location", "")]),
            posted_at=datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
            url=j.get("hostedUrl", ""), department=cats.get("team", ""),
            description="\n\n".join(p for p in parts if p).strip(),
        ))
    return out


# --------------------------------------------------------------------- Ashby
def ashby(company: str, token: str, **_):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                params={"includeCompensation": "true"})
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        out.append(dict(
            source="ashby", company=company, job_id=j.get("id", ""),
            title=j.get("title", ""), location=", ".join(l for l in locs if l),
            posted_at=j.get("publishedAt"), url=j.get("jobUrl", ""),
            department=j.get("team") or j.get("department") or "",
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
        ))
    return out


# ----------------------------------------------------------- SmartRecruiters
def smartrecruiters(company: str, token: str, country: str = "in", **_):
    out, offset = [], 0
    while True:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                    params={"limit": 100, "offset": offset, "country": country})
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            out.append(dict(
                source="smartrecruiters", company=company, job_id=j.get("id", ""),
                title=j.get("name", ""),
                location=", ".join(x for x in [loc.get("city"), loc.get("country", "").upper()] if x),
                posted_at=j.get("releasedDate"),
                url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                department=(j.get("department") or {}).get("label", ""),
                description="", _detail=j.get("ref"),
            ))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0):
            break
        time.sleep(0.5)
    return out


def smartrecruiters_detail(job: dict):
    ref = job.pop("_detail", None)
    if not ref:
        return job
    d = _get(ref)
    secs = (d.get("jobAd") or {}).get("sections") or {}
    job["description"] = "\n\n".join(
        _strip_html((secs.get(k) or {}).get("text"))
        for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
    return job


# ------------------------------------------------------------------- Workday
_POSTED = re.compile(r"Posted\s+(Today|Yesterday|(\d+)\+?\s+Days?\s+Ago)", re.I)


def _workday_age_days(posted_on: str):
    m = _POSTED.search(posted_on or "")
    if not m:
        return None
    if m.group(1).lower() == "today":
        return 0
    if m.group(1).lower() == "yesterday":
        return 1
    return int(m.group(2))


def workday(company: str, url: str, search: list[str] | None = None,
            max_age_days: int = 2, **_):
    """url = the careers site, e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    Lists jobs (20 per page, newest first), stops paging once postings are older
    than max_age_days, then fetches each fresh job's detail for the exact
    start date, requisition id and full description."""
    m = re.match(r"https://([^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
    host, site = m.group(1), m.group(2)
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    seen, stubs = set(), []
    for term in (search or [""]):
        offset = 0
        while True:
            r = S.post(f"{api}/jobs", timeout=30,
                       json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
                       headers={"Content-Type": "application/json"})
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            if not posts:
                break
            old_streak = 0
            for p in posts:
                age = _workday_age_days(p.get("postedOn", ""))
                if age is not None and age > max_age_days:
                    old_streak += 1
                    continue
                path = p.get("externalPath")
                if path and path not in seen:
                    seen.add(path)
                    stubs.append(p)
            # Workday sorts by relevance for searches; stop when a whole page is old
            if old_streak == len(posts) or offset > 400:
                break
            offset += 20
            time.sleep(0.5)
    out = []
    for p in stubs:
        try:
            d = _get(f"{api}{p['externalPath']}").get("jobPostingInfo", {})
        except Exception:
            d = {}
        start = d.get("startDate")
        out.append(dict(
            source="workday", company=company,
            job_id=d.get("jobReqId") or (p.get("bulletFields") or [""])[0],
            title=d.get("title") or p.get("title", ""),
            location=d.get("location") or p.get("locationsText", ""),
            posted_at=f"{start}T00:00:00+00:00" if start else None,
            posted_on_text=p.get("postedOn", ""),
            url=d.get("externalUrl") or f"https://{host}/{site}{p['externalPath']}",
            department=d.get("jobFamilyGroup", "") if isinstance(d.get("jobFamilyGroup"), str) else "",
            description=_strip_html(d.get("jobDescription")),
        ))
        time.sleep(0.4)
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "smartrecruiters": smartrecruiters, "workday": workday}
