"""Prepare blind research cases from an export; optional explicit, bounded model replay.

python -m trader.replay export.zip --output cases.jsonl
python -m trader.replay cases.jsonl --run --limit 12 --output answers.jsonl
Provider/model/token come from existing OPENTEDDY_* environment variables.
"""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from trader.strategy.contract import parse_proposal
from trader.strategy.openteddy import OpenTeddyProvider
from trader.strategy.provider import ProviderError
from trader.strategy.research import VERSION, normalize_research_input, research_prompt


def prepare(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo("summary.json").file_size > 64 * 1024 * 1024:
            raise ValueError("Export summary too large")
        summary = json.loads(archive.read("summary.json"))
    cases: dict[str, dict[str, Any]] = {}
    for book in summary["activity"].values():
        for result in book.get("shadow_ai", {}).get("results", []):
            if "research_input" not in result:
                continue
            # Select every recorded case, regardless of original decision or future return.
            data = normalize_research_input(result["research_input"])
            canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
            case_id = hashlib.sha256(canonical.encode()).hexdigest()
            if case_id in cases:
                continue
            instructions, context = research_prompt(canonical, "unanchored", case_id)
            cases[case_id] = {
                "case_id": case_id,
                "version": VERSION,
                "instructions": instructions,
                "context": context,
            }
    return sorted(
        cases.values(), key=lambda c: (json.loads(c["context"])["evaluated_at"], c["case_id"])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--run", action="store_true", help="Invoke configured model; may incur fees"
    )
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.limit <= 24:
        parser.error("limit must be 1..24")
    if not args.run:
        cases = prepare(args.input)
        if not cases:
            parser.error("No replayable research_input found")
        with args.output.open("x") as out:
            for case in cases:
                out.write(json.dumps(case, ensure_ascii=False) + "\n")
        print(f"Prepared {len(cases)} blind cases; no model calls")
        return
    # Reconstruct from the allowlist, ignoring any injected labels/instructions in the file.
    run_cases: list[tuple[str, str, str]] = []
    with args.input.open() as source:
        for line in source:
            if len(line) > 65536:
                raise ValueError("Case too large")
            case = json.loads(line)
            data = normalize_research_input(json.loads(case["context"]))
            canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
            case_id = hashlib.sha256(canonical.encode()).hexdigest()
            if case_id != case["case_id"] or case["version"] != VERSION:
                raise ValueError("Case identity/version mismatch")
            instructions, context = research_prompt(canonical, "unanchored", case_id)
            run_cases.append((case_id, instructions, context))
            if len(run_cases) >= args.limit:
                break
    provider = OpenTeddyProvider()
    with args.output.open("x") as out:
        for case_id, instructions, context in run_cases:
            row: dict[str, Any] = {
                "case_id": case_id,
                "version": VERSION,
                "provider": provider.backend,
                "model": provider.model,
                "executable": False,
                "status": "OK",
            }
            try:
                raw = provider.complete(instructions, context)
                parse_proposal(raw, json.loads(context)["snapshot"]["symbol"])
                row["proposal"] = json.loads(raw)
            except (ProviderError, ValueError) as error:
                row.update(status="ERROR", error=str(error))
            row["metadata"] = provider.last_metadata.copy()
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if row["status"] == "ERROR":
                break  # Stop on failures/quota/busy; no retries and no fallback.
    print("Replay saved; no orders were created")


if __name__ == "__main__":
    main()
