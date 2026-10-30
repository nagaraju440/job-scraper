"""Fetchers for company career sites via their public job-board APIs.

Each fetcher returns a list of dicts in one shared schema:
  source, company, job_id, title, location, posted_at (ISO, tz-aware) or None,
  url, department, description (plain text)
All endpoints are public, keyless JSON feeds that the ATS vendors publish for
companies to embed their openings. Calls are few, spaced, and retried politely.
"""
from __future__ import annotations

import html
import random
import re
import time
from datetime import datetime, timezone

import requests

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
})


class FeedDown(Exception):
    """The company's career site is temporarily unavailable (e.g. Workday maintenance)."""


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)  # Greenhouse content is escaped twice
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", s).strip()


def _request(method, url, tries=4, **kw):
    """HTTP with retries on 429/5xx/timeouts and maintenance-page detection."""
    last = None
    for i in range(tries):
        try:
            r = S.request(method, url, timeout=40, allow_redirects=True, **kw)
            if "maintenance" in r.url or "unavailable" in r.url:
                raise FeedDown(f"maintenance page ({r.url})")
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"{r.status_code} for {url}")
            else:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if "json" not in ctype and r.text.lstrip()[:1] not in "[{":
                    raise FeedDown(f"non-JSON response ({ctype or 'no content-type'})")
                return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        time.sleep(2 ** i + random.random())
    if isinstance(last, requests.HTTPError) and str(last).startswith("503"):
        raise FeedDown(str(last))
    raise last


def _get(url, **kw):
    return _request("GET", url, **kw)


# ---------------------------------------------------------------- Greenhouse
def greenhouse(company: str, token: str, **_):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                params={"content": "true"})
    out = []
    for j in data.get("jobs", []):
        offices = [o.get("name", "") for o in j.get("offices") or [] if o.get("name")]
        loc = (j.get("location") or {}).get("name", "")
        out.append(dict(
            source="greenhouse", company=company,
            job_id=str(j.get("requisition_id") or j.get("id")),
            ats_id=str(j.get("id")),
            title=j.get("title", ""),
            location=", ".join(dict.fromkeys([loc] + offices)).strip(", "),
            posted_at=j.get("first_published") or j.get("updated_at"),
            url=j.get("absolute_url", ""),
            department=", ".join(d.get("name", "") for d in j.get("departments") or []),
            description=_strip_html(j.get("content")),
        ))
    return out, {"open": len(out)}


# --------------------------------------------------------------------- Lever
def lever(company: str, token: str, region: str = "", **_):
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    data = _get(f"https://{host}/v0/postings/{token}", params={"mode": "json"})
    if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
        raise requests.HTTPError(f"404 lever board '{token}': {data.get('error', data)}")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", "") + "\n" + _strip_html(lst.get("content")))
        parts.append(j.get("additionalPlain") or "")
        ts = j.get("createdAt")
        locs = list(cats.get("allLocations") or [cats.get("location", "")])
        if (j.get("country") or "").upper() == "IN":
            locs.append("India")
        out.append(dict(
            source="lever", company=company, job_id=j.get("id", ""),
            title=j.get("text", ""),
            location=", ".join(l for l in locs if l) + (" (remote)" if j.get("workplaceType") == "remote" else ""),
            posted_at=datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
            url=j.get("hostedUrl", ""), department=cats.get("team", "") or cats.get("department", ""),
            description="\n\n".join(p for p in parts if p).strip(),
        ))
    return out, {"open": len(out)}


# --------------------------------------------------------------------- Ashby
def ashby(company: str, token: str, **_):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                params={"includeCompensation": "true"})
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        for a in [j] + list(j.get("secondaryLocations") or []):
            locs.append((((a.get("address") or {}).get("postalAddress")) or {}).get("addressCountry", ""))
        out.append(dict(
            source="ashby", company=company, job_id=j.get("id", ""),
            title=j.get("title", ""),
            location=", ".join(dict.fromkeys(l for l in locs if l)) + (" (remote)" if j.get("isRemote") else ""),
            posted_at=j.get("publishedAt"), url=j.get("jobUrl", ""),
            department=j.get("team") or j.get("department") or "",
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
        ))
    return out, {"open": len(out)}


