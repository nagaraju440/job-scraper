"""One-off helper: guess career feeds for company names.
  python -m scraper.find_feeds companies_to_find.txt
Tries likely board names on Greenhouse, Lever, Ashby and SmartRecruiters and
writes data/find_feeds.json with every hit and its open-job count. Results are
reviewed by hand before anything is added to config.yaml."""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
STOP = r"\b(inc|incorporated|ltd|limited|pvt|private|llc|plc|corp|corporation|co|company|group|technologies|technology|" \
       r"software|solutions|india|global|services|the|labs|holdings)\b"


def slugs(name):
    paren = re.findall(r"\((.*?)\)", name)
    out = []
    for p in paren:  # "PW (PhysicsWallah)" -> also try "physicswallah"
        w = re.findall(r"[a-z0-9]+", re.sub(STOP, " ", p.lower()))
        if w:
            out += ["".join(w), "-".join(w)]
    base = re.sub(r"\(.*?\)", "", name).replace("&", "and").lower()
    core = re.sub(STOP, " ", base)
    for s in (base, core):
        words = re.findall(r"[a-z0-9]+", s)
        if words:
            out += ["".join(words), "-".join(words), words[0]]
    first = out[-1] if out else ""
    out += [first + "careers", first + "jobs", first + "inc", first + "india"]
    return list(dict.fromkeys(x for x in out if len(x) > 1))


def probe(name):
    hits = []
    for s in slugs(name):
        tests = [
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", lambda j: (len(j.get("jobs", [])),
                (j.get("jobs") or [{}])[0].get("company_name", ""))),
            ("lever", f"https://api.lever.co/v0/postings/{s}?mode=json",
             lambda j: (len(j), "") if isinstance(j, list) else (0, "")),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{s}", lambda j: (len(j.get("jobs", [])), "")),
            ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1",
             lambda j: (j.get("totalFound", 0), ((j.get("content") or [{}])[0].get("company") or {}).get("name", ""))),
        ]
        for ats, url, count in tests:
            try:
                r = S.get(url, timeout=20)
                if r.status_code == 200:
                    n, label = count(r.json())
                    if n and n > 0:
                        hits.append({"ats": ats, "token": s, "open": n, "label": label})
            except Exception:
                pass
            time.sleep(0.2)
    return name, hits


def main():
    names = [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = dict(ex.map(probe, names))
    out = ROOT / "data" / "find_feeds.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    found = {k: v for k, v in res.items() if v}
    print(f"{len(found)}/{len(names)} companies have at least one candidate feed")
    for k, v in found.items():
        print(k, "->", v)


if __name__ == "__main__":
    main()
