"""Multi-turn coding experiment with private, fixed per-phase acceptance checks.

One ContextState, Workspace and global request budget for the entire conversation.
Only the next user message is released after a submission; grading never enters history.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .coding import CodingTask, grade
from .experiment import summarise_run, write_run_files
from .loop import LoopConfig, Model, build_system_prompt, rendered_tools_for, run_episode
from .sandbox import Sandbox
from .trace import Trace
from .workspace import Workspace


def phase_verdict(grading: dict, phase: dict) -> dict:
    """Do not infer independence or instruction loss from correlated grader cases."""
    values = {}
    for requirement in phase["active"]:
        cases = [c for c in grading.get("case_results", []) if requirement in c["requirements"]]
        values[requirement] = (
            None
            if not cases or grading["status"] == "inconclusive"
            else all(c["passed"] for c in cases)
        )
    return values


def run_staged(
    task_root: Path,
    schedule: str,
    model: Model,
    config: LoopConfig,
    run_dir: Path,
    sandbox: Sandbox,
    provider_info: dict[str, Any] | None = None,
) -> dict:
    spec = json.loads((task_root / f"{schedule}.json").read_text())
    phases = spec["phases"]
    if not phases or any(not p["message"].strip() for p in phases):
        raise ValueError("Nonempty ordered phase messages required")
    run_dir.mkdir(parents=True, exist_ok=False)
    frozen = run_dir / "inputs"
    frozen.mkdir()
    shutil.copy2(task_root / f"{schedule}.json", frozen / "schedule.json")
    for phase in phases:
        shutil.copy2(task_root / phase["grader"], frozen / phase["grader"])
    workspace = Workspace.from_snapshot(task_root / "snapshot", run_dir, sandbox)
    trace = Trace(run_dir / "trace.jsonl")
    current = 0
    checkpoints = []
    prompt = build_system_prompt(config.arm, "coding_shared.md")

    def checkpoint(answer: str | None, step: int, submitted: bool) -> None:
        phase = phases[current]
        task = CodingTask(
            spec["task_id"], phase["message"], run_dir / "snapshot", frozen / phase["grader"], 240
        )
        grading = grade(task, workspace.root, sandbox, timeout_s=240)
        verdict = phase_verdict(grading, phase)
        cases = json.loads(task.grader_path.read_text())["cases"]
        probes = {}
        for case, outcome in zip(cases, grading.get("case_results", []), strict=True):
            key = hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()
            probes[key] = {
                "name": case["args"][0],
                "passed": outcome["passed"] if grading["status"] != "inconclusive" else None,
            }
        previous = checkpoints[-1]["probes"] if checkpoints else {}
        # A regression needs the SAME test, fixture and expected meaning at both checkpoints.
        regressions = [
            probe["name"]
            for key, probe in probes.items()
            if probe["passed"] is False and previous.get(key, {}).get("passed") is True
        ]
        row = {
            "phase": phase["id"],
            "step": step,
            "submitted": submitted,
            "answer": answer,
            "requirements": verdict,
            "probes": probes,
            "regressions": regressions,
            "revised": phase.get("revised", []),
            "grading": grading,
        }
        destination = run_dir / "checkpoints" / phase["id"]
        destination.mkdir(parents=True)
        shutil.copytree(workspace.root, destination / "workspace")
        (destination / "grade.json").write_text(json.dumps(row, indent=2) + "\n")
        checkpoints.append(row)
        trace.event("phase_grade", **row)

    def next_phase(state, answer: str, step: int) -> str | None:
        nonlocal current
        checkpoint(answer, step, True)
        if current == len(phases) - 1:
            return None
        current += 1
        trace.event("phase_start", phase=phases[current]["id"], after_step=step)
        return phases[current]["message"]

    try:
        trace.event(
            "run_start",
            session_id=run_dir.name,
            task_id=spec["task_id"],
            schedule=schedule,
            model=model.name,
            provider=provider_info or {"provider": "scripted-fixture"},
            config=asdict(config),
            prompt={"text": prompt},
            tools=rendered_tools_for(config.arm, workspace, config.required_submission_criteria),
        )
        trace.event("phase_start", phase=phases[0]["id"], after_step=0)
        result = run_episode(
            phases[0]["message"],
            workspace,
            model,
            config,
            trace,
            system_prompt=prompt,
            on_submission=next_phase,
        )
        if result.termination != "submitted":
            checkpoint(None, result.steps, False)
        summary = summarise_run(
            trace, result, model, config, spec["task_id"], run_dir.name, run_dir
        )
        summary["phase_results"] = checkpoints
        summary["completed_phases"] = sum(c["submitted"] for c in checkpoints)
        summary["total_phases"] = len(phases)
        summary["grading"] = checkpoints[-1]["grading"]
        summary["all_phases_passed"] = summary["completed_phases"] == len(phases) and all(
            c["grading"]["correct"] for c in checkpoints
        )
        summary["schedule"] = schedule
        summary["changes"] = [asdict(c) for c in workspace.changes()]
        (run_dir / "diff.patch").write_text(workspace.diff().text + "\n")
        write_run_files(run_dir, summary, result.state)
        return summary
    finally:
        trace.close()
