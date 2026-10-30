"""LinkedIn / Naukri listings via JobSpy (open source, MIT).
Used for DISCOVERY: companies that are not in config.yaml yet.

LinkedIn search results are fetched WITHOUT descriptions (fast, few requests);
descriptions and the company's own apply link are then read only for the
LinkedIn jobs that pass the profile filter (linkedin_details)."""
from __future__ import annotations

import html
import math
import random
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests

_LI = requests.Session()
_LI.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
})


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def jobspy_search(sites, terms, location, hours_old, per_term, deadline=None):
    from jobspy import scrape_jobs  # imported lazily so career-site runs need no pandas

    out, errors = [], []
    for site in sites:
        for term in terms:
            if deadline and time.time() > deadline:
                errors.append(f"{site} '{term}': skipped - job-board time budget used up")
                continue
            df = None
            for attempt in range(2):
                try:
                    df = scrape_jobs(
                        site_name=[site], search_term=term,
                        location=location, results_wanted=per_term, hours_old=hours_old,
                        country_indeed="India", verbose=0,
                        # LinkedIn descriptions are fetched later, only for jobs that pass the filter
                        fetch_description=(site != "linkedin"),
                    )
                    break
                except Exception as e:  # one site failing must not stop the others
                    if attempt == 1:
                        errors.append(f"{site} '{term}': {type(e).__name__}: {str(e)[:150]}")
                    time.sleep(15)
            if df is None:
                continue
            for r in df.to_dict("records"):
                dp = _clean(r.get("date_posted"))
                jid = _clean(r.get("id"))
                out.append(dict(
                    source=f"jobspy-{site}", company=_clean(r.get("company")),
                    job_id=jid[3:] if jid.startswith("li-") else jid, title=_clean(r.get("title")),
                    location=_clean(r.get("location")),
                    posted_at=f"{dp[:10]}T00:00:00+05:30" if dp else None,
                    url=_clean(r.get("job_url_direct")) or _clean(r.get("job_url")),
                    listing_url=_clean(r.get("job_url")),
                    department="", description=_clean(r.get("description")),
                    company_url=_clean(r.get("company_url")),
                ))
            time.sleep(5)  # LinkedIn rate-limits fast bursts
    return out, errors


_DESC = re.compile(r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)
_APPLY = re.compile(r'<code id="applyUrl"[^>]*><!--"(.*?)"--></code>', re.S)


def _text(h):
    h = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<li[^>]*>", "- ", h, flags=re.I)
    h = html.unescape(re.sub(r"<[^>]+>", "", h))
    h = re.sub(r"[ \t\xa0]+", " ", h)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", h).strip()


def parse_linkedin_job_page(page: str):
    """-> (description text, company's own apply URL or '')"""
    m = _DESC.search(page)
    desc = _text(m.group(1)) if m else ""
    apply_url = ""
    a = _APPLY.search(page)
    if a:
        raw = html.unescape(a.group(1))
        q = parse_qs(urlparse(raw).query)
        apply_url = unquote(q["url"][0]) if "url" in q else raw
        if "linkedin.com" in urlparse(apply_url).netloc:
            apply_url = ""
    return desc, apply_url


def linkedin_details(job_ids, deadline=None, max_fail=4):
    """Fetch description + external apply link for each LinkedIn job id (politely).
    Returns {job_id: (description, apply_url)} and a list of error notes."""
    got, errors, fails = {}, [], 0
    for jid in job_ids:
        if deadline and time.time() > deadline:
            errors.append(f"linkedin details: time budget used up, {len(job_ids) - len(got)} left without description")
            break
        try:
            r = _LI.get(f"https://www.linkedin.com/jobs/view/{jid}", timeout=30)
            if r.status_code == 429:
                fails += 1
                time.sleep(30 * fails)
                continue
            r.raise_for_status()
            if not urlparse(r.url).path.startswith("/jobs/view/"):
                raise ValueError(f"redirected to {urlparse(r.url).path}")
            got[jid] = parse_linkedin_job_page(r.text)
            fails = 0
        except Exception as e:
            fails += 1
            errors.append(f"linkedin job {jid}: {type(e).__name__}: {str(e)[:80]}")
        if fails >= max_fail:
            errors.append(f"linkedin details: stopped after {max_fail} failures in a row")
            break
        time.sleep(random.uniform(2.0, 4.0))
    return got, errors
