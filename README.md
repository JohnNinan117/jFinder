# jFinder

A small Python scraper that finds recent startup jobs and ranks them against
John's resume.

The first version reads Y Combinator's public jobs directory. Those listings
link to YC's Work at a Startup application flow. It searches the main,
software-engineering, and remote software-engineering pages, removes duplicate
listings, keeps only jobs posted within the last two days, and scores each job
description using the skills and target roles in `resume_profile.json`.

It also searches selected startup boards through Ashby's official public API,
plus the a16z, Sequoia, Accel, and General Catalyst portfolio job boards. The
portfolio boards are dynamic, so those sources use an installed Google Chrome
or Chromium in headless mode; if no browser is found, YC and Ashby still run.
Large-company listings receive a strong score penalty because the goal is a
higher chance of review by a small hiring team, not maximum job volume.

## Setup

You need Python 3.10 or newer. From this project folder, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Run it

```bash
python3 scraper.py
```

The profile is based on the supplied resume: Python, SQL, React, Node.js,
TypeScript, data/ETL work, APIs, Linux, and networking. It gives extra weight to
FDE, solutions, full-stack, product, backend, data, and new-grad roles. Senior,
staff, principal, founding-engineer, management, and executive titles are
skipped by default. Listings that state a minimum of more than one year of
experience are also skipped; change `maximum_required_experience_years` in
`resume_profile.json` if that cutoff changes.

The CSV includes a numeric `match_score` and plain-language `match_reasons`.
Edit `resume_profile.json` whenever the resume or job targets change.

Edit `data/ashby_boards.txt` to add an Ashby startup. For example, the board
name in `https://jobs.ashbyhq.com/Mintlify` is `Mintlify`. The starter
watchlist contains 22 verified boards. Each run also finds Ashby application
links in the current a16z results and searches those companies' complete public
boards automatically, so the source list can grow without manual edits. Every
run prints how many boards were discovered and reached, how many jobs met the
two-day cutoff, and how many survived resume scoring.

Location scoring prioritizes San Francisco and the Bay Area, followed by New
York and Texas. Toronto and remote roles remain eligible. US jobs are not
removed merely because they do not offer traditional visa sponsorship: a
possible TN case is left for manual review. Explicit citizenship, clearance,
or existing-work-authorization language is penalized and shown in the
`eligibility_notes` CSV column.

Two days is the default maximum age. You can choose a different cutoff:

```bash
python3 scraper.py --max-days 7
```

To bypass resume scoring and choose exact title phrases, repeat `--keyword`:

```bash
python3 scraper.py --keyword "forward deployed" --keyword "product engineer"
```

To save somewhere else:

```bash
python3 scraper.py --output results/yc_jobs.csv
```

## Discord alerts while your computer is off

The workflow in `.github/workflows/job-alerts.yml` runs on GitHub's servers at
17 minutes past every hour. The first normal run records the current matches
without sending a flood of old jobs. Later runs post only newly seen,
resume-matched jobs to Discord and commit the URL history in
`data/seen_jobs.json`.

1. In Discord, open your private job-alert channel and choose **Edit Channel →
   Integrations → Webhooks → New Webhook**, then copy its webhook URL.
2. On GitHub, open the jFinder repository and choose **Settings → Secrets and
   variables → Actions → New repository secret**.
3. Name the secret `DISCORD_WEBHOOK_URL`, paste the webhook URL, and save it.
4. Push these project changes to GitHub.
5. Open **Actions → jFinder Discord alerts → Run workflow**, select the test
   notification option, and run it. You should immediately receive a Discord
   message.
6. Run it once more without the test option to initialize the job history.

Never paste the Discord webhook into a project file or commit it to Git. GitHub
may delay scheduled jobs during busy periods, and it disables scheduled
workflows in public repositories after 60 days without repository activity.

Job sites change their HTML occasionally. If YC changes its job cards, the
parser in `scraper.py` may need a small update. Please keep the request rate
low and use the results for personal job research.
