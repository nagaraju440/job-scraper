# job-scraper

Free daily job feed for Nagaraju's and Jhansi's job-search scheduled tasks (replaces Apify).

- **Runs on GitHub Actions** (`.github/workflows/daily.yml`): 05:47 IST main run, 08:17 IST retry of any company site that was down.
- **Sources**
  - ~170 company career sites: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Oracle Cloud, Eightfold (Microsoft) and Amazon (`scraper/ats.py`, list in `config.yaml`).
  - LinkedIn and Naukri via [JobSpy](https://github.com/speedyapply/JobSpy) (`scraper/boards.py`). LinkedIn descriptions are fetched only for jobs that pass the filter. (LinkedIn no longer shows the company's own apply link to logged-out visitors, so `apply_url` is usually empty for LinkedIn rows; the daily task finds the company posting itself. Naukri rows often carry the company link, which feeds auto-discovery.)
- **Output**: `data/<YYYY-MM-DD IST>/nagaraju.json`, `jhansi.json` (+ `.csv`) and `health.json`. Only a loose pre-filter is applied (title family, India/remote, posted in the last 36h - jobs already seen in the previous 3 days are kept and tagged `seen_before`); the Claude scheduled tasks do the real filtering, resumes and referral contacts.
- **Self-growing**: when a LinkedIn/Naukri apply link points at a supported career site, that company's feed is added to `discovered.yaml` and fetched directly from the next day.

## Manual runs
Actions → daily-job-scrape → Run workflow, `mode`:
- `run` (default) – full scrape; `hours` 48/72 to catch up after a missed day
- `retry` – re-fetch only today's failed/down feeds
- `validate` – check every company feed is alive
- `find` – guess career feeds for the names in `companies_to_find.txt` (results in `data/find_feeds.json`, reviewed by hand)
