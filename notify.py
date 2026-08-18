"""Send Discord alerts for newly discovered resume-matched jobs."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from scraper import (
    DEFAULT_ASHBY_BOARDS,
    DEFAULT_GREENHOUSE_BOARDS,
    DEFAULT_LEVER_SITES,
    DEFAULT_PROFILE,
    Job,
    load_ashby_boards,
    load_named_watchlist,
    load_profile,
    save_csv,
    scrape,
)


DEFAULT_STATE = Path(__file__).parent / "data" / "seen_jobs.json"
DISCORD_EMBED_LIMIT = 10


def load_seen(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path}")
    return data


def remember_jobs(seen: dict[str, dict[str, str]], jobs: list[Job]) -> None:
    first_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for job in jobs:
        seen.setdefault(
            job.url,
            {
                "first_seen": first_seen,
                "title": job.title,
                "company": job.company,
                "source": job.source,
            },
        )


def save_seen(path: Path, seen: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(dict(sorted(seen.items())), file, indent=2)
        file.write("\n")


def job_embed(job: Job) -> dict:
    description = job.details[:900] if job.details else "No location details listed."
    fields = [
        {"name": "Posted", "value": job.posted or "Unknown", "inline": True},
        {"name": "Source", "value": job.source, "inline": True},
        {"name": "Resume match", "value": str(job.match_score), "inline": True},
    ]
    if job.eligibility_notes:
        fields.append(
            {
                "name": "Eligibility note",
                "value": job.eligibility_notes[:1024],
                "inline": False,
            }
        )
    return {
        "title": f"{job.title} — {job.company}"[:256],
        "url": job.url,
        "description": description,
        "color": 0x4F46E5,
        "fields": fields,
        "footer": {"text": f"Matched: {job.match_reasons}"[:2048]},
    }


def post_discord(webhook_url: str, payload: dict) -> None:
    response = requests.post(webhook_url, json=payload, timeout=20)
    if response.status_code == 429:
        try:
            retry_after = float(response.json().get("retry_after", 1))
        except (ValueError, AttributeError):
            retry_after = 1
        time.sleep(min(retry_after, 10))
        response = requests.post(webhook_url, json=payload, timeout=20)
    response.raise_for_status()


def send_jobs(webhook_url: str, jobs: list[Job]) -> None:
    for start in range(0, len(jobs), DISCORD_EMBED_LIMIT):
        batch = jobs[start : start + DISCORD_EMBED_LIMIT]
        payload = {"embeds": [job_embed(job) for job in batch]}
        if start == 0:
            payload["content"] = f"🔎 jFinder found {len(jobs)} new resume match(es)."
        post_discord(webhook_url, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Send a test message only")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--max-days", type=int, default=2)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit(
            "DISCORD_WEBHOOK_URL is missing. Add it as a GitHub Actions secret."
        )

    if args.test:
        post_discord(
            webhook_url,
            {"content": "✅ jFinder is connected. New job matches will appear here."},
        )
        print("Discord test notification sent.")
        return

    profile = load_profile(DEFAULT_PROFILE)
    boards = load_ashby_boards(DEFAULT_ASHBY_BOARDS)
    greenhouse_boards = load_named_watchlist(DEFAULT_GREENHOUSE_BOARDS)
    lever_sites = load_named_watchlist(DEFAULT_LEVER_SITES)
    jobs = scrape(
        profile,
        boards,
        greenhouse_boards,
        lever_sites,
        max_days=args.max_days,
    )
    save_csv(jobs, Path("jobs.csv"))

    state_exists = args.state.exists()
    seen = load_seen(args.state)
    if not state_exists:
        if not jobs:
            raise SystemExit("No jobs were found, so jFinder did not initialize its history.")
        remember_jobs(seen, jobs)
        save_seen(args.state, seen)
        print(f"Initialized job history with {len(jobs)} current match(es); no alerts sent.")
        return

    new_jobs = [job for job in jobs if job.url not in seen]
    if not new_jobs:
        print("No new resume-matched jobs.")
        return

    send_jobs(webhook_url, new_jobs)
    remember_jobs(seen, new_jobs)
    save_seen(args.state, seen)
    print(f"Sent {len(new_jobs)} new job alert(s) to Discord.")


if __name__ == "__main__":
    main()
