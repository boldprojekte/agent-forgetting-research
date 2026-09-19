---
title: "Manuscript claim and evidence map"
created: 2026-09-19
updated: 2026-09-19
type: evidence
tags: [research]
sources: ["manuscript.md", "../evaluation/otel-checkout-noise-03/manifest.json", "../literature/verified-for-draft.md"]
---

# Manuscript claim and evidence map

[Manuscript](manuscript.md) · [Project index](../index.md) · [Current status](../status.md)

## Development provenance and author rationale

| Source | Coverage in the revised manuscript | Evidence boundary |
|---|---|---|
| [Development record](../evidence/provenance.md) | Introduction: one sentence on the Arios origin and February 2026 implementation history. Exact milestones and full hashes remain in the companion development record. | Git metadata and selected source presence were rechecked on September 19. Not a first-conception date, public-disclosure date, worldwide priority claim, or execution test of old versions. The March integration scope follows the documented historical audit. |
| [Initial discussion](../notes/discussion-001.md) | Arios origin, broad exposure followed by selection, ephemeral reminders, history-edit/cache distinction. | Earlier conception is an author report; no invented conception date or human-memory mechanism. |
| [Author clarification](../notes/discussion-001.md) and [original desk analogy](../raw/transcripts/forget-mechanism-001.md) | Introduction: learn from human strengths and exploit software-specific control; organized desk, explicit filing, notes and retrieval references. | Author design position and analogy, not a neuroscience claim, universal description of agents, or proof of correct selection. |
| [Tool-noise rationale](../notes/tool-noise-rationale.md) | Introduction and Discussion: curation can operate on verbose external tools without redesigning the producer; relevance can emerge after inspection. | Motivation and interface property, not comparative superiority over filtering or arbitrary MCP tools. |
| [Reversible compaction and task transitions](../notes/reversible-compaction.md) | Method and Discussion: individual notes, batch selection, preserved surrounding conversation, recovery, next-task-informed cleanup, separate cost/latency accounting. | Active notes are lossy; archived payloads remain retrievable. No guaranteed quality improvement or measured TTFT effect. |

This is a targeted coverage check of the central author notes and provenance record, not a claim that every historical discussion or pilot belongs in the manuscript. Future autonomous whole-session compaction and persistent handoff notes are outside the evaluated interface and are not presented as implemented results.

## Main empirical claims

| Claim | Primary evidence | Boundary |
|---|---|---|
| OTel final measured prompt 912,492 retained, 231,951 method | Each `runs/otel-checkout-noise-03-{retained,method}/followup/summary.json`, last `usage` element | Input before final generation, not post-answer context or reasoning capacity. |
| Input falls 49.77%; final prompt falls 74.58% | Same usage lists; [pair metrics](../evidence/otel-checkout-noise-03-pair/metrics.json) | One pair, different actions and observations. |
| Cost $4.378–4.379 versus $1.283–1.436 | Each run's `costs.json`, rates and cache records | Tariff estimate; incomplete cache detail creates range; local compute excluded. |
| Both primary candidates pass 2/2 | Each run's `primary-grade.json` and `primary-grade.txt` | Two tested behaviors, not exhaustive task completion or equivalence. |
| Method passes SIGTERM/SIGINT, retained fails | Each run's `followup-grade.json` and `followup-grade.txt` | Specific isolated runtime probes, not general graceful-shutdown correctness. |
| Retained passes startup conflict; method times out | Same follow-up grade files | Separate real functional outcome; no need to rely on static patterns. |
| Mixed follow-up totals 2/6 and 3/6 | `scripts/grade_otel_currency.py`, criteria construction and source audit | Dependent criteria, regex/static heuristics. Not a six-item independent behavioral accuracy score. |
| Nine successful batches, 26 stored originals | Method final `archive.json`, trace `context_transition` and `tool_result` events | Storage integrity is distinct from note adequacy; no recovery in this pair. |
| 2,247,229 original text characters, 9,709 note characters | Sum `original_text` and `note` lengths in final archive | Character counts, not token counts; stub/call overhead excluded. |
| Context jump occurs late in retained primary | Retained trace: primary step 74 `browser_find`, 2,478,818 text characters; primary submission step 77 | Strong primary quality at a large final prompt is not sustained primary implementation under that load. |
| Planroom has no compression/cost advantage | [Pair metrics](../evidence/planroom-v12-pair/metrics.json) and [independent report](../evidence/planroom-07-retained-v12.md) | Journey coverage with file-picker and fault-injection gaps; different model-written tests. |
| Sphinx six-session outcomes | [Metrics](../evidence/sphinx-manifest-01/metrics.json), [protocol](../experiments/sphinx-manifest-01.md), phase artifacts under `runs/sphinx-manifest-01-*` | Three sessions per arm, one authored task, older harness/output configuration. |
| Lower context can accompany worse assessed quality | [Browser fourth-task report](../evidence/browser-fourth-01.md) and manual assessment linked there | Unblinded rubric, inherited histories differ. |

