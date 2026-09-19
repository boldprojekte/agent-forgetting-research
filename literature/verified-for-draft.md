---
title: "Verified related work for the first manuscript draft"
created: 2026-09-19
updated: 2026-09-19
type: literature
tags: [research]
sources: ["../raw/literature/paper-draft-2026-09-19/metadata.json"]
---

# Verified related work for the first manuscript draft

[Project index](../index.md) · [Manuscript](../paper/manuscript.md) · [Original candidates](candidates.md)

Targeted review, not an exhaustive novelty search. Bibliographic metadata was fetched from primary records and archived with SHA-256 hashes in the linked metadata file. The BibTeX is generated from these records. arXiv records use first-submission years; newer inspected versions are identified below. Claims below are restricted to the inspected material.

| Citation key | Primary record and inspected material | Supported use in the manuscript |
|---|---|---|
| `liu_2024_lost` | [TACL publication](https://aclanthology.org/2024.tacl-1.9/), abstract and bibliographic record | Relevant-information position affects retrieval and multi-document QA in studied models; not a universal degradation threshold. |
| `packer_2023_memgpt` | [arXiv](https://arxiv.org/abs/2310.08560), [v2 sections 2.1–2.4](https://arxiv.org/html/2310.08560v2) | Memory tiers, function-mediated movement, recall storage and queue management already support agent-managed external memory. |
| `zhang_2026_memory` | [ACL Findings publication](https://aclanthology.org/2026.findings-acl.956/), [v3 section 3.1–3.3](https://arxiv.org/html/2510.12635v3) | MemAct exposes ID-based pruning and writing as policy actions and trains with DCPO. Acting-agent context control is established prior work. |
| `sun_2025_scaling` | [arXiv record](https://arxiv.org/abs/2510.11967), [author project method summary](https://context-folding.github.io/) | Context-Folding uses branch/return subtrajectories and FoldGRPO; full-text PDF sections 2.2–2.3 additionally inspected during submission preparation. |
| `yi_2026_learning` | [arXiv record](https://arxiv.org/abs/2605.30785), [v1 section 3.1–3.2](https://arxiv.org/html/2605.30785v1) | AdaCoM trains a separate manager to edit a frozen agent's context, supporting rewriting, deletion and merging. |
| `zhang_2025_agentic` | [arXiv record](https://arxiv.org/abs/2510.04618), v3 abstract | ACE maintains evolving playbooks through generation, reflection and curation. No quantitative external result is repeated in the draft. |

## Submission preparation review, 19 September 2026

Additional primary records and full texts are archived in [the preparation source manifest](../raw/literature/submission-prep-2026-09-19/metadata.json). Search queries included `AgentFold agent context management arxiv`, `PRISM context management agent forgetting arxiv`, and `FadeMem Selective Forgetting agent memory arxiv`. The exact PRISM and Selective Forgetting IDs were resolved from our original candidate register rather than confusing them with similarly named papers. Four new arXiv bibliographic records were transcribed automatically; external performance numbers are not reproduced.

| Work | Inspected source | Supported distinction |
|---|---|---|
| AgentFold, Ye et al. | [v1](https://arxiv.org/html/2510.24699v1), sections 3.1–3.4; primary metadata | Agent-generated folding directives, multi-scale summaries, latest interaction, supervised training. Another close precedent for agent-controlled curation. |
| FadeMem, Wei et al. | [v2](https://arxiv.org/html/2601.18642v2), sections 2.1–2.4 | Adaptive decay and pruning in a layered persistent-memory system, with conflict resolution and fusion. |
| PRISM, Nahid and Rafiei | [v1](https://arxiv.org/html/2510.14278v1), section 2 | Iterative Selector/Adder retrieval, not a one-shot irreversible filter. Later abstract metadata is consistent with these narrowly stated mechanisms; no current-version numerical claims. |
| Selective Forgetting, Rusu et al. | [v1](https://arxiv.org/html/2608.28978v1), section 3 | Conversation-to-graph extraction, retrieval and periodic score-based pruning. |
| pi-fold, Shane Conner | [Repository](https://github.com/shaneconner/fold), README pinned to commit `7eb2ab47e291f4616ba4850e3648724fa5600a16` | Exact stored spans, in-place briefs and expansion are shared concepts. README distinguishes older agent marking from current automatic policy. Documentation inspected; reported experiments not independently reproduced. Project website and current README describe different generations, so the README is the citation authority here. |
| Anthropic context editing | [Official docs](https://platform.claude.com/docs/en/build-with-claude/context-editing), Overview, Tool result clearing, server-side behavior and prompt caching | Configurable chronological clearing, placeholders, client history preserved, cache effects. Read through web tool; local archival request returned 403, so no local snapshot is claimed. |
| Context-Folding, Sun et al. | [PDF](https://arxiv.org/pdf/2510.11967), sections 2.2–2.3 | Branch/return context transitions and FoldGRPO verified from full text, closing the initial project-summary-only limitation. |

This remains a targeted mechanism comparison, not an exhaustive systematic review or proof of novelty. No source establishes the uniqueness of archives, recovery, stable IDs, or acting-agent curation. The contribution is framed as a concrete interface and exploratory empirical study. AgentFold has an ICLR 2026 record surfaced during search; the manuscript cites the specific arXiv version actually inspected rather than importing unverified proceedings metadata.
