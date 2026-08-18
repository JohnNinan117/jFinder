"""Find recent YC startup jobs that match a resume-based profile."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.ycombinator.com"
SOURCE_PAGES = (
    "/jobs/",
    "/jobs/role/software-engineer",
    "/jobs/role/software-engineer/remote",
)
DEFAULT_PROFILE = Path(__file__).with_name("resume_profile.json")
DEFAULT_ASHBY_BOARDS = Path(__file__).parent / "data" / "ashby_boards.txt"
A16Z_URL = "https://portfoliojobs.a16z.com/jobs"
SEQUOIA_URL = "https://jobs.sequoiacap.com/jobs"
GETRO_BOARDS = (
    ("Accel Portfolio Jobs", "https://jobs.accel.com/jobs"),
    ("General Catalyst Jobs", "https://jobs.generalcatalyst.com/jobs"),
)
USER_AGENT = "jFinder/0.1 (personal job search project)"


@dataclass(frozen=True)
class Job:
    title: str
    company: str
    posted: str
    details: str
    match_score: int
    match_reasons: str
    eligibility_notes: str
    url: str
    source: str = "Y Combinator Jobs"


def fetch_page(session: requests.Session, path: str) -> str:
    """Download one public YC jobs page."""
    response = session.get(urljoin(BASE_URL, path), timeout=20)
    response.raise_for_status()
    return response.text


def parse_jobs(html: str) -> list[Job]:
    """Turn the job cards on a YC page into simple Job records."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[Job] = []

    for card in soup.select("li"):
        job_link = card.select_one('a[href^="/companies/"][href*="/jobs/"]')
        if job_link is None:
            continue

        company_links = card.select('a[href^="/companies/"]:not([href*="/jobs/"])')
        company_link = next(
            (link for link in company_links if link.select_one("span.font-bold")),
            company_links[0] if company_links else None,
        )
        company = "Unknown company"
        if company_link is not None:
            company_slug = company_link.get("href", "").rstrip("/").split("/")[-1]
            company = company_slug.replace("-", " ").title()
            name = company_link.select_one("span.font-bold")
            if name is not None:
                company = " ".join(name.stripped_strings)

        posted = "Unknown"
        if company_link is not None:
            posted_label = company_link.select_one("span.text-sm.text-gray-400")
            if posted_label is not None:
                posted = " ".join(posted_label.stripped_strings).strip("() ")

        details_box = card.select_one("div.flex.flex-wrap.items-center")
        details = ""
        if details_box is not None:
            parts = [part.strip() for part in details_box.stripped_strings]
            details = " | ".join(part for part in parts if part != "•")

        jobs.append(
            Job(
                title=" ".join(job_link.stripped_strings),
                company=company,
                posted=posted,
                details=details,
                match_score=0,
                match_reasons="",
                eligibility_notes="",
                url=urljoin(BASE_URL, job_link.get("href", "")),
            )
        )

    return jobs


def matches(job: Job, keywords: tuple[str, ...]) -> bool:
    title = job.title.casefold()
    return any(keyword.casefold() in title for keyword in keywords)


def contains_term(text: str, term: str) -> bool:
    """Match a word or phrase without partial-word false positives."""
    return bool(re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text.casefold()))


def score_job(job: Job, description: str, profile: dict) -> Job:
    """Score one role using the editable resume profile."""
    title = job.title.casefold()
    if any(contains_term(title, term) for term in profile["excluded_title_terms"]):
        return job

    score = 0
    reasons: list[tuple[int, str]] = []
    searchable = f"{job.title} {job.details} {description}"

    for term, weight in profile["preferred_title_terms"].items():
        if contains_term(title, term):
            score += weight
            reasons.append((weight, f"title: {term}"))

    for term, weight in profile["skills"].items():
        if contains_term(searchable, term):
            score += weight
            reasons.append((weight, term))

    for term, weight in profile.get("company_signals", {}).items():
        if contains_term(searchable, term):
            score += weight
            reasons.append((weight, term))

    location_matches = [
        (weight, term)
        for term, weight in profile.get("location_preferences", {}).items()
        if contains_term(job.details, term)
    ]
    if location_matches:
        weight, term = max(location_matches)
        score += weight
        reasons.append((weight, f"location: {term}"))

    experience_matches = [
        (weight, term)
        for term, weight in profile.get("experience_signals", {}).items()
        if contains_term(searchable, term)
    ]
    if experience_matches:
        weight, term = max(experience_matches)
        score += weight
        reasons.append((weight, term))

    eligibility_notes: list[str] = []
    for term, penalty in profile.get("eligibility_review_terms", {}).items():
        if contains_term(searchable, term):
            score += penalty
            eligibility_notes.append(term)

    reasons.sort(key=lambda item: (-item[0], item[1]))
    summary = ", ".join(reason for _, reason in reasons[:8])
    return replace(
        job,
        match_score=score,
        match_reasons=summary,
        eligibility_notes=", ".join(eligibility_notes),
    )


