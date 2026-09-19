---
title: "Fourth continuation: lower resource use, weaker method answer"
created: 2026-09-11
updated: 2026-09-11
type: evidence
tags: [research]
sources: ["browser-fourth-01/metrics.json", "browser-fourth-01/assessment.md", "../experiments/browser-fourth-01/protocol.md"]
---

# Completed fourth task

Both episodes submitted without provider or tool errors under the same corrected transport policy. Their inherited histories differ deliberately. Read-only design task; no implementation executed.

| Metric | Retained | Method |
| --- | ---: | ---: |
| Logical / physical requests | 11 / 11 | 19 / 19 |
| Wall seconds | 172.04 | 132.51 |
| First input | 267,470 | 55,206 |
| Peak input | 307,713 | 88,532 |
| Last measured input | 307,713 | 63,811 |
| Total reported tokens | 3,229,152 | 1,207,890 |
| Input / output | 3,219,215 / 9,937 | 1,195,620 / 12,270 |
| Known cost USD | 0.16945–0.42740 | 0.08194–0.14608 |
| Manual criteria | 6/12 | 4/12 |

Method used 62.59% fewer reported tokens and 79.26% less last measured input, with a shorter wall time despite more requests. Known-usage cost intervals do not overlap; no unmetered attempts occurred. These are tariff estimates with missing cache details, not invoices or proof of half the cost. Seed costs excluded. Confirmed cached inputs: retained1,476,096; method661,184. Median first substantive stream delta from physical attempt start: retained10.71s, method1.78s, no retry waiting. Not a controlled isolated cache-effect comparison.

Method made five Apply calls, archived13 new results,56 total originals intact; no recovery. All source states, genuine fourth user messages, accepted reasoning and unique tool executions verified. Last measured input precedes final answer generation, not an exact post-answer state tokenization. Retained crosses300k active input for the first time in this sequence.

[Assessment](browser-fourth-01/assessment.md): both workflows contain serious state-machine flaws. Method acknowledges DB_DONE redeliveries before the external effect has necessarily happened and confuses API-key retention with backlog age. Retained omits the terminal DONE transition and can reprocess DISPATCHED work. Quality preservation is not established in this pair; lower context alone does not guarantee a better or equally good answer. Preserve this adverse observation alongside resource savings. No statistical degradation threshold or causal attribution to forgetting follows from one different-history pair.

[Start](../index.md) · [Protocol](../experiments/browser-fourth-01/protocol.md) · [Prior comparison](browser-third-completed-02.md)
