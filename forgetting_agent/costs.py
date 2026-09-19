"""Internal USD estimates from reported usage, never an account invoice.

Read-only retrospective comparison:
python -m forgetting_agent.costs RUN... --pricing SNAPSHOT --out NEW_JSON
A current tariff applied to an old run is explicitly a revaluation.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "supports_prompt_caching",
)


def fetch_pricing(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """One bounded metadata GET; never send credentials to an inferred other host."""
    snapshot: dict[str, Any] = {
        "observed_at": datetime.now(UTC).isoformat(),
        "model": model,
        "currency": "USD",
        "status": "unavailable",
    }
    if base_url.rstrip("/") != "https://api.tensorx.ai/v1":
        snapshot["reason"] = "No verified pricing adapter for this endpoint"
        return snapshot
    url = base_url.rstrip("/") + "/model/info"
    snapshot["source"] = url
    try:
        request = Request(url, headers={"Authorization": "Bearer " + api_key})
        with urlopen(request, timeout=15) as response:
            data = json.load(response)
        rates = [
            {k: row.get("model_info", {}).get(k) for k in FIELDS}
            for row in data["data"]
            if row["model_name"] == model
        ]
        if not rates or any(row != rates[0] for row in rates):
            raise ValueError("Missing or conflicting model prices")
        snapshot.update(status="available", rates=rates[0])
    except Exception as error:
        # Exception messages can contain URLs/credentials. Save only the class.
        snapshot["reason"] = type(error).__name__
    return snapshot


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _rate(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def estimate(events: list[dict], pricing: dict | None = None) -> dict:
    """Bounds cover known usage only; an unmetered attempt removes the total ceiling."""
    start = next((e for e in events if e["event"] == "run_start"), {})
    provider = start.get("provider") or {}
    revalued = pricing is not None
    pricing = pricing if revalued else provider.get("pricing")
    model = start.get("model")
    rates = (pricing or {}).get("rates", {})
    inp, output, cache, write = (_rate(rates.get(k)) for k in FIELDS[:4])
    supported = (pricing or {}).get("model") == model and inp is not None and output is not None
    if rates.get("supports_prompt_caching") is False:
        cache = inp
    supported = supported and cache is not None
    # Null creation rate is interpreted as ordinary input, explicitly conditional below.
    cold = write if write is not None else inp
    rows = []
    low = Decimal(0)
    high = Decimal(0)
    totals = dict(
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens_reported=0,
        cached_input_tokens=0,
        unknown_cache_input_tokens=0,
    )
    missing = 0
    responses = [e for e in events if e["event"] == "response"]
    response_steps = {e.get("step") for e in responses}
    attempts = [e for e in events if e["event"] == "provider_attempt"]
    supplements = [
        e
        for e in attempts
        if e.get("usage") and (e.get("outcome") != "success" or e.get("step") not in response_steps)
    ]
    for e in [*responses, *supplements]:
        u = e.get("usage") or {}
        p, c = _count(u.get("prompt_tokens")), _count(u.get("completion_tokens"))
        cached = _count((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
        if p is None or (cached is not None and cached > p):
            cached = None
        reasoning = _count((u.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        totals["input_tokens"] += p or 0
        totals["output_tokens"] += c or 0
        totals["reasoning_tokens_reported"] += reasoning or 0
        totals["cached_input_tokens"] += cached or 0
        totals["unknown_cache_input_tokens"] += (p or 0) if cached is None else 0
        missing += int(p is None or c is None)
        row = {
            "step": e.get("step"),
            "input_tokens": p,
            "output_tokens": c,
            "cached_input_tokens": cached,
            "lower_usd": None,
            "upper_usd": None,
        }
        if supported:
            # Completion tokens already include reasoning: do not bill it twice.
            lower = upper = Decimal(c or 0) * output
            if cached is None:
                lower += Decimal(p or 0) * min(inp, cold, cache)
                upper += Decimal(p or 0) * max(inp, cold, cache)
            else:
                lower += Decimal(cached) * cache + Decimal(p - cached) * min(inp, cold)
                upper += Decimal(cached) * cache + Decimal(p - cached) * max(inp, cold)
            low += lower
            high += upper
            row.update(lower_usd=float(lower), upper_usd=float(upper))
        rows.append(row)
    # Failed attempts and successful attempts without a response may still be charged.
    unmetered = sum(e.get("outcome") != "success" and e not in supplements for e in attempts)
    response_steps = {e.get("step") for e in responses}
    unmetered += sum(
        e.get("outcome") == "success"
        and e.get("step") not in response_steps
        and e not in supplements
        for e in attempts
    )
    requested = {e.get("step") for e in events if e["event"] == "request"}
    attempted = {e.get("step") for e in attempts}
    unmetered += len(requested - attempted - response_steps)
    complete = any(e["event"] == "termination" for e in events)
    fixture = provider.get("provider") == "scripted-fixture"
    image_occurrences = sum(
        (e.get("input_metrics") or {}).get("image_count", 0)
        for e in events
        if e["event"] == "request"
    )
    return {
        "image_accounting": {
            "logical_request_image_occurrences": image_occurrences,
            "separate_image_tokens": None if image_occurrences else 0,
            "separate_image_cost_usd": None if image_occurrences else 0,
        },
        "currency": "USD",
        "model": model,
        "pricing": pricing,
        "valuation": "retrospective_tariff" if revalued else "run_start_tariff",
        "status": "not_applicable" if fixture else "estimated" if supported else "unpriced",
        "complete": complete,
        "tokens_reported": totals,
        "responses": len(responses),
        "metered_rejected_attempts": len(supplements),
        "responses_with_missing_usage": missing,
        "unmetered_attempts": unmetered,
        "known_usage_lower_usd": float(low) if supported and not fixture else None,
        "known_usage_upper_usd": float(high) if supported and not fixture else None,
        "total_upper_usd": float(high)
        if supported and not fixture and complete and not missing and not unmetered
        else None,
        "actual_billed_usd": None,
        "assumptions": [
            "Estimate, not invoice; excludes taxes, credits and account adjustments.",
            "Null cache-creation tariff uses ordinary input rate.",
            "Unknown usage/failed requests can add unbounded unobserved cost.",
            "Reasoning is included in completion_tokens, not added again.",
            *(
                [
                    "Image tokens are not estimated from bytes. Pricing uses aggregate provider "
                    "usage; separate image fees or rates are not modeled."
                ]
                if image_occurrences
                else []
            ),
        ],
        "per_response": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--pricing", type=Path, help="Saved model-specific pricing snapshot")
    parser.add_argument(
        "--out", required=True, type=Path, help="New comparison JSON; refuses overwrite"
    )
    args = parser.parse_args()
    pricing = json.loads(args.pricing.read_text()) if args.pricing else None
    reports = []
    for run in args.runs:
        events = []
        with (run / "trace.jsonl").open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    parser.error(f"Incomplete trace: {run}")
                if event["event"] in {
                    "run_start",
                    "request",
                    "response",
                    "provider_attempt",
                    "termination",
                }:
                    events.append(event)
        summary_path = run / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        reports.append(
            {
                "run": str(run),
                "termination": summary.get("termination"),
                "grade": {k: (summary.get("grading") or {}).get(k) for k in ("passed", "cases")},
                "costs": estimate(events, pricing),
            }
        )
    with args.out.open("x") as target:
        json.dump(reports, target, indent=2)
        target.write("\n")
    for report in reports:
        cost = report["costs"]
        print(
            f"{report['run']}: {cost['status']}; known usage USD "
            f"{cost['known_usage_lower_usd']}..{cost['known_usage_upper_usd']}; "
            f"unmetered attempts={cost['unmetered_attempts']}"
        )


if __name__ == "__main__":
    main()