def is_recent(posted: str, max_days: int) -> bool:
    """Return True when YC's relative posting age is within the cutoff."""
    label = posted.casefold()
    if any(unit in label for unit in ("minute", "hour", "just now", "today")):
        return True
    if "yesterday" in label:
        return max_days >= 1

    match = re.search(r"(\d+)\s+days?", label)
    if match:
        return int(match.group(1)) <= max_days

    try:
        published = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - published <= timedelta(days=max_days)


def load_profile(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def load_ashby_boards(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def excluded_title(title: str, profile: dict) -> bool:
    return any(contains_term(title, term) for term in profile["excluded_title_terms"])


def keep_scored(job: Job, description: str, profile: dict) -> Job | None:
    if excluded_title(job.title, profile):
        return None
    scored = score_job(job, description, profile)
    return scored if scored.match_score >= profile["minimum_score"] else None


def scrape_ashby(
    session: requests.Session,
    profile: dict,
    boards: list[str],
    max_days: int,
    delay: float,
) -> list[Job]:
    """Read selected startup boards through Ashby's official public API."""
    found: list[Job] = []
    recent_count = 0
    available_count = 0
    for index, board in enumerate(boards):
        if index:
            time.sleep(delay)
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        try:
            response = session.get(url, params={"includeCompensation": "true"}, timeout=20)
            response.raise_for_status()
        except requests.RequestException as error:
            print(f"Skipping unavailable Ashby board: {board} ({error})")
            continue

        try:
            items = response.json().get("jobs", [])
        except (ValueError, AttributeError) as error:
            print(f"Skipping invalid Ashby response: {board} ({error})")
            continue
        available_count += 1

        for item in items:
            posted = item.get("publishedAt") or "Unknown"
            if not item.get("isListed", True) or not is_recent(posted, max_days):
                continue
            recent_count += 1

            locations = [item.get("location", "")]
            locations.extend(
                location.get("location", "")
                for location in item.get("secondaryLocations", [])
            )
            location_text = " / ".join(dict.fromkeys(filter(None, locations)))
            details = " | ".join(
                filter(
                    None,
                    (
                        item.get("employmentType"),
                        item.get("department"),
                        item.get("team"),
                        location_text,
                        "Remote" if item.get("isRemote") else "",
                        (item.get("compensation") or {}).get(
                            "compensationTierSummary", ""
                        ),
                    ),
                )
            )
            job = Job(
                title=item.get("title", "Untitled role"),
                company=board,
                posted=posted,
                details=details,
                match_score=0,
                match_reasons="",
                eligibility_notes="",
                url=item.get("applyUrl") or item.get("jobUrl") or url,
                source="Ashby",
            )
            scored = keep_scored(job, item.get("descriptionPlain", ""), profile)
            if scored:
                found.append(scored)
    print(
        f"Ashby: checked {len(boards)} board(s), reached {available_count}, "
        f"found {recent_count} recent job(s), kept {len(found)} resume match(es)."
    )
    return found


def find_chrome() -> str | None:
    candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    )
    return next((str(path) for path in candidates if path and Path(path).exists()), None)


def render_dynamic_board(url: str, label: str, marker: str) -> str:
    """Render one public dynamic job board with an installed Chrome browser."""
    chrome = find_chrome()
    if chrome is None:
        print(f"Skipping {label}: Google Chrome or Chromium was not found.")
        return ""

    with tempfile.TemporaryDirectory(prefix="jfinder-chrome-") as profile_dir:
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
            "--virtual-time-budget=8000",
            "--dump-dom",
            url,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=35, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"Skipping {label}: Chrome could not render the board ({error})")
            return ""
    if result.returncode != 0 or marker not in result.stdout:
        print(f"Skipping {label}: the public board did not return job cards.")
        return ""
    return result.stdout


def consider_url(base_url: str, max_days: int) -> str:
    query = urlencode(
        [
            ("jobTypes", "Software Engineer"),
            ("jobTypes", "Engineer"),
            ("postedSince", f"P{max_days}D"),
        ]
    )
    return f"{base_url}?{query}"


