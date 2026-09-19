---
title: "Prepublication review and remaining decisions"
created: 2026-09-19
updated: 2026-09-19
type: evidence
tags: [research]
sources: ["manuscript.md", "claim-evidence.md", "../literature/verified-for-draft.md"]
---

# Prepublication review

Author supplied by the user: **Jan-Peter Franke, Münster, Germany**. Contact supplied by the author: **franke@bold-projekte.com**. The account `janpeterfranke` was created with author authorization and its email verified on 19 September 2026. Its profile uses Independent Researcher, Germany, career status Other and default category cs.AI. Password-manager storage is pending the author unlocking Dashlane. No university, employer, degree, ORCID or endorsement is invented.

## Scientific review completed

- Related work expanded from six to twelve references: AgentFold, PRISM, FadeMem, graph-based Selective Forgetting, pi-fold and Anthropic context editing added. Four new arXiv records were archived and used to generate metadata. The exact pi-fold README commit is pinned. The close overlap is explicit; there is no first-invention or unique-recovery claim.
- Context-Folding branch/return and FoldGRPO are now checked against full-text sections 2.2–2.3, not just the author project page.
- Main quantitative claims recomputed from the original per-response summaries. All 37 frozen experiment-input hashes still match. All 306 figure rows match reported input usage. The follow-up summary is cumulative and is not added to the primary summary.
- Follow-up grader inspected against both submitted candidates. Method uses `server_ptr->Shutdown`, missing the grader's literal `server->Shutdown` matcher. Retained uses `signalfd`, missing the literal `sigwait` matcher. Both contain remaining-budget calculations. Static failures must not be treated as proof that these mechanisms are absent.
- Method's startup-failure branch calls `signal_thread.join()` without stopping the signal-waiting thread, consistent with the measured timeout. This is a source-level interpretation, not a new execution result. Runtime signal exits and startup timeout remain the primary follow-up evidence; original grades are unchanged.
- Negative Planroom and browser results, all six Sphinx sessions, differing observations, late primary context growth, absent main-case recovery and image confounding remain disclosed.
- Original raw runs, submitted candidates, frozen graders and benchmark inputs were not edited. No paid inference or new benchmark run was performed for preparation.

## Limits of the release candidate

The companion package permits checking arithmetic and inspecting the main-case implementation and submitted final workspaces. It does not publish complete model transcripts, archive payloads, browser recordings, the private Arios repository or the complete earlier experiment history. Numerical summaries for supporting experiments are included; those are not a full independent reproduction of every quality judgment. Archive notes and hashes support inspection but cannot externally prove equality to withheld originals. These distinctions must remain clear in any announcement.

A fresh machine setup for the live Docker/browser experiment has not been rerun. Frozen code and lockfiles preserve the setup, but provider access, model availability, container images, Linux sandbox support and system resources remain external requirements. The bundled offline checks and arithmetic verifier are the validated reproducibility surface.

## Editorial review revision

The author-approved revision consolidates repeated caveats into Section 7, rounds descriptive percentage changes to whole percentages (the sub-percent Planroom increase is described as less than 1%), keeps exact token counts, reduces the desk analogy to two sentences, and replaces the provenance table with a one-sentence Arios origin statement. Full commit evidence remains in the companion record. Discussion now explains the retained-history baseline and identifies a same-model comparison against automatic context editing as the next study. No baseline result was invented, no original score changed, and the adverse browser result and selected-repeat disclosure remain.

## Packaging checks

The revised 12-page PDF was visually reviewed, including figures, tables and references. The LaTeX log has no reported warnings or overflow. The source ZIP compiles independently with Tectonic 0.17.0 (XeLaTeX); its extracted PDF text matches the reviewed build. All 28 focused tests pass using the staged code. Arithmetic checks cover 37 frozen input files and 306 usage rows. The release candidate was scanned for exact local credential values and private-key headers, with no matches; this is a scoped check, not a universal secret-detection guarantee.

## Decisions needed before uploading

1. Author review of the actual PDF, contribution framing, results and limitations.
2. arXiv account created and email verified; endorsement requirements have not yet been checked. Contact email is confirmed. Suggested category: `cs.AI`, subject to arXiv moderation.
3. Paper distribution license. A proposed conservative option is arXiv's perpetual non-exclusive distribution license; no selection has been submitted.
4. Permission and license for the separate code/data release. Proposed author-code license: MIT. No license grant has been applied on the author's behalf. Existing third-party notices must remain.
5. Public destination for the artifact package, then add its durable URL to the paper before submission. Until then the paper accurately says the package is prepared locally.

The paper is an original exploratory systems report, not a literature survey or position-only article. Additional balanced replications and stronger semantic grading would improve the evidence, but no invented numerical sample requirement is imposed as a prerequisite to a narrowly framed preprint. Acceptance by arXiv is not guaranteed.

## Current official guidance checked

- [Submission guidelines](https://info.arxiv.org/help/submit/index.html): author self-submission, source upload for LaTeX papers, preview and metadata checks.
- [Endorsement](https://info.arxiv.org/help/endorsement.html): initial-category endorsement and contacting an eligible endorser, not mass outreach.
- [Moderation and AI disclosure](https://info.arxiv.org/help/moderation/index.html): scientific content and author responsibility; significant AI assistance disclosed, AI not an author.
- [Licenses](https://info.arxiv.org/help/license/index.html): distribution permission must be selected deliberately.
- [CS content-type policy](https://blog.arxiv.org/2025/10/31/attention-authors-updated-practice-for-review-articles-and-position-papers-in-arxiv-cs-category/): separate rules for surveys and position papers; do not mislabel this empirical report.

[Manuscript](manuscript.md) · [Status](../status.md)
