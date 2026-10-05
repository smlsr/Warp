# Warp board

Updated 2026-10-03T16:43:24Z · paused=False

- Tickets: **3/366** (0.8%)
- Tokens: **0** · agent minutes: **0**
- Remaining agent-hours: **3419** · critical path left: **176h** · ETA ~**427.4h** at current cap
- Cap: 18 agents · model `claude-sonnet-5-5-high`

## Status

| status | n |
|---|---|
| queued | 363 |
| merged | 3 |

## Gates

| gate | status | members |
|---|---|---|
| G0 Skeleton | green | L-01, M-01, D-01 |
| G1 Contracts | pending | F-01, L-02, D-03, P-07, O-02, O-03, P-01, U-00, A-03, A-04 |
| G2 Ingest → ready (LOOM) | pending | L-10, L-32, L-40, L-164 |
| G3 Query plane + sets (LOOM) | pending | L-80, L-84, L-86, L-90, L-163 |
| G4 Swatch rows | pending | S-04, S-10 |
| G5 RUBRIC definitions + query API | pending | R-14, R-22, R-25 |
| G6 CORE engines + orchestrator | pending | C-46, C-49, C-51, C-68, C-69 |
| G7 Workbench UI | pending | U-01, U-03, U-09, L-110 |
| G8 APP BFF + seed | pending | A-01, A-07, A-10, A-14, A-33, A-35 |
| G9 TTEC Quality UI | pending | Q-00, Q-05, Q-12, Q-40, Q-45 |
| G10 System works (compose e2e) | pending | L-112, X-06, P-10 |
| G11 Ship (hos-dev) | pending | I-08, X-10, RU-24, CU-22 |

## Alarms

None.

## Awaiting approval (above MEDIUM)

None.

## In flight

None.

## Next ready (critical first)

- **M-02** L/HIGH REVIEW — Migration 0002+0003: items spine, facts, spans, identifiers, biz_objects, stars
- **O-02** M/MEDIUM AUTO — pkg/lib/obs OTel helper: module gate, 3-span cap, attribute budget, meters
- **O-03** S/LOW AUTO — pkg/lib/log zerolog helper with required fields and redactor
- **F-01** M/MEDIUM AUTO — pkg/lib/token: UnpackToken stub (LOOM_TOKEN_STUB=1), real link+GOD, guard
- **D-02** L/HIGH REVIEW — Dapr components: pubsub loom-bus on NATS JetStream, state store hos-revocations
- **R-01** XL/CRITICAL REVIEW — RUBRIC migrations 0022–0025: grants, rubric, ZTP, three-level assignment DDL
- **D-03** M/MEDIUM AUTO — pkg/lib/events CloudEvents codec with tenant fields and forbidden-payload guard
- **U-00** L/HIGH REVIEW — UI workspace + shell: Turborepo, Router/Query, ui preset, contracts codegen
- **A-03** M/MEDIUM AUTO — STUB registry: -1/[]/null contract, X-HOS-Stub header, WARN log, metric, report
- **A-04** L/HIGH REVIEW — Typed Dapr invoke clients for Loom, CORE and RUBRIC + recorded fakes
- **Q-02** L/HIGH REVIEW — Quality atoms: theme tokens.css, ScoreBadge, KpiTile, chips on shared primitives
- **A-05** M/MEDIUM AUTO — Scoped TTL cache and the period model (week, month, 30 days, quarter)
