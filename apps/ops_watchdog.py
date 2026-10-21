"""One-shot external dead-man check; invoke via systemd timer/cron/monitor.

Example: python -m apps.ops_watchdog --path /run/bot/heartbeat.json \
    --venue hyperliquid --environment pas --account-id <public_team_address>
Exit 0 means recently-written correctly-scoped heartbeat; exit 2 means stale or
missing. No automatic venue cancellation or alert delivery is implied.
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from observability.events import EventScope
from observability.watchdog import inspect_heartbeat


def main() -> int:
    parser = argparse.ArgumentParser(description="Check external bot heartbeat")
    parser.add_argument("--path", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--tournament-id", default=None)
    parser.add_argument("--max-age-seconds", type=float, default=30)
    args = parser.parse_args()
    result = inspect_heartbeat(
        args.path,
        EventScope(args.venue, args.environment, args.account_id, args.tournament_id),
        max_age=timedelta(seconds=args.max_age_seconds),
    )
    print("HEALTHY" if result.healthy else f"UNHEALTHY: {result.reason}")
    return 0 if result.healthy else 2


if __name__ == "__main__":
    raise SystemExit(main())
