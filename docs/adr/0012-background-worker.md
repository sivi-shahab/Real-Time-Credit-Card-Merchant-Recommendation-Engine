# ADR-0012 — A background worker for training and the learning loops

**Status:** Accepted · 2026-09-26

## Context
Training (and Optuna tuning with it) ran in a thread of the API process, so a large
`tuneTrials` competed with serving on that replica (threat D-7). The uplift report was run
by hand. Auto-retrain and the online bandit also ran as loops inside every API replica.
A scheduler such as Airflow was considered: it would downgrade FastAPI in our environment,
add a scheduler, API server, DAG processor and metadata database to operate and secure,
and its UI would be a second place to change schedules outside the settings maker-checker
(ADR-0011), for two recurring jobs.

## Decision
1. **One worker process, `python -m rec.worker`**, from the same image (with the
   `[causal]` extra), run as the `worker` service. It runs the training queue, the
   scheduled uplift report, auto-retrain and the online bandit, and applies the approved
   learning settings like every API replica. The API keeps only the guardrail and the
   settings refresh.
2. **Training is a queue.** The API inserts a `QUEUED` row and returns; a worker claims the
   oldest with `FOR UPDATE SKIP LOCKED`. A job `RUNNING` past `TRAINING_TIMEOUT_HOURS` (6)
   lost its worker and is failed on the next turn.
3. **The uplift report runs every `UPLIFT_REPORT_INTERVAL_HOURS` (24, 0 = off)** under a
   Redis lock and is saved for the dashboard; too little data counts as `insufficient`,
   an exception as `failed` (alerted).
4. **Several workers are safe.** Jobs are claimed once; the report, auto-retrain and bandit
   passes each take a Redis lock. The worker serves its own metrics (`:9103`).

## Consequences
- Queued jobs wait while no worker runs; `TargetDown` pages for the scraped worker.
- `training.status` SSE events are emitted in the worker, not the API; the dashboard's job
  list polls, so it still updates, a few seconds later.
- The timeout is not a heartbeat: a training run longer than it would be failed wrongly.
- Dataset generation (`POST /admin/v1/datasets`) still runs in the API process.
- An organisation-wide scheduler can still call `python -m rec.ml.uplift` later; nothing
  here depends on the worker being the only caller.
