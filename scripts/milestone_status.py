#!/usr/bin/env python3
"""Report corpus progress against staged policy-cluster milestones."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from contextlib import closing
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


queue_store = load_script_module("queue_store", SCRIPT_DIR / "queue_store.py")


DEFAULT_MILESTONES = [10_000, 100_000, 500_000, 1_000_000]


def pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def first_pending_milestone(policy_documents: int, milestones: list[int]) -> int:
    for target in milestones:
        if policy_documents < target:
            return target
    return milestones[-1]


def build_stage_plan(
    policy_documents: int,
    active_seeds: int,
    pending_fetches: int,
    ok_fetches: int,
    permanent_errors: int,
    milestones: list[int],
) -> dict:
    target = first_pending_milestone(policy_documents, milestones)
    terminal = ok_fetches + permanent_errors
    success_rate = ok_fetches / terminal if terminal else None
    expected_from_pending = int(pending_fetches * success_rate) if success_rate is not None else None
    remaining = max(target - policy_documents, 0)

    if remaining == 0:
        action = "current milestone complete; advance to the next configured milestone"
        gate = "freeze a status export, failure report, and seed/source yield snapshot before expanding"
    elif active_seeds < target:
        action = "expand and validate more seeds before relying on this stage"
        gate = "active validated seed count must be at least the stage target"
    elif expected_from_pending is not None and expected_from_pending < remaining:
        action = "expand pending active fetches and keep current workers focused on high-yield sources"
        gate = "pending active fetches multiplied by observed terminal success rate should cover the remaining stage gap"
    elif success_rate is not None and success_rate < 0.75:
        action = "pause broad expansion and repair failure modes on a fixed regression set"
        gate = "terminal policy success rate should recover above 75% before scaling this source mix"
    else:
        action = "continue policy-cluster batches until the current milestone is reached"
        gate = "monitor stale running rows, policy success rate, and disk growth while batches run"

    return {
        "current_stage_target": target,
        "current_policy_clusters": policy_documents,
        "remaining_policy_clusters": remaining,
        "active_seed_surplus_for_stage": active_seeds - target,
        "observed_terminal_success_rate": pct(ok_fetches, terminal),
        "expected_policy_clusters_from_pending_at_observed_rate": expected_from_pending,
        "recommended_action": action,
        "stage_gate": gate,
    }


def collect_status(db_path: str | Path, milestones: list[int]) -> dict:
    queue_store.init_db(db_path)
    stats = queue_store.stats(db_path)
    policy_documents = int(stats["policy_documents"])
    active_seeds = int(stats["active_seed_validations"])
    seed_rows = int(stats["seed_rows"])
    pending_fetches = int(stats["pending_fetches"])
    permanent_errors = int(stats["permanent_error_fetches"])
    ok_fetches = int(stats["ok_fetches"])
    attempted_fetches = ok_fetches + permanent_errors

    with closing(queue_store.connect(db_path)) as conn:
        country_status = [
            dict(row)
            for row in conn.execute(
                """
                select country, status, count(*) as count
                from policy_fetch
                group by country, status
                order by country, status
                """
            )
        ]
        source_status = [
            dict(row)
            for row in conn.execute(
                """
                select s.seed_source, f.status, count(*) as count
                from policy_fetch f
                join app_seed s on s.seed_id = f.seed_id
                group by s.seed_source, f.status
                order by s.seed_source, f.status
                """
            )
        ]
        next_claim_countries = [
            dict(row)
            for row in conn.execute(
                """
                select f.country, count(*) as active_pending
                from policy_fetch f
                join seed_validation v on v.seed_id = f.seed_id
                where f.status = 'pending' and v.status = 'active'
                group by f.country
                order by active_pending desc, f.country
                limit 20
                """
            )
        ]
        next_claim_sources = [
            dict(row)
            for row in conn.execute(
                """
                select s.seed_source, count(*) as active_pending
                from policy_fetch f
                join app_seed s on s.seed_id = f.seed_id
                join seed_validation v on v.seed_id = f.seed_id
                where f.status = 'pending' and v.status = 'active'
                group by s.seed_source
                order by active_pending desc, s.seed_source
                limit 20
                """
            )
        ]
        fetch_methods = {
            row["policy_fetch_method"] or "unknown": row["count"]
            for row in conn.execute(
                """
                select policy_fetch_method, count(*) as count
                from policy_document
                group by policy_fetch_method
                """
            )
        }
        accepted_attempt_quality = {
            row["quality"] or "unknown": row["count"]
            for row in conn.execute(
                """
                select coalesce(
                           (
                               select a.quality
                               from policy_url_attempt a
                               where a.fetch_id = d.fetch_id
                                 and a.status = 'accepted'
                               order by a.attempt_index desc
                               limit 1
                           ),
                           'unknown'
                       ) as quality,
                       count(*) as count
                from policy_document d
                group by quality
                """
            )
        }
        document_quality = conn.execute(
            """
            select count(*) as total_documents,
                   sum(case when lower(policy_url) like '%.pdf%'
                              or lower(coalesce(policy_markdown_path, '')) like '%.pdf.md'
                              or exists (
                                  select 1
                                  from policy_link l
                                  where l.document_id = d.document_id
                                    and lower(l.url) like '%.pdf%'
                              )
                            then 1 else 0 end) as pdf_documents,
                   sum(case when coalesce(policy_cluster_nodes_count, 0) > 1 then 1 else 0 end) as documents_with_cluster_nodes,
                   sum(coalesce(policy_cluster_nodes_count, 0)) as total_cluster_nodes,
                   sum(coalesce(policy_cluster_edges_count, 0)) as total_cluster_edges,
                   sum(coalesce(policy_cluster_errors_count, 0)) as total_cluster_errors,
                   min(policy_text_chars) as min_text_chars,
                   avg(policy_text_chars) as avg_text_chars,
                   max(policy_text_chars) as max_text_chars
            from policy_document d
            """
        ).fetchone()
        link_quality = conn.execute(
            """
            select count(*) as total_documents,
                   sum(case when link_count > 0 then 1 else 0 end) as documents_with_links,
                   sum(case when source_link_count > 0 then 1 else 0 end) as documents_with_source_link,
                   min(link_count) as min_links,
                   avg(link_count) as avg_links,
                   max(link_count) as max_links
            from (
                select d.document_id,
                       count(l.link_id) as link_count,
                       sum(case when l.url = d.policy_url then 1 else 0 end) as source_link_count
                from policy_document d
                left join policy_link l on l.document_id = d.document_id
                group by d.document_id
            )
            """
        ).fetchone()
        text_char_buckets = [
            dict(row)
            for row in conn.execute(
                """
                select bucket, count(*) as count
                from (
                    select case
                        when policy_text_chars < 500 then '<500'
                        when policy_text_chars < 1000 then '500-999'
                        when policy_text_chars < 5000 then '1000-4999'
                        when policy_text_chars < 20000 then '5000-19999'
                        else '20000+'
                    end as bucket
                    from policy_document
                )
                group by bucket
                order by case bucket
                    when '<500' then 1
                    when '500-999' then 2
                    when '1000-4999' then 3
                    when '5000-19999' then 4
                    else 5
                end
                """
            )
        ]
        source_yield = [
            dict(row)
            for row in conn.execute(
                """
                select s.seed_source,
                       count(*) as total_fetches,
                       sum(case when f.status = 'ok' then 1 else 0 end) as ok_fetches,
                       sum(case when f.status = 'permanent_error' then 1 else 0 end) as permanent_error_fetches,
                       sum(case when f.status = 'running' then 1 else 0 end) as running_fetches,
                       sum(case when f.status = 'pending' then 1 else 0 end) as pending_fetches
                from policy_fetch f
                join app_seed s on s.seed_id = f.seed_id
                group by s.seed_source
                order by ok_fetches desc, total_fetches desc, s.seed_source
                """
            )
        ]
        country_yield = [
            dict(row)
            for row in conn.execute(
                """
                select f.country,
                       count(*) as total_fetches,
                       sum(case when f.status = 'ok' then 1 else 0 end) as ok_fetches,
                       sum(case when f.status = 'permanent_error' then 1 else 0 end) as permanent_error_fetches,
                       sum(case when f.status = 'running' then 1 else 0 end) as running_fetches,
                       sum(case when f.status = 'pending' then 1 else 0 end) as pending_fetches
                from policy_fetch f
                group by f.country
                order by ok_fetches desc, total_fetches desc, f.country
                limit 50
                """
            )
        ]

    def add_yield_rates(rows: list[dict]) -> list[dict]:
        enriched = []
        for row in rows:
            item = dict(row)
            terminal = int(item.get("ok_fetches") or 0) + int(item.get("permanent_error_fetches") or 0)
            item["terminal_fetches"] = terminal
            item["success_rate_terminal"] = pct(int(item.get("ok_fetches") or 0), terminal)
            enriched.append(item)
        return enriched

    milestone_rows = []
    for target in milestones:
        milestone_rows.append(
            {
                "target_policy_clusters": target,
                "current_policy_clusters": policy_documents,
                "remaining_policy_clusters": max(target - policy_documents, 0),
                "completion_rate": pct(policy_documents, target),
                "active_seed_surplus": active_seeds - target,
                "has_enough_active_seeds": active_seeds >= target,
            }
        )

    return {
        "stats": stats,
        "stage_plan": build_stage_plan(
            policy_documents=policy_documents,
            active_seeds=active_seeds,
            pending_fetches=pending_fetches,
            ok_fetches=ok_fetches,
            permanent_errors=permanent_errors,
            milestones=milestones,
        ),
        "milestones": milestone_rows,
        "rates": {
            "active_seed_rate": pct(active_seeds, seed_rows),
            "policy_success_rate_among_terminal_fetches": pct(ok_fetches, attempted_fetches),
            "policy_attempt_terminal_fetches": attempted_fetches,
            "pending_fetches_per_current_policy_document": pct(pending_fetches, max(policy_documents, 1)),
        },
        "fetch_methods": fetch_methods,
        "accepted_attempt_quality": accepted_attempt_quality,
        "document_quality": {
            "total_documents": int(document_quality["total_documents"] or 0),
            "pdf_documents": int(document_quality["pdf_documents"] or 0),
            "pdf_document_rate": pct(int(document_quality["pdf_documents"] or 0), int(document_quality["total_documents"] or 0)),
            "documents_with_cluster_nodes": int(document_quality["documents_with_cluster_nodes"] or 0),
            "documents_with_cluster_nodes_rate": pct(
                int(document_quality["documents_with_cluster_nodes"] or 0),
                int(document_quality["total_documents"] or 0),
            ),
            "total_cluster_nodes": int(document_quality["total_cluster_nodes"] or 0),
            "avg_cluster_nodes_per_document": pct(
                int(document_quality["total_cluster_nodes"] or 0),
                int(document_quality["total_documents"] or 0),
            ),
            "total_cluster_edges": int(document_quality["total_cluster_edges"] or 0),
            "total_cluster_errors": int(document_quality["total_cluster_errors"] or 0),
            "min_text_chars": int(document_quality["min_text_chars"] or 0),
            "avg_text_chars": round(float(document_quality["avg_text_chars"] or 0), 2),
            "max_text_chars": int(document_quality["max_text_chars"] or 0),
            "text_char_buckets": text_char_buckets,
        },
        "link_quality": {
            "total_documents": int(link_quality["total_documents"] or 0),
            "documents_with_links": int(link_quality["documents_with_links"] or 0),
            "documents_with_links_rate": pct(
                int(link_quality["documents_with_links"] or 0),
                int(link_quality["total_documents"] or 0),
            ),
            "documents_with_source_link": int(link_quality["documents_with_source_link"] or 0),
            "documents_with_source_link_rate": pct(
                int(link_quality["documents_with_source_link"] or 0),
                int(link_quality["total_documents"] or 0),
            ),
            "min_links_per_document": int(link_quality["min_links"] or 0),
            "avg_links_per_document": round(float(link_quality["avg_links"] or 0), 2),
            "max_links_per_document": int(link_quality["max_links"] or 0),
        },
        "next_claim_countries": next_claim_countries,
        "next_claim_sources": next_claim_sources,
        "source_yield": add_yield_rates(source_yield),
        "country_yield": add_yield_rates(country_yield),
        "country_status": country_status,
        "source_status": source_status,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report staged corpus milestone status.")
    parser.add_argument("--db", default=str(queue_store.default_db_path()))
    parser.add_argument("--milestones", default="10000,100000,500000,1000000")
    parser.add_argument("--output-json", default=None)
    return parser


def parse_milestones(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    return values or DEFAULT_MILESTONES


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = collect_status(args.db, parse_milestones(args.milestones))
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