## Method and configuration

- [Frozen OTel input hashes](../evaluation/otel-checkout-noise-03/manifest.json): 37 files, 605-file snapshot, reference/baseline preflight.
- [Context operations](../forgetting_agent/context.py): stable IDs, batch validation, archive, in-place stubs and recovery.
- [Loop and provider projection](../forgetting_agent/loop.py): actual runtime reminder location and eligibility, protected persistent history, limits.
- [Method instructions](../forgetting_agent/prompts/method_section.md): prompted policy, estimated noise heuristic, batching and recovery guidance.
- [Runner](../scripts/run_otel_checkout_noise.py): 1,500 calls and 1,800 attempts, 131,072 output tokens, low requested reasoning, no seed/temperature. The prior conversational reference to a 300-call guard does not describe this runner.
- [Primary grader](../scripts/grade_otel_checkout.py) and [currency grader](../scripts/grade_otel_currency.py): private checks, source-pattern limitations.

## Development and exclusions

| Episode family | Treatment in this draft | Reason |
|---|---|---|
| OTel 03 | Main case | Completed pair, frozen repaired instrument, both candidates independently graded. |
| OTel 01 | Development disclosure | Currency tools used the wrong build surface and blocked required headers; primary error contract incomplete. [Report](../evidence/otel-checkout-noise-01-pair.md). |
| OTel 02 retained | Excluded development run | Follow-up contract refined before final pair; no matching method episode. |
| Planroom V12 | Contrasting completed pair | Negative resource result retained. |
| Earlier Planroom | Harness-development history | Step cap, output cap, history JSON and completion behavior changed conditions. [Reports index](../evidence/README.md). |
| Sphinx manifest six-session batch | Separate descriptive table | All six results retained; not pooled with later runs. |
| Browser fourth task and selected repeat | Adverse observation and selection disclosure | Original lower quality kept; post-outcome method repeat does not replace it. |
| Other pilots | Available in evidence index, not pooled | Prompt versions, provider failures and graders differ; no claim of comprehensive meta-analysis. |

## Literature and remaining publication work

[Verified literature record](../literature/verified-for-draft.md) maps the original six papers and six additional research/software/documentation references to inspected primary material. [BibTeX](references.bib) is generated from fetched metadata, preserved with hashes in [raw metadata](../raw/literature/paper-draft-2026-09-19/metadata.json). The targeted review is not an exhaustive novelty audit. No claim that archives, recovery, stable IDs, or acting-agent curation are individually novel is supported.

The current artifact is a prepared preprint candidate with a local PDF and source bundle; it has not been submitted or accepted. Next research decisions are balanced replication and better behavioral follow-up coverage. Author identity and location are supplied. The targeted closest-work review has been expanded, and local artifact packaging and neutral LaTeX formatting are prepared. Author approval, licensing and public deposit remain outstanding; the arXiv account is created and email verified. No additional paid run or public release was started while drafting.

## Figure provenance

Run `uv run --no-project paper/build_figures.py` from the workspace to rebuild the figure. The script reads recorded summary/metrics JSON only and makes no inference requests. It emits PNG, SVG and vector PDF plus a 306-row per-request CSV. Both panel data and the source summaries remain available. The PNG was visually checked for readable axes, labels and clipping.

## Submission review

[Preparation review](submission-review.md) records the source-level grader audit, expanded literature comparison and release limitations. [Submission metadata](submission-metadata.json) uses the author's supplied name, location and contact email; manuscript affiliation and license remain unset. The verified account uses Independent Researcher as its profile affiliation. The package arithmetic verifier checks all 37 frozen inputs and 306 usage rows. The local artifact release deliberately omits full provider traces and archive payloads, so archive equality remains an author-verified finding rather than independently reproducible from the released hashes alone.