# ----------------------------------------------------------- SmartRecruiters
def smartrecruiters(company: str, token: str, country: str = "in", **_):
    out, offset, total = [], 0, None
    while True:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                    params={"limit": 100, "offset": offset, "country": country})
        if total is None:
            total = data.get("totalFound", 0)
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            out.append(dict(
                source="smartrecruiters", company=company,
                job_id=j.get("refNumber") or j.get("id", ""),
                title=j.get("name", ""),
                location=", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
                         + (" (remote)" if loc.get("remote") else ""),
                posted_at=j.get("releasedDate"),
                url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                department=(j.get("department") or {}).get("label", ""),
                description="", _detail=j.get("ref"),
            ))
        offset += len(items)
        if not items or offset >= total:
            break
        time.sleep(0.5)
    return out, {"open": total}


def smartrecruiters_detail(job: dict):
    ref = job.pop("_detail", None)
    if not ref:
        return job
    d = _get(ref)
    secs = (d.get("jobAd") or {}).get("sections") or {}
    job["description"] = "\n\n".join(
        t for t in (_strip_html((secs.get(k) or {}).get("text"))
                    for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")) if t)
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


def _workday_country_filter(facets, country="India"):
    """Workday tenants name their country facet differently. Find a facet whose
    values include the country exactly; else use every nested location value
    starting with the country name."""
    groups = []  # (facetParameter, values) at top level and one level down
    for f in facets or []:
        vals = f.get("values") or []
        if any(v.get("values") for v in vals):
            for g in vals:
                groups.append((g.get("facetParameter") or f.get("facetParameter"), g.get("values") or []))
        else:
            groups.append((f.get("facetParameter"), vals))
    c = country.lower()
    for param, vals in groups:  # 1) a value that IS the country
        hit = [v["id"] for v in vals if (v.get("descriptor") or "").strip().lower() == c and v.get("id")]
        if hit and param:
            return {param: hit}
    word = re.compile(rf"\b{re.escape(c)}\b", re.I)
    for param, vals in groups:  # 2) location values naming the country
        hit = [v["id"] for v in vals if word.search(v.get("descriptor") or "") and v.get("id")]
        if hit and param:
            return {param: hit}
    return {}


def workday(company: str, url: str, country: str = "India", max_age_days: int = 2,
            max_jobs: int = 1500, **_):
    """url = the careers site, e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    1. read the site's facets and apply its country filter (India),
    2. page through ALL matching postings (Workday returns the total only on the
       first page and wraps around past the end, so we stop at that total and
       de-duplicate by path),
    3. fetch the detail of every posting no older than max_age_days for the
       exact start date, requisition id and full description."""
    m = re.match(r"https://(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/?#]+)", url)
    if m:  # https://wd1.myworkdaysite.com/recruiting/<tenant>/<site>
        host, tenant, site = m.group(1), m.group(2), m.group(3)
        public = f"https://{host}/recruiting/{tenant}/{site}"
    else:  # https://<tenant>.wdN.myworkdayjobs.com/<site>
        m = re.match(r"https://([^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
        if not m:
            raise ValueError(f"bad workday url {url}")
        host, site = m.group(1), m.group(2)
        tenant = host.split(".")[0]
        public = f"https://{host}/{site}"
    api = f"https://{host}/wday/cxs/{tenant}/{site}"

    def page(facets, offset):
        return _request("POST", f"{api}/jobs", headers={"Content-Type": "application/json"},
                        json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""})

    first = page({}, 0)
    facets = _workday_country_filter(first.get("facets"), country) if country else {}
    if facets:
        first = page(facets, 0)
    total = int(first.get("total") or 0)
    seen, stubs, all_count = set(), [], 0
    batch, offset = first.get("jobPostings") or [], 0
    while batch:
        for p in batch:
            path = p.get("externalPath")
            if not path or path in seen:
                continue
            seen.add(path)
            all_count += 1
            age = _workday_age_days(p.get("postedOn", ""))
            if age is None or age <= max_age_days:
                stubs.append(p)
        offset += 20
        if offset >= min(total, max_jobs):
            break
        time.sleep(0.4)
        batch = page(facets, offset).get("jobPostings") or []

    out = []
    for p in stubs:
        try:
            d = _get(f"{api}{p['externalPath']}").get("jobPostingInfo", {})
        except Exception:
            d = {}
        start = d.get("startDate")
        req = d.get("jobReqId") or next((b for b in p.get("bulletFields") or [] if re.search(r"\d", b)), "")
        out.append(dict(
            source="workday", company=company, job_id=req,
            title=d.get("title") or p.get("title", ""),
            location=", ".join(x for x in [d.get("location") or p.get("locationsText", ""),
                                           (d.get("country") or {}).get("descriptor", "") if isinstance(d.get("country"), dict) else ""] if x),
            posted_at=f"{start}T00:00:00+05:30" if start else None,
            posted_on_text=p.get("postedOn", ""),
            url=d.get("externalUrl") or f"{public}{p['externalPath']}",
            department="",
            description=_strip_html(d.get("jobDescription")),
        ))
        time.sleep(0.3)
    return out, {"open": total, "listed": all_count, "country_filter": bool(facets)}


# ------------------------------------------------------------ Oracle Cloud HCM
def oracle(company: str, url: str, site: str, country: str = "IN", max_age_days: int = 2,
           max_pages: int = 40, **_):
    """url = https://<pod>.fa.<dc>.oraclecloud.com, site = e.g. CX_1.
    The requisition list is sorted newest first, so we stop once a page is older
    than the window, then fetch each fresh India job's detail."""
    base = url.rstrip("/")
    cut = (datetime.now(timezone.utc).date().toordinal() - max_age_days)
    stubs, total, offset = [], 0, 0
    for _ in range(max_pages):
        # the finder syntax (; , =) must reach Oracle unencoded, exactly as its own site sends it
        data = _get(f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                    f"&expand=requisitionList.secondaryLocations"
                    f"&finder=findReqs;siteNumber={site},limit=25,offset={offset},sortBy=POSTING_DATES_DESC")
        item = (data.get("items") or [{}])[0]
        total = item.get("TotalJobsCount") or total
        reqs = item.get("requisitionList") or []
        if not reqs:
            break
        old = 0
        for r in reqs:
            d = r.get("PostedDate") or ""
            try:
                fresh = datetime.fromisoformat(d[:10]).date().toordinal() >= cut
            except ValueError:
                fresh = True
            if not fresh:
                old += 1
                continue
            countries = {r.get("PrimaryLocationCountry")} | {
                s.get("CountryCode") for s in r.get("secondaryLocations") or []}
            if country in countries:
                stubs.append(r)
        offset += 25
        if old == len(reqs):
            break
        time.sleep(0.4)
    out = []
    for r in stubs:
        try:
            d = (_get(f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all"
                      f"&onlyData=true&finder=ById;Id=%22{r['Id']}%22,siteNumber={site}").get("items") or [{}])[0]
        except Exception:
            d = {}
        desc = "\n\n".join(t for t in (_strip_html(d.get(k) or r.get(k)) for k in (
            "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr",
            "ShortDescriptionStr")) if t)
        out.append(dict(
            source="oracle", company=company, job_id=str(d.get("RequisitionId") or r.get("Id")),
            title=r.get("Title", ""),
            location="; ".join([r.get("PrimaryLocation", "")] + [s.get("Name", "") for s in r.get("secondaryLocations") or []]).strip("; "),
            posted_at=d.get("ExternalPostedStartDate") or (f"{r['PostedDate'][:10]}T00:00:00+05:30" if r.get("PostedDate") else None),
            url=f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
            department=r.get("Department") or r.get("JobFamily") or "",
            description=desc,
        ))
        time.sleep(0.3)
    return out, {"open": total}


# ---------------------------------------------------------------- Eightfold
def eightfold(company: str, url: str, domain: str, location: str = "India",
              max_age_days: int = 2, max_pages: int = 60, **_):
    """Eightfold-hosted career sites (e.g. url=https://apply.careers.microsoft.com,
    domain=microsoft.com). Sorted by posting time, 10 per page."""
    base = url.rstrip("/")
    cut = time.time() - (max_age_days + 1) * 86400
    stubs, total, start = [], 0, 0
    for _ in range(max_pages):
        data = _get(f"{base}/api/pcsx/search", params={"domain": domain, "query": "", "location": location,
                                                       "start": start, "sort_by": "timestamp"})
        d = data.get("data") or data
        total = d.get("count") or total
        pos = d.get("positions") or []
        if not pos:
            break
        fresh = [p for p in pos if (p.get("postedTs") or 0) >= cut]
        stubs.extend(fresh)
        start += len(pos)
        if not fresh or start >= total:
            break
        time.sleep(0.4)
    out = []
    for p in stubs:
        try:
            dd = _get(f"{base}/api/pcsx/position_details",
                      params={"position_id": p["id"], "domain": domain, "hl": "en"})
            dd = dd.get("data") or dd
        except Exception:
            dd = {}
        ts = p.get("postedTs")
        purl = dd.get("publicUrl") or p.get("positionUrl") or f"/careers/job/{p['id']}"
        out.append(dict(
            source="eightfold", company=company, job_id=str(p.get("displayJobId") or p.get("atsJobId") or p["id"]),
            title=p.get("name", ""), location="; ".join(p.get("locations") or []),
            posted_at=datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            url=purl if purl.startswith("http") else base + purl,
            department=p.get("department") or "",
            description=_strip_html(dd.get("jobDescription")),
        ))
        time.sleep(0.3)
    return out, {"open": total}


# ------------------------------------------------------------------- Amazon
def amazon(company: str = "Amazon", country: str = "IND", max_age_days: int = 2,
           categories=("software-development",), **_):
    """amazon.jobs public search. Its 'recent' sort is not strictly by date, so
    every page is read (100 per page)."""
    cut = datetime.now(timezone.utc).date().toordinal() - max_age_days
    out, total, offset = [], 0, 0
    while True:
        params = [("normalized_country_code[]", country), ("result_limit", 100),
                  ("offset", offset), ("sort", "recent")] + [("category[]", c) for c in categories]
        data = _get("https://www.amazon.jobs/en/search.json", params=params)
        total = data.get("hits") or total
        jobs = data.get("jobs") or []
        for j in jobs:
            try:
                pd = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y")
            except ValueError:
                pd = None
            if pd and pd.date().toordinal() < cut:
                continue
            desc = "\n\n".join(t for t in (_strip_html(j.get(k)) for k in (
                "description", "basic_qualifications", "preferred_qualifications")) if t)
            out.append(dict(
                source="amazon", company=company, job_id=str(j.get("id_icims") or j.get("id")),
                title=j.get("title", ""), location=j.get("normalized_location") or j.get("location", ""),
                posted_at=f"{pd.date().isoformat()}T00:00:00+05:30" if pd else None,
                url="https://www.amazon.jobs" + (j.get("job_path") or ""),
                department=j.get("job_category") or "", description=desc,
            ))
        offset += len(jobs)
        if not jobs or offset >= total or offset >= 2000:
            break
        time.sleep(0.5)
    return out, {"open": total}


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "smartrecruiters": smartrecruiters, "workday": workday,
            "oracle": oracle, "eightfold": eightfold, "amazon": amazon}

# --------------------------------------------------------- feed auto-discovery
_FEED_PATTERNS = [
    ("workday", re.compile(r"https?://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/(?:job|details)/"),
     lambda m: {"url": f"https://{m[1]}/{m[2]}"}),
    ("workday", re.compile(r"https?://(wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/?#]+)/(?:job|details)/"),
     lambda m: {"url": f"https://{m[1]}/recruiting/{m[2]}/{m[3]}"}),
    ("greenhouse", re.compile(r"https?://(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_-]+)/jobs/"), lambda m: {"token": m[1]}),
    ("greenhouse", re.compile(r"https?://boards\.greenhouse\.io/embed/job_app\?for=([a-z0-9_-]+)"), lambda m: {"token": m[1]}),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([a-z0-9_-]+)/[0-9a-f-]{36}"), lambda m: {"token": m[1]}),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)/[0-9a-f-]{36}"), lambda m: {"token": m[1]}),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)/\d+"), lambda m: {"token": m[1]}),
    ("oracle", re.compile(r"https?://([a-z0-9]+\.fa\.[a-z0-9]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([^/?#]+)/"),
     lambda m: {"url": f"https://{m[1]}", "site": m[2]}),
]


def feed_from_url(url: str):
    """If a job URL points at a supported career platform, return the company's feed config."""
    for ats, rx, build in _FEED_PATTERNS:
        m = rx.search(url or "")
        if m:
            cfg = build(m)
            if cfg:
                return {"ats": ats, **cfg}
    return None


def feed_key(c: dict) -> str:
    return c["ats"] + ":" + (c.get("token") or c.get("url", "").lower().rstrip("/") + "|" + c.get("site", ""))
