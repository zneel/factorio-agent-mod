# Agentic workflow: how the crew plays toward the rocket

One loop, four roles, one shared state. The goal and its phases are in `ROADMAP.md`.

## Roles

| Role | Who | Owns | Reads | Writes |
|---|---|---|---|---|
| **Supervisor / architect** | the main Claude session | roadmap, hard layout designs, restarts, game speed | `crew.py roadmap/designs/offline`, `plant_status` | `ROADMAP.md`, jobs (design results), restarts agents |
| **Planner** | one Haiku agent, 8 `watch` cycles per run | the job board, the research queue | `crew.py watch`, `roadmap`, `notes` | `post_job`, `update_job`, `start_research`, `crew.py design` |
| **Workers** x4 | one Haiku agent per character: agent, hauler, runner1, runner2 | executing jobs | `crew.py next` (job + teammates + messages) | game actions, `finish_job`, `post_job` (delegation), `crew.py note` |
| **Game** | the Factorio server and bridge | truth | | |

Everything is stateless between agent runs except the shared state: the **job board**, **chat**,
`runtime/plant-notes.md` (facts about the base), `runtime/design-queue.json`, and the game itself.

## Job lifecycle

1. **Plan.** The planner measures (`watch`, `roadmap`, `production`), picks the biggest gap of the current
   phase, and posts a job (or a PAIR with `PARTNER: <name> #<id>`) with exact tiles, a machine source
   for materials and an accept check. No hand-feeding jobs. Hard layouts go to the architect:
   `crew.py design "<title>" "<evidence: view_map tiles, what blocks>"`; the supervisor answers with
   concrete jobs and closes the request.
2. **Claim.** A worker runs `crew.py next <name>`. It gets the job, what each teammate is doing and
   its unread messages. Each worker handles 3 jobs, then `crew.py` says STOP and the supervisor starts a
   fresh one (small contexts).
3. **Do.** Build exactly what the brief says. Delegate what a teammate does better
   (`post_job for=<name>`); the result comes back as chat. Record facts others will need:
   `crew.py note "<fact with positions>"`.
4. **Verify and close.** Inspect before `finish_job` (accept check from the brief). Blocked only if a
   placement truly fails or the accept check fails, with the reason.
5. **Review.** The planner reads finished/blocked notes, compares KPIs before/after, cancels stale
   jobs and posts the next. A blocked job is never re-posted unchanged: decide, or send a design request.

## Control rules

- The board must always hold a job each character can take. An idle character is waste.
- Every job serves a ROADMAP phase gate. Symptom tickets (a full machine) are cancelled; fix the root.
- Hand-feeding is forbidden; build the automatic feed (GAME.md "Design basics").
- Escalation ladder: worker fixes it -> worker delegates to a teammate -> planner decides -> architect
  designs (`crew.py design`).
- Supervisor on every wake-up: `crew.py offline` (restart those workers/planner), `crew.py designs`
  (answer requests), `crew.py roadmap` (is the phase gate moving?).

## Health signals (all on the stream board)

crew status per character (on job / idle / worker OFFLINE), flow %, science per minute, roadmap phase,
job counts. A worker whose heartbeat is older than 4 minutes (10 while it holds a job) is OFFLINE.
