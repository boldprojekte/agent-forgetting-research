# Agent-controlled forgetting: research artifacts

Jan-Peter Franke · Münster, Germany · 19 September 2026

This repository accompanies *Agent-Controlled Forgetting for Tool-Using Agents: Reversible Context Curation in Practice*. The paper and versioned artifact archives are available from the [release page](https://github.com/boldprojekte/agent-forgetting-research/releases/tag/v1.0.0). arXiv submission is in progress.

## What can be checked here

- `python paper/verify_artifacts.py` recomputes the main OpenTelemetry prompt/token claims, checks 306 figure rows and checks 37 frozen input hashes. It makes no network request and does not grade candidates.
- `paper/figures/otel-prompt-series.csv` contains every reported request's prompt and completion count for the main pair.
- `runs/otel-checkout-noise-03-{method,retained}/` contains original primary/follow-up summaries, cost records, grades and the final submitted workspace. **Follow-up summaries include the primary usage cumulatively. Do not add both summaries.** The final workspace is after both phases; it is not a preserved primary-only submission.
- `archive-audit.json` contains per-result notes, text lengths and original-payload hashes. Original archive payloads are not included, so equality with withheld traces is an author-verified claim, not externally reproducible from these hashes alone.
- Supporting Planroom and Sphinx numeric records and adverse browser reports are included. Complete supporting-run trajectories and manual grading materials are not included.

`FILE_MANIFEST.json` records the copied source files. `PACKAGE_SHA256.json` records the final package contents, including generated documentation. Original files are preserved byte for byte unless explicitly identified as derived. Some historical reports contain paths/links into the full private research workspace; they are provenance references, not working public URLs.

## Offline mechanism checks

Use Python 3.11 or later in an isolated environment. Install the project with its development extras (`pip install -e '.[dev]'`) and run:

```sh
python -m pytest tests/test_context_apply.py tests/test_context_recover.py tests/test_image_context.py tests/test_costs.py
```

These tests cover archival/recovery, image handling and usage accounting. They do not establish task quality or successful recovery decisions by the model. The package intentionally includes only this focused subset of the full research suite.

To rebuild the published results figure, use `uv run --no-project paper/build_figures.py`. The script uses existing records, not inference.

## Inspecting or rerunning the main experiment

The pinned benchmark source, runtime overlays, task specifications, private graders, prompts and runner are under `experiments/otel-checkout-noise-01`, `experiments/otel-checkout-noise-03`, `scripts`, and `forgetting_agent`. The `private` directory name means hidden from the evaluated agent, not that these files should be concealed from a research reader. The frozen manifest is under `evaluation/otel-checkout-noise-03`.

Live reruns require Linux, Docker Compose, sufficient local resources, Node.js, the pinned Playwright MCP dependencies and browser, and a funded compatible provider account. Install `.[mcp]`, install the browser integration from its lockfile, and follow the experiment protocol after inspecting its commands. Provider credentials must be supplied separately; no credential file is distributed. The author used TensorX model identifier `z-ai/glm-5.3-flash`; future model identity and availability are not guaranteed. A live run spends API credit. **No command in this README starts one automatically.** A clean-host reproduction has not been validated during packaging.

## Interpretation

The main comparison is one sequential pair with different observations and generated code. The primary oracle checks two behaviors; follow-up grades mix process probes with syntax-sensitive checks. Neither statistical equivalence nor general superiority is established. The runtime probes are stronger evidence than the aggregate 3/6 versus 2/6 follow-up score.

## Exclusions

No `.env` containing provider credentials, full provider transcript, private Arios source, personal research conversations, browser image archive, downloaded model, browser binary or third-party literature PDF is included. `.env.benchmark` files contain benchmark configuration, not provider access credentials. Existing OpenTelemetry licenses and notices are retained alongside its source. See `LICENSING.md` for the license applying to each part.