def consider_company(card: BeautifulSoup) -> tuple[str, str]:
    company_link = card.select_one(".job-list-job-company-link")
    if company_link:
        return " ".join(company_link.stripped_strings), ""

    group = card.find_parent(class_="grouped-job-result")
    if group is None:
        return "Unknown company", ""
    logo = group.select_one(".grouped-job-company-logo[alt]")
    company = (
        logo.get("alt", "Unknown company").removesuffix(" logo")
        if logo
        else "Unknown company"
    )
    company_context = " ".join(
        element.get_text(" ", strip=True)
        for element in group.select(
            ".job-boards-company-tag, .grouped-job-company-description"
        )
    )
    return company, company_context


def parse_consider_jobs(
    html: str, profile: dict, max_days: int, source: str
) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[Job] = []
    for card in soup.select(".job-list-job"):
        title_link = card.select_one(".job-list-job-title a[href]")
        posted_box = card.select_one(".job-list-badge-posted")
        if not title_link or not posted_box:
            continue

        posted = " ".join(posted_box.stripped_strings).removeprefix("Posted ")
        if not is_recent(posted, max_days):
            continue
        company, company_context = consider_company(card)
        details = " | ".join(
            dict.fromkeys(
                " ".join(element.stripped_strings)
                for element in card.select(
                    ".job-list-badge:not(.job-list-badge-posted), .job-list-job-skill"
                )
                if " ".join(element.stripped_strings)
            )
        )
        job = Job(
            title=" ".join(title_link.stripped_strings),
            company=company,
            posted=posted,
            details=details,
            match_score=0,
            match_reasons="",
            eligibility_notes="",
            url=title_link.get("href", ""),
            source=source,
        )
        searchable = f"{card.get_text(' ', strip=True)} {company_context}"
        scored = keep_scored(job, searchable, profile)
        if scored:
            found.append(scored)
    return found


def parse_a16z_jobs(html: str, profile: dict, max_days: int) -> list[Job]:
    return parse_consider_jobs(html, profile, max_days, "a16z Portfolio Jobs")


def parse_getro_jobs(
    html: str, profile: dict, max_days: int, source: str, board_url: str
) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[Job] = []
    for card in soup.select('[data-testid="job-list-item"]'):
        title_link = card.select_one('[data-testid="job-title-link"][href]')
        company_meta = card.select_one(
            '[itemprop="hiringOrganization"] [itemprop="name"]'
        )
        date_meta = card.select_one('[itemprop="datePosted"]')
        if not title_link or not company_meta or not date_meta:
            continue

        posted = date_meta.get("content", "Unknown")
        if not is_recent(posted, max_days):
            continue
        locations = [
            element.get("content", "")
            for element in card.select('[itemprop="addressLocality"]')
        ]
        tags = [
            element.get_text(" ", strip=True)
            for element in card.select('[data-testid="tag"]')
        ]
        details = " | ".join(dict.fromkeys(filter(None, [*locations, *tags])))
        job = Job(
            title=" ".join(title_link.stripped_strings),
            company=company_meta.get("content", "Unknown company"),
            posted=posted,
            details=details,
            match_score=0,
            match_reasons="",
            eligibility_notes="",
            url=urljoin(board_url, title_link.get("href", "")),
            source=source,
        )
        scored = keep_scored(job, card.get_text(" ", strip=True), profile)
        if scored:
            found.append(scored)
    return found


def discover_ashby_boards(html: str) -> list[str]:
    """Find Ashby board names exposed by a16z job application links."""
    soup = BeautifulSoup(html, "html.parser")
    boards: list[str] = []
    for link in soup.select('a[href*="jobs.ashbyhq.com/"]'):
        parsed = urlparse(link.get("href", ""))
        if parsed.hostname != "jobs.ashbyhq.com":
            continue
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            boards.append(unquote(path_parts[0]))
    return list(dict.fromkeys(boards))


