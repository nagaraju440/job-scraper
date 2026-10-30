"""Diagnostics: save what LinkedIn's public job pages contain around the apply button.
  python -m scraper.debug_li <job_id> [<job_id> ...]
Writes data/debug/linkedin_pages.txt (snippets only, no personal data)."""
import re
import sys
from pathlib import Path

from .boards import _LI, parse_linkedin_job_page

out = []
for jid in sys.argv[1:]:
    r = _LI.get(f"https://www.linkedin.com/jobs/view/{jid}", timeout=30)
    page = r.text
    desc, apply_url = parse_linkedin_job_page(page)
    out.append(f"=== {jid} status={r.status_code} final_url={r.url} len={len(page)} desc_len={len(desc)} apply_url={apply_url!r}")
    for kw in ["applyUrl", "externalApply", "offsite", "apply-button", "jobs-apply", "Apply"]:
        for m in list(re.finditer(re.escape(kw), page))[:3]:
            out.append(f"--- {kw} @{m.start()}: " + page[max(0, m.start() - 200):m.start() + 300].replace("\n", " "))
p = Path(__file__).resolve().parent.parent / "data" / "debug"
p.mkdir(parents=True, exist_ok=True)
(p / "linkedin_pages.txt").write_text("\n".join(out))
print("\n".join(out)[:4000])
