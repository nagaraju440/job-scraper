"""LinkedIn / Naukri / Google Jobs listings via JobSpy (open source, MIT).
Used for DISCOVERY: companies that are not in config.yaml yet."""
from __future__ import annotations

import math
import time


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def jobspy_search(sites, terms, location, hours_old, per_term):
    from jobspy import scrape_jobs  # imported lazily so career-site runs need no pandas

    out, errors = [], []
    for site in sites:
        for term in terms:
            df = None
            for attempt in range(2):
                try:
                    df = scrape_jobs(
                        site_name=[site], search_term=term,
                        google_search_term=f"{term} jobs in {location} since yesterday",
                        location=location, results_wanted=per_term, hours_old=hours_old,
                        country_indeed="India", fetch_description=True, verbose=0,
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
                out.append(dict(
                    source=f"jobspy-{site}", company=_clean(r.get("company")),
                    job_id=_clean(r.get("id")), title=_clean(r.get("title")),
                    location=_clean(r.get("location")),
                    posted_at=f"{dp[:10]}T00:00:00+05:30" if dp else None,
                    url=_clean(r.get("job_url_direct")) or _clean(r.get("job_url")),
                    department="", description=_clean(r.get("description")),
                    company_url=_clean(r.get("company_url")),
                ))
            time.sleep(5)  # LinkedIn rate-limits fast bursts
    return out, errors