def job_key(job: Job) -> str:
    company = re.sub(r"\s+\([^)]*\)$", "", job.company.casefold())
    normalize = lambda value: re.sub(r"[^a-z0-9]+", "", value.casefold())
    return f"{normalize(company)}|{normalize(job.title)}"


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Keep the richest version when two sources show the same role."""
    unique: dict[str, Job] = {}
    for job in jobs:
        key = job_key(job)
        current = unique.get(key)
        if current is None or job.match_score > current.match_score:
            unique[key] = job
        elif job.match_score == current.match_score and job.source == "Ashby":
            unique[key] = job
    return list(unique.values())


def scrape(
    profile: dict,
    ashby_boards: list[str],
    keywords: tuple[str, ...] | None = None,
    max_days: int = 2,
    delay: float = 0.35,
) -> list[Job]:
    """Fetch recent listings and rank them by resume fit."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    recent: dict[str, Job] = {}

    for index, path in enumerate(SOURCE_PAGES):
        if index:
            time.sleep(delay)
        for job in parse_jobs(fetch_page(session, path)):
            if is_recent(job.posted, max_days):
                recent[job.url] = job

    found: list[Job] = []
    for index, job in enumerate(recent.values()):
        if keywords:
            if matches(job, keywords):
                found.append(job)
            continue

        if excluded_title(job.title, profile):
            continue

        if index:
            time.sleep(delay)
        try:
            html = fetch_page(session, job.url)
        except requests.RequestException as error:
            print(f"Skipping unavailable listing: {job.url} ({error})")
            continue
        description = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        scored_job = score_job(job, description, profile)
        if scored_job.match_score >= profile["minimum_score"]:
            found.append(scored_job)

    if not keywords:
        consider_pages: list[tuple[str, str]] = []
        for source, url in (
            ("a16z Portfolio Jobs", A16Z_URL),
            ("Sequoia Portfolio Jobs", SEQUOIA_URL),
        ):
            html = render_dynamic_board(
                consider_url(url, max_days), source, "job-list-job"
            )
            consider_pages.append((source, html))

        getro_pages: list[tuple[str, str, str]] = []
        for source, url in GETRO_BOARDS:
            search_url = f"{url}?{urlencode({'q': 'engineer'})}"
            html = render_dynamic_board(search_url, source, "job-card")
            getro_pages.append((source, url, html))

        portfolio_jobs: list[Job] = []
        for source, html in consider_pages:
            matches = parse_consider_jobs(html, profile, max_days, source)
            portfolio_jobs.extend(matches)
            print(f"{source}: kept {len(matches)} direct resume match(es).")
        for source, url, html in getro_pages:
            matches = parse_getro_jobs(html, profile, max_days, source, url)
            portfolio_jobs.extend(matches)
            print(f"{source}: kept {len(matches)} direct resume match(es).")

        discovery_html = " ".join(html for _, html in consider_pages)
        discovered_boards = discover_ashby_boards(discovery_html)
        all_ashby_boards = list(dict.fromkeys([*ashby_boards, *discovered_boards]))
        new_boards = [board for board in discovered_boards if board not in ashby_boards]
        print(
            f"Portfolio boards exposed {len(discovered_boards)} Ashby board(s) "
            f"({len(new_boards)} new)."
        )
        found.extend(
            scrape_ashby(session, profile, all_ashby_boards, max_days, delay)
        )
        found.extend(portfolio_jobs)

    return sorted(
        deduplicate_jobs(found),
        key=lambda job: (-job.match_score, job.company.casefold(), job.title.casefold()),
    )


def save_csv(jobs: list[Job], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(Job.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(job) for job in jobs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help="Override resume scoring with a title phrase. Repeat for multiple phrases.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="Resume matching profile (default: resume_profile.json)",
    )
    parser.add_argument(
        "--ashby-boards",
        type=Path,
        default=DEFAULT_ASHBY_BOARDS,
        help="Ashby startup watchlist (default: data/ashby_boards.txt)",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=2,
        help="Oldest posting age to include (default: 2 days)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("jobs.csv"),
        help="CSV destination (default: jobs.csv)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.max_days < 0:
        raise SystemExit("--max-days must be zero or greater")
    keywords = tuple(args.keywords) if args.keywords else None
    profile = load_profile(args.profile)
    boards = load_ashby_boards(args.ashby_boards)
    jobs = scrape(
        profile, boards, keywords=keywords, max_days=args.max_days
    )
    save_csv(jobs, args.output)

    match_type = "title-matched" if keywords else "resume-matched"
    source_counts = Counter(job.source for job in jobs)
    print(
        f"Found {len(jobs)} {match_type} job(s) posted within "
        f"{args.max_days} day(s). Saved to {args.output}"
    )
    if source_counts:
        source_summary = ", ".join(
            f"{source}: {count}" for source, count in sorted(source_counts.items())
        )
        print(f"Sources: {source_summary}")
    for job in jobs:
        score = f"match {job.match_score}" if not keywords else "keyword match"
        print(f"- {job.title} at {job.company} ({job.posted}; {score})")


if __name__ == "__main__":
    main()
