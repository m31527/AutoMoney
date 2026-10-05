"""Fixed-v4 paired replay. Default prepares cases only; --run-cloud explicitly invokes models."""

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from trader.replay import prepare
from trader.strategy.contract import parse_proposal
from trader.strategy.openteddy import OpenTeddyProvider
from trader.strategy.provider import ProviderError


def compare(rows: list[dict[str, Any]], case_ids: list[str]) -> dict[str, Any]:
    by_key = {(r["case_id"], r["arm"]): r for r in rows}
    paired = []
    for case_id in case_ids:
        local, cloud = by_key.get((case_id, "local")), by_key.get((case_id, "cloud"))
        if local and cloud and local["status"] == cloud["status"] == "OK":
            if local["input_sha256"] != cloud["input_sha256"]:
                raise ValueError("Cannot compare different inputs")
            paired.append(
                {
                    "case_id": case_id,
                    "local": local["proposal"]["action"],
                    "cloud": cloud["proposal"]["action"],
                }
            )
    arms = {}
    for arm in ("local", "cloud"):
        group = [r for r in rows if r["arm"] == arm]
        arms[arm] = {
            "attempted": len(group),
            "failed": sum(r["status"] != "OK" for r in group),
            "actions": dict(Counter(r["proposal"]["action"] for r in group if r["status"] == "OK")),
            "input_tokens": sum(
                r["metadata"]
                .get("usage", {})
                .get("input_tokens", r["metadata"].get("usage", {}).get("prompt_tokens", 0))
                for r in group
            ),
            "output_tokens": sum(
                r["metadata"]
                .get("usage", {})
                .get("output_tokens", r["metadata"].get("usage", {}).get("completion_tokens", 0))
                for r in group
            ),
            "mean_seconds": sum(r["seconds"] for r in group) / len(group) if group else None,
        }
    return {
        "planned_cases": len(case_ids),
        "complete_pairs": len(paired),
        "incomplete_pairs": len(case_ids) - len(paired),
        "action_disagreements": sum(p["local"] != p["cloud"] for p in paired),
        "arms": arms,
        "pairs": paired,
        "cost_usd": None,
        "limitations": "Action differences are not profit or evidence of superiority. "
        "No orders. Tokens are not dollars; failures may also incur unreported usage. "
        "Pair with original ZIP for subsequent outcome analysis; no future labels sent.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--local-model", default=os.environ.get("OPENTEDDY_MODEL", "qwen3.8:27b"))
    parser.add_argument("--cloud-model")
    parser.add_argument("--run-cloud", action="store_true", help="Explicit paid model invocation")
    args = parser.parse_args()
    if not 1 <= args.limit <= 12:
        parser.error("limit must be 1..12 (two calls per case)")
    if args.run_cloud and not args.cloud_model:
        parser.error("--cloud-model is required for --run-cloud")
    cases = prepare(args.export)[: args.limit]  # Chronological, never chosen by future returns.
    if not cases:
        parser.error("No replayable cases")
    providers = (
        {
            arm: OpenTeddyProvider(backend=backend, model=model, replay=True)
            for arm, backend, model in (
                ("local", "ollama", args.local_model),
                ("cloud", "openai", args.cloud_model),
            )
        }
        if args.run_cloud
        else {}
    )
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases))
    (args.output / "plan.json").write_text(
        json.dumps(
            {
                "case_ids": [c["case_id"] for c in cases],
                "selection": "first chronological unique cases",
                "local_model": args.local_model,
                "cloud_model": args.cloud_model,
                "max_calls": len(cases) * 2,
                "executed": args.run_cloud,
            },
            indent=2,
        )
    )
    if not args.run_cloud:
        print(f"Prepared {len(cases)} cases; no model calls. Explicit --run-cloud required.")
        return
    rows: list[dict[str, Any]] = []
    try:
        with (args.output / "answers.jsonl").open("x") as out:
            stop = False
            for index, case in enumerate(cases):
                for arm in ("local", "cloud") if index % 2 == 0 else ("cloud", "local"):
                    provider = providers[arm]
                    started = time.monotonic()
                    row: dict[str, Any] = {
                        "case_id": case["case_id"],
                        "arm": arm,
                        "provider": provider.backend,
                        "model": provider.model,
                        "status": "OK",
                        "input_sha256": hashlib.sha256(
                            (case["instructions"] + case["context"]).encode()
                        ).hexdigest(),
                    }
                    try:
                        raw = provider.complete(case["instructions"], case["context"])
                        parse_proposal(raw, json.loads(case["context"])["snapshot"]["symbol"])
                        row["proposal"] = json.loads(raw)
                    except (ProviderError, ValueError) as error:
                        row.update(status="ERROR", error=str(error))
                        stop = True
                    row.update(
                        metadata=provider.last_metadata.copy(), seconds=time.monotonic() - started
                    )
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    if stop:
                        break
                if stop:
                    break
    finally:
        report = compare(rows, [c["case_id"] for c in cases])
        (args.output / "comparison.json").write_text(json.dumps(report, indent=2))
    print(f"Saved comparison: {report['complete_pairs']} complete pairs; no orders created")


if __name__ == "__main__":
    main()
