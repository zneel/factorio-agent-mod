# Steering plan: how the overseer controls the crew

## What goes wrong today (from the worker reports)

1. **Blind planning.** The planner didn't know what we own, so briefs named empty chests, wrong items
   or wrong positions, and the steel stock (1,600) sat unused next to a starved engine assembler.
2. **Jobs that change nothing.** Many jobs were surveys or fault tickets (andon) that ended "no fix",
   or were closed "done" without a real check (placeholder notes, failed accept checks marked done).
3. **Steering only at job boundaries.** The overseer could change the board, but not a worker that was
   mid-job on the wrong thing.
4. **Layout decisions in the wrong place.** Haiku workers and the Haiku planner can't design routes, so
   layout jobs bounced back as "blocked: needs a decision".
5. **No measurement of the crew itself.** Nobody knew which kind of job works and which wastes time.

## The five levers

| # | Lever | What | Status |
|---|---|---|---|
| 1 | **See** | `crew.py audit`: stock and where it sits, ore rate and hours left, power load, every recipe's capacity vs output with BOTTLENECK / IDLE flags. `crew.py where <item>` names the chests. | **done** (agent-bridge 0.1.18) |
| 2 | **Talk** | `crew.py order <char> "..."`: printed on top of every command that worker runs until it acks (`crew.py ack`). Redirects a worker mid-job. `crew.py orders` shows open/acked orders. | **done** |
| 3 | **Specify** | Machine-checkable jobs: the brief carries `ACCEPT:` clauses (`status x,y = working`, `chest x,y has item >= n`, `belt x,y carries item`), checked when the job is posted (tiles placeable, materials exist) and again at `finish_job`. A job can't close "done" while its checks fail; it flips to blocked with the measured values. | next |
| 4 | **Decide where the skill is** | Workers only build. Surveys are done by the planner with read-only tools (audit, view_map, inspect, belt_status). Auto-andon jobs stop. Layout questions go to the architect's design queue and come back as exact jobs in batches. | next |
| 5 | **Measure the crew** | `crew.py scorecard`: per worker and per job type, the share of jobs that changed the factory (production or capacity moved), blocked, or no-op; time per job. Target: 70%+ of jobs change the factory. Shown on the stream board. | next |

## Overseer routine (every wake-up)

1. `crew.py offline`: restart any worker or the planner that stopped.
2. `crew.py audit`: the BOTTLENECK lines are the next targets; IDLE-blocked lines say which consumer is missing.
3. `crew.py designs`: answer design requests with exact jobs (tiles verified with `view_map`).
4. `crew.py orders` / the board: redirect a worker with `crew.py order` when it works on something stale.
5. `crew.py roadmap`: is the phase gate moving? If not, change the plan, not the workers.

## Order of work for levers 3-5

1. `can_place` bridge command (is this item placeable at x,y,dir?) and the ACCEPT evaluator in crew.py;
   `finish_job` wrapper that runs the checks.
2. Turn off auto-andon jobs; the planner's prompt: no surveys for workers.
3. Outcome log: each finished job records before/after audit numbers for its area; `crew.py scorecard`.
