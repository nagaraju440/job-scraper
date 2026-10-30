"""Daily job scrape. Usage:
  python -m scraper.run                 # normal daily run
  python -m scraper.run --validate      # check every company feed, no filtering
  python -m scraper.run --hours 72      # wider window (catch-up after a missed day)

Writes data/<YYYY-MM-DD IST>/<profile>.json + .csv and health.json.
Only a LOOSE pre-filter happens here (title family, India/remote, freshness).
The strict judgement (stack fit, experience bar, company size, pay) is done
by the Claude scheduled task that reads these files.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .ats import FETCHERS, smartrecruiters_detail
from .boards import jobspy_search

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_company(c, max_age_days):
    kind = c["ats"]
    args = {k: v for k, v in c.items() if k not in ("name", "ats", "notes")}
    if kind == "workday":
        args["max_age_days"] = max_age_days
    t0 = time.time()
    jobs = FETCHERS[kind](c["name"], **args)
    return jobs, round(time.time() - t0, 1)


def matches(job, prof, since):
    title = job["title"]
    if not any(re.search(p, title, re.I) for p in prof["title_include"]):
        return False, "title"
    if any(re.search(p, title, re.I) for p in prof["title_exclude"]):
        return False, "title-excluded"
    loc = job.get("location", "") + " " + job.get("description", "")[:300]
    if not re.search(prof["location_regex"], loc, re.I):
        return False, "location"
    dt = parse_dt(job.get("posted_at"))
    if dt is None:
        job["freshness"] = "posted date unknown - verify"
    elif dt < since:
        return False, "old"
    else:
        job["freshness"] = "fresh"
    return True, ""


def recent_seen(profile, today, days=3):
    seen = set()
    for i in range(1, days + 1):
        f = ROOT / "data" / (today - timedelta(days=i)).isoformat() / f"{profile}.json"
        if f.exists():
            for j in json.loads(f.read_text()):
                seen.add(j["key"])
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--no-boards", action="store_true", help="skip LinkedIn/Naukri/Google")
    a = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    hours = a.hours or cfg.get("window_hours", 24)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    today = now.astimezone(IST).date()
    outdir = ROOT / "data" / today.isoformat()
    outdir.mkdir(parents=True, exist_ok=True)
    health = {"run_at_ist": now.astimezone(IST).isoformat(timespec="minutes"),
              "window_hours": hours, "companies": {}, "boards": {}, "problems": []}

    # ---- 1. career sites, in parallel (each vendor host gets few calls)
    companies = cfg["companies"]
    all_jobs = []

    def work(c):
        try:
            jobs, secs = fetch_company(c, max(2, hours // 24 + 1))
            return c, jobs, secs, None
        except Exception as e:
            return c, [], 0, f"{type(e).__name__}: {str(e)[:200]}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, jobs, secs, err in ex.map(work, companies):
            health["companies"][c["name"]] = {"ats": c["ats"], "total_open": len(jobs),
                                             "seconds": secs, "error": err}
            if err:
                health["problems"].append(f"{c['name']} ({c['ats']}): {err}")
            all_jobs.extend(jobs)

    if a.validate:
        (outdir / "validate.json").write_text(json.dumps(health, indent=1))
        bad = [n for n, h in health["companies"].items() if h["error"] or h["total_open"] == 0]
        print(f"{len(companies) - len(bad)}/{len(companies)} company feeds OK")
        for n in bad:
            print("  BAD:", n, health["companies"][n])
        return 0

    # ---- 2. job boards (discovery)
    if not a.no_boards and cfg.get("boards", {}).get("enabled", True):
        b = cfg["boards"]
        for prof_name, prof in cfg["profiles"].items():
            jobs, errs = jobspy_search(b["sites"], prof["board_terms"], b["location"],
                                       hours, b["results_per_term"])
            for j in jobs:
                j["_profile_hint"] = prof_name
            all_jobs.extend(jobs)
            counts = {}
            for j in jobs:
                counts[j["source"]] = counts.get(j["source"], 0) + 1
            health["boards"][prof_name] = {"counts": counts, "errors": errs}
            for s in b["sites"]:
                if counts.get(f"jobspy-{s}", 0) == 0:
                    health["problems"].append(f"{prof_name}: {s} returned 0 jobs {errs[:1]}")

    # ---- 3. per profile: loose filter, dedupe, cross-day dedupe
    known = {norm(c["name"]) for c in companies}
    summary = {}
    for prof_name, prof in cfg["profiles"].items():
        seen_before = recent_seen(prof_name, today)
        kept, reasons, keys = [], {}, set()
        # ATS rows first so they win the dedupe over board copies
        for j in sorted(all_jobs, key=lambda x: x["source"].startswith("jobspy")):
            if j.get("_profile_hint") not in (None, prof_name):
                continue
            ok, why = matches(j, prof, since)
            if not ok:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            city = norm(j["location"]).split(" ")[0] if j["location"] else ""
            key = f"{norm(j['company'])}|{norm(j['title'])}|{city}"
            if key in keys:
                continue
            keys.add(key)
            if key in seen_before:
                reasons["reported-on-earlier-day"] = reasons.get("reported-on-earlier-day", 0) + 1
                continue
            if j["source"] == "smartrecruiters" and not j["description"]:
                try:
                    smartrecruiters_detail(j)
                except Exception:
                    pass
            j.pop("_detail", None)
            j.pop("_profile_hint", None)
            j["key"] = key
            j["new_company"] = j["source"].startswith("jobspy") and norm(j["company"]) not in known
            kept.append(j)
        kept.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
        (outdir / f"{prof_name}.json").write_text(json.dumps(kept, indent=1, ensure_ascii=False))
        with open(outdir / f"{prof_name}.csv", "w", newline="", encoding="utf-8") as f:
            cols = ["company", "title", "location", "posted_at", "freshness", "source",
                    "job_id", "url", "new_company"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
        summary[prof_name] = {"kept": len(kept), "dropped": reasons,
                              "new_companies": sorted({j["company"] for j in kept if j["new_company"]})}

    health["summary"] = summary
    (outdir / "health.json").write_text(json.dumps(health, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))
    ok_feeds = sum(1 for h in health["companies"].values() if not h["error"])
    print(f"company feeds OK: {ok_feeds}/{len(companies)}; problems: {len(health['problems'])}")
    for p in health["problems"]:
        print("  -", p)
    # fail the workflow (GitHub emails the owner) if most feeds broke
    return 1 if ok_feeds < 0.7 * len(companies) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
