"""Daily job scrape. Usage:
  python -m scraper.run                  # normal daily run
  python -m scraper.run --validate       # check every company feed is alive (no filtering)
  python -m scraper.run --hours 72       # wider window (catch-up after a missed day)
  python -m scraper.run --retry-failed   # re-fetch only the feeds that failed/were down
                                         # earlier today and merge them into today's files

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

from .ats import FETCHERS, FeedDown, feed_from_url, feed_key, smartrecruiters_detail
from .boards import jobspy_search, linkedin_details

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
CSV_COLS = ["company", "title", "location", "posted_at", "freshness", "source",
            "job_id", "url", "apply_url", "new_company", "seen_before"]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except ValueError:
        return None


def is_date_only(s):
    """Workday / job boards give a date, not a time."""
    return bool(s) and bool(re.search(r"T00:00:00", str(s)))


def fetch_company(c, max_age_days):
    kind = c["ats"]
    args = {k: v for k, v in c.items() if k not in ("name", "ats", "notes", "search")}
    if kind == "workday":
        args["max_age_days"] = max_age_days
    t0 = time.time()
    jobs, meta = FETCHERS[kind](c["name"], **args)
    return jobs, meta, round(time.time() - t0, 1)


def matches(job, prof, since):
    title = job.get("title") or ""
    if not any(re.search(p, title, re.I) for p in prof["title_include"]):
        return False, "title"
    if any(re.search(p, title, re.I) for p in prof["title_exclude"]):
        return False, "title-excluded"
    loc = (job.get("location") or "") + " " + (job.get("description") or "")[:400]
    if not re.search(prof["location_regex"], loc, re.I):
        return False, "location"
    raw = job.get("posted_at")
    dt = parse_dt(raw)
    if dt is None:
        job["freshness"] = "posted date unknown - verify"
    elif is_date_only(raw):
        # compare calendar dates in IST so "posted yesterday" is not lost to a strict 24h cut
        if dt.astimezone(IST).date() < since.astimezone(IST).date():
            return False, "old"
        job["freshness"] = "fresh"
    elif dt < since:
        return False, "old"
    else:
        job["freshness"] = "fresh"
    return True, ""


def job_key(j):
    city = norm(j.get("location")).split(" ")[0] if j.get("location") else ""
    return f"{norm(j['company'])}|{norm(j['title'])}|{city}"


def recent_seen(profile, today, days=3):
    """{job key: earliest earlier date it appeared in a scraper file}"""
    seen = {}
    for i in range(days, 0, -1):
        d = (today - timedelta(days=i)).isoformat()
        f = ROOT / "data" / d / f"{profile}.json"
        if f.exists():
            for j in json.loads(f.read_text()):
                seen.setdefault(j["key"], d)
    return seen


def fetch_all(companies, hours, health):
    jobs_out = []

    def work(c):
        try:
            jobs, meta, secs = fetch_company(c, max(2, hours // 24 + 1))
            return c, jobs, meta, secs, None, None
        except FeedDown as e:
            return c, [], {}, 0, None, f"DOWN: {str(e)[:160]}"
        except Exception as e:
            return c, [], {}, 0, f"{type(e).__name__}: {str(e)[:200]}", None

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, jobs, meta, secs, err, down in ex.map(work, companies):
            health["companies"][c["name"]] = {"ats": c["ats"], "open": meta.get("open"),
                                             "fetched": len(jobs), "seconds": secs,
                                             "error": err, "down": down, **{k: v for k, v in meta.items() if k != "open"}}
            if err:
                health["problems"].append(f"{c['name']} ({c['ats']}): {err}")
            if down:
                health["problems"].append(f"{c['name']} ({c['ats']}): {down} - will retry")
            jobs_out.extend(jobs)
    return jobs_out



def write_profiles(cfg, all_jobs, names, outdir, today, since, retry):
    """Loose per-profile filter, de-duplication and the json/csv outputs. Returns the summary."""
    known = {norm(n) for n in names}
    summary = {}
    for prof_name, prof in cfg["profiles"].items():
        seen_before = recent_seen(prof_name, today)
        existing = []
        pf = outdir / f"{prof_name}.json"
        if retry and pf.exists():
            existing = json.loads(pf.read_text())
        kept, reasons, keys = list(existing), {}, {j["key"] for j in existing}
        # career-site rows first so they win the dedupe over job-board copies
        for j in sorted(all_jobs, key=lambda x: x["source"].startswith("jobspy")):
            if j.get("_profile_hint") not in (None, prof_name):
                continue
            orig, j = j, dict(j)
            ok, why = matches(j, prof, since)
            if not ok:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            key = job_key(j)
            if key in keys:
                reasons["duplicate"] = reasons.get("duplicate", 0) + 1
                continue
            keys.add(key)
            if key in seen_before:
                # tag, never drop: the daily task de-duplicates against its own trackers,
                # and a scraper file it never read (late or test run) must not hide jobs
                j["seen_before"] = seen_before[key]
                reasons["seen-on-earlier-day (kept, tagged)"] = reasons.get("seen-on-earlier-day (kept, tagged)", 0) + 1
            if j["source"] == "smartrecruiters" and not j.get("description"):
                try:
                    smartrecruiters_detail(j)
                    orig["description"] = j["description"]  # cache: fetched once per run
                    orig.pop("_detail", None)
                except Exception as e:
                    j["description_error"] = f"{type(e).__name__}"
            j.pop("_detail", None)
            j.pop("_profile_hint", None)
            j["key"] = key
            j["new_company"] = j["source"].startswith("jobspy") and norm(j["company"]) not in known
            if not j.get("description"):
                j["description_missing"] = True
            kept.append(j)
        kept.sort(key=lambda x: str(x.get("posted_at") or ""), reverse=True)
        pf.write_text(json.dumps(kept, indent=1, ensure_ascii=False))
        with open(outdir / f"{prof_name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
        summary[prof_name] = {"kept": len(kept), "added_now": len(kept) - len(existing),
                              "dropped": reasons,
                              "no_description": sum(1 for j in kept if j.get("description_missing")),
                              "new_companies": sorted({j["company"] for j in kept if j.get("new_company")})}

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-boards", action="store_true", help="skip LinkedIn/Naukri/Google")
    ap.add_argument("--skip-if-done", action="store_true",
                    help="scheduled backup runs: exit if today's full run already finished")
    a = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    hours = a.hours or cfg.get("window_hours", 24)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    today = now.astimezone(IST).date()
    outdir = ROOT / "data" / today.isoformat()
    if a.skip_if_done and (outdir / "health.json").exists():
        h = json.loads((outdir / "health.json").read_text())
        if "summary" in h and "phase" not in h and h.get("run_at_ist", "")[11:13] >= "04":
            print(f"today's run already finished at {h['run_at_ist']} - nothing to do")
            return 0
    outdir.mkdir(parents=True, exist_ok=True)
    companies = list(cfg["companies"])
    names = [c["name"] for c in companies]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"config.yaml lists these companies twice: {sorted(dupes)}")
    # feeds learned automatically from job-board apply links on earlier days
    disc_path = ROOT / "discovered.yaml"
    discovered = (yaml.safe_load(disc_path.read_text()) or []) if disc_path.exists() else []
    known_feeds = {feed_key(c) for c in companies}
    for d in discovered:
        if feed_key(d) not in known_feeds and d["name"] not in names:
            companies.append({k: v for k, v in d.items() if k not in ("added", "from_job")})
            known_feeds.add(feed_key(d))
            names.append(d["name"])

    prev_health = None
    if a.retry_failed:
        hp = outdir / "health.json"
        if not hp.exists():
            print("no run today yet - nothing to retry")
            return 0
        prev_health = json.loads(hp.read_text())
        failed = {n for n, h in prev_health["companies"].items() if h.get("error") or h.get("down")}
        companies = [c for c in companies if c["name"] in failed]
        print(f"retrying {len(companies)} feeds: {[c['name'] for c in companies]}")
        if not companies:
            return 0

    health = {"run_at_ist": now.astimezone(IST).isoformat(timespec="minutes"),
              "window_hours": hours, "companies": {}, "boards": {}, "problems": []}

    all_jobs = fetch_all(companies, hours, health)

    if a.validate:
        (outdir / "validate.json").write_text(json.dumps(health, indent=1))
        bad = [n for n, h in health["companies"].items() if h["error"] or h["down"] or not h["open"]]
        print(f"{len(companies) - len(bad)}/{len(companies)} company feeds OK")
        for n in bad:
            print("  BAD:", n, health["companies"][n])
        return 0

    # checkpoint: career-site results are saved now, so a later crash/timeout never loses them
    if not a.retry_failed:
        partial = write_profiles(cfg, all_jobs, names, outdir, today, since, False)
        (outdir / "health.json").write_text(json.dumps(
            {**health, "phase": "career sites done - job boards still running", "summary": partial}, indent=1))

    # ---- job boards (discovery) - not repeated on a retry run
    if not a.retry_failed and not a.no_boards and cfg.get("boards", {}).get("enabled", True):
        b = cfg["boards"]
        deadline = time.time() + 60 * b.get("max_minutes", 40)
        for prof_name, prof in cfg["profiles"].items():
            jobs, errs = jobspy_search(b["sites"], prof["board_terms"], b["location"],
                                       hours, b["results_per_term"], deadline=deadline)
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

        # LinkedIn: description + company apply link only for jobs that pass the loose filter
        want = {}
        for j in all_jobs:
            if j["source"] == "jobspy-linkedin" and not j.get("description"):
                prof = cfg["profiles"].get(j.get("_profile_hint"))
                if prof and matches(dict(j), prof, since)[0]:
                    want.setdefault(j["job_id"], []).append(j)
        got, errs = linkedin_details(list(want), deadline=deadline + 60 * b.get("details_minutes", 20))
        for jid, (desc, apply_url) in got.items():
            for j in want[jid]:
                j["description"] = desc
                if apply_url:
                    j["apply_url"] = apply_url
        health["boards"]["linkedin_details"] = {"wanted": len(want), "fetched": len(got),
                                                "with_apply_link": sum(1 for v in got.values() if v[1]),
                                                "errors": errs[:10]}
        if want and len(got) < 0.5 * len(want):
            health["problems"].append(f"linkedin details: only {len(got)}/{len(want)} descriptions fetched")

    # ---- learn new company feeds from job-board apply links (relevant titles only)
    new_feeds = []
    for j in all_jobs:
        if not j["source"].startswith("jobspy") or not j.get("company"):
            continue
        prof = cfg["profiles"].get(j.get("_profile_hint"))
        if not prof or not any(re.search(p, j["title"], re.I) for p in prof["title_include"]):
            continue
        src_url = j.get("apply_url") or j.get("url")
        f = feed_from_url(src_url)
        if not f or feed_key(f) in known_feeds or j["company"] in names:
            continue
        entry = {"name": j["company"], **f, "added": today.isoformat(), "from_job": src_url}
        new_feeds.append(entry)
        known_feeds.add(feed_key(f))
        names.append(j["company"])
    if new_feeds:
        disc_path.write_text(yaml.safe_dump(discovered + new_feeds, sort_keys=False, allow_unicode=True))
    health["new_feeds_discovered"] = [f"{f['name']} ({f['ats']})" for f in new_feeds]

    summary = write_profiles(cfg, all_jobs, names, outdir, today, since, a.retry_failed)

    if prev_health:  # merge retry results into today's health
        for n, h in health["companies"].items():
            prev_health["companies"][n] = h
        prev_health["problems"] = [p for p in prev_health["problems"]
                                   if not any(p.startswith(n + " (") for n in health["companies"])] + health["problems"]
        prev_health["retry_at_ist"] = health["run_at_ist"]
        prev_health["summary_after_retry"] = summary
        health = prev_health
    else:
        health["summary"] = summary
    (outdir / "health.json").write_text(json.dumps(health, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))
    feeds = health["companies"]
    ok_feeds = sum(1 for h in feeds.values() if not h.get("error") and not h.get("down"))
    print(f"company feeds OK: {ok_feeds}/{len(feeds)}; problems: {len(health['problems'])}")
    for p in health["problems"]:
        print("  -", p)
    # fail the workflow (GitHub emails the owner) if many feeds broke
    return 1 if ok_feeds < 0.8 * len(feeds) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
