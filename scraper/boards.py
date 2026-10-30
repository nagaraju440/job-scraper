"""LinkedIn / Naukri / Google Jobs listings via JobSpy (open source, MIT).
Used for DISCOVERY: companies that are not in companies.yaml yet."""
from __future__ import annotations

import time


def jobspy_search(sites, terms, location, hours_old, per_term):
    from jobspy import scrape_jobs  # imported lazily so ATS-only runs need no pandas

    out, errors = [], []
    for site in sites:
        for term in terms:
            try:
                df = scrape_jobs(
                    site_name=[site], search_term=term,
                    google_search_term=f"{term} jobs in {location} since yesterday",
                    location=location, results_wanted=per_term, hours_old=hours_old,
                    country_indeed="India", fetch_description=True, verbose=0,
                )
            except Exception as e:  # one site failing must not stop the others
                errors.append(f"{site} '{term}': {type(e).__name__}: {str(e)[:150]}")
                continue
            for r in df.fillna("").to_dict("records"):
                dp = r.get("date_posted")
                out.append(dict(
                    source=f"jobspy-{site}", company=str(r.get("company", "")),
                    job_id=str(r.get("id", "")), title=str(r.get("title", "")),
                    location=str(r.get("location", "")),
                    posted_at=f"{dp}T00:00:00+00:00" if dp else None,
                    url=str(r.get("job_url_direct") or r.get("job_url") or ""),
                    department="", description=str(r.get("description", "")),
                    company_url=str(r.get("company_url", "")),
                ))
            time.sleep(4)  # LinkedIn rate-limits fast bursts
    return out, errors
