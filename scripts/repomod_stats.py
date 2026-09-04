"""Aggregates RepoModernizer's SK='SUMMARY' items (written by
app.agent.checkpointer.DynamoDBCheckpointer.put_run_summary/note_resume) into
the operational metrics tracked for the project: repos migrated, resume
success rate, avg cost/wall-clock per migration, and the human-gate rejection
rate. Cross-checks against Cost Explorer's actual tagged spend, if available.

Usage: uv run python scripts/repomod_stats.py [--table repomod-checkpoints]
"""
import argparse
import datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr


def fetch_summaries(table_name: str) -> list[dict]:
    table = boto3.resource("dynamodb").Table(table_name)
    items = []
    kwargs = {"FilterExpression": Attr("SK").eq("SUMMARY")}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def month_to_date_tagged_cost() -> float | None:
    ce = boto3.client("ce")
    today = datetime.date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": "project", "Values": ["repomodernizer"]}},
        )
        return sum(float(r["Total"]["UnblendedCost"]["Amount"]) for r in resp["ResultsByTime"])
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", default="repomod-checkpoints")
    args = parser.parse_args()

    summaries = fetch_summaries(args.table)
    if not summaries:
        print(f"No SUMMARY items in {args.table} yet -- run a migration, then re-run this.")
        return

    completed = [s for s in summaries if s.get("status") == "done"]
    migrated = [s for s in completed if int(s.get("files_failed", 0)) == 0 and int(s.get("files_total", 0)) > 0]
    resumed = [s for s in summaries if int(s.get("resume_invocations", 0)) > 0]
    resumed_and_migrated = [s for s in resumed if s in migrated]

    total_approved = sum(int(s.get("files_approved", 0)) for s in summaries)
    total_rejected = sum(int(s.get("files_rejected", 0)) for s in summaries)
    gate_decisions = total_approved + total_rejected

    def avg(values: list[Decimal | float]) -> float:
        return float(sum(values) / len(values)) if values else 0.0

    avg_cost = avg([Decimal(str(s.get("cost_used_usd", 0))) for s in completed])
    wall_clock = [
        float(s["last_updated_at"]) - float(s["started_at"])
        for s in completed if "started_at" in s and "last_updated_at" in s
    ]
    avg_wall_clock_s = avg(wall_clock)

    print(f"Tasks with a run recorded: {len(summaries)}")
    print(f"Repos successfully migrated: {len(migrated)}")
    print(f"Resume attempts (post-crash): {len(resumed)}")
    if resumed:
        print(f"Resume success rate: {len(resumed_and_migrated) / len(resumed):.0%}")
    else:
        print("Resume success rate: n/a (no resumes recorded)")
    print(f"Avg cost per completed run: ${avg_cost:.4f}")
    print(f"Avg wall-clock per completed run: {avg_wall_clock_s / 60:.1f} min")
    if gate_decisions:
        print(f"Human gate rejection rate: {total_rejected / gate_decisions:.0%} ({total_rejected}/{gate_decisions})")
    else:
        print("Human gate rejection rate: n/a (no gate decisions recorded)")

    mtd_cost = month_to_date_tagged_cost()
    if mtd_cost is not None:
        print(f"Cost Explorer, month-to-date tagged spend (project=repomodernizer): ${mtd_cost:.2f}")
    else:
        print("Cost Explorer cross-check unavailable (tag not active yet, or no CE permission).")


if __name__ == "__main__":
    main()
