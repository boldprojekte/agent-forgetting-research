---
title: "Fourth-task method repeat: quality variation with lower resource use"
created: 2026-09-11
updated: 2026-09-11
type: evidence
tags: [research]
sources: ["browser-fourth-repeat-01/protocol.md", "browser-fourth-repeat-01/assessment.md", "browser-fourth-repeat-01/metrics.json", "browser-fourth-01.md"]
---

# Completed exploratory repeat

| Metric | Retained | Method original | Method repeat |
| --- | ---: | ---: | ---: |
| Manual criteria | 6/12 | 4/12 | 6/12 |
| Last measured input | 307,713 | 63,811 | 65,781 |
| Peak input | 307,713 | 88,532 | 71,365 |
| Total reported tokens | 3,229,152 | 1,207,890 | 999,762 |
| Requests / physical attempts | 11 / 11 | 19 / 19 | 16 / 16 |
| Wall seconds | 172.04 | 132.51 | 128.65 |
| Known cost USD | 0.16945–0.42740 | 0.08194–0.14608 | 0.06858–0.11299 |

Repeat submitted without provider/tool errors, unmetered attempts or retry waiting. Input985,258/output14,504; confirmed cached input608,768. Five Applies archive five more originals,48 total verified; no recovery. First input55,206, with the exact same first request as the original method run. Own C-seed, four real user turns, accepted reasoning and unique tool executions verified by analyze.py. Last input precedes final answer generation.

Repeat used69.04% fewer reported total tokens than retained. Median physical-attempt time to first substantive stream delta1.70s (original method1.78s, retained10.71s), not isolated cache causality. These are phase-only costs, excluding historical construction and other attempts; repeating adds cost and the original method sample must remain counted. Both method D runs together cost USD0.15053–0.25907 known usage.

[Assessment](browser-fourth-repeat-01/assessment.md): the repeat separates external dispatch and uses a first-attempt retention clock, improving on the first method answer. However, it fails to connect PROCESSED to DISPATCH_PENDING and lacks ordering/recovery details. It matches retained's coarse score, not proven equal correctness or noninferiority. One repeat after observing a failure supports within-condition variation, not a conclusion that the earlier outcome was merely chance. No tuning or corrective hints were supplied.

No additional episodes started. Monitoring paused after assessment; original samples preserved.

[Start](../index.md) · [Original fourth task](browser-fourth-01.md)
