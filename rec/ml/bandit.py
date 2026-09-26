"""Continuous learning, stage 2 — an online contextual bandit that never serves (ADR-0007).

One `river` Bayesian linear regression over the serving feature vector scores every
candidate by an upper confidence bound (mean + exploration * stdev), so what it learns
about one merchant carries to similar ones. It runs beside every live request like a
shadow model: the order it would have shown is compared with the served one and recorded
in `shadow_evaluations` as model version `online-ucb`.

It learns from the impressions customers actually saw. The served items' vectors are kept
in Redis under the response requestId (the id clients send impressions with); once an
impression's observation window closes, its label is learned exactly once.

State is JSON, never pickle: Redis runs without AUTH in this deployment, and unpickling
from it would hand code execution to anyone who can write a key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
from river.linear_model import BayesianLinearRegression

from rec.ml import registry
from rec.ml.attribution import ATTRIBUTION_WINDOW, OBSERVATION_WINDOW, label_from_events
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("bandit")
VERSION = registry.BANDIT_VERSION
MODEL_KEY = "bandit:model"
WATERMARK_KEY = "bandit:watermark"
LOCK_KEY = "bandit:lock"
CTX_PREFIX = "bandit:ctx:"
CTX_TTL_SECONDS = int((OBSERVATION_WINDOW + ATTRIBUTION_WINDOW).total_seconds())
# The state below is river's private layout; pinned in pyproject, round-trip tested.
_STATE = ("_idx", "_ss_arr", "_ss_inv_arr", "_eta_arr", "_cap", "_n")


def new_model() -> BayesianLinearRegression:
    return BayesianLinearRegression(alpha=1.0, beta=1.0)


def dump(model: BayesianLinearRegression) -> str:
    return json.dumps({k: (v.tolist() if isinstance(v, np.ndarray) else v)
                       for k, v in ((k, getattr(model, k)) for k in _STATE)})


def load(text: str) -> BayesianLinearRegression:
    model, state = new_model(), json.loads(text)
    model._idx, model._cap, model._n = state["_idx"], state["_cap"], state["_n"]
    model._ss_arr = np.array(state["_ss_arr"], dtype=np.float64).reshape(model._cap, model._cap)
    model._ss_inv_arr = np.asfortranarray(
        np.array(state["_ss_inv_arr"], dtype=np.float64).reshape(model._cap, model._cap))
    model._eta_arr = np.array(state["_eta_arr"], dtype=np.float64)
    model._m_dirty = True  # posterior mean is recomputed from the natural parameters
    return model


def ucb(model: BayesianLinearRegression, vectors: list[dict[str, float]],
        exploration: float) -> list[float]:
    scores = []
    for x in vectors:
        dist = model.predict_one(x, with_dist=True)
        scores.append(dist.mu + exploration * dist.sigma)
    return scores


# ------------------------------------------------------------------- per-process cache

_cached: tuple[float, BayesianLinearRegression] | None = None


async def current(r) -> BayesianLinearRegression:
    """The shared model, re-read from Redis at most every learn interval."""
    global _cached
    now = time.monotonic()
    if _cached is None or now - _cached[0] > settings.online_bandit_learn_interval_seconds:
        text = await r.get(MODEL_KEY)
        _cached = (now, load(text) if text else new_model())
    return _cached[1]


# ---------------------------------------------------------------------- serve + learn


async def shadow(store, request_id: str, customer_id: str, served_order: list[str],
                 candidate_ids: list[str],
                 make_vectors: Callable[[], list[dict[str, float]]]) -> None:
    """Fire-and-forget beside a live request. Must never affect the response."""
    try:
        vectors = make_vectors()
        by_id = dict(zip(candidate_ids, vectors, strict=True))
        served = {m: json.dumps(by_id[m]) for m in served_order if m in by_id}
        if served:
            key = CTX_PREFIX + request_id
            await store.r.hset(key, mapping=served)
            await store.r.expire(key, CTX_TTL_SECONDS)
        t0 = time.perf_counter()
        scores = ucb(await current(store.r), vectors, settings.online_bandit_exploration)
        ranked = [m for _, m in sorted(zip(scores, candidate_ids, strict=True),
                                       key=lambda p: (-p[0], p[1]))][:len(served_order)]
        await registry.record_shadow(request_id, customer_id, VERSION, "LIVE",
                                     registry.rank_agreement(served_order, ranked),
                            bool(served_order and ranked and ranked[0] == served_order[0]),
                            (time.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001 - the bandit is informational only
        log.info("bandit shadow skipped: %s", exc)


async def learn(store, *, now: datetime | None = None) -> int:
    """Learn every impression whose observation window closed since the last pass.

    ponytail: labels are taken when the observation window closes, so a conversion that
    arrives later inside the attribution window is missed here; the batch retrain
    (stage 1) relabels on every export and does count it.
    """
    global _cached
    now = now or datetime.now(UTC)
    if not await store.r.set(LOCK_KEY, "1", nx=True,
                             ex=settings.online_bandit_learn_interval_seconds):
        return 0
    try:
        until = now - OBSERVATION_WINDOW
        mark = await store.r.get(WATERMARK_KEY)
        since = datetime.fromisoformat(mark) if mark else until - ATTRIBUTION_WINDOW
        conn = await pg.pool()
        impressions = await conn.fetch(
            """SELECT * FROM impressions WHERE occurred_at > $1 AND occurred_at <= $2""",
            since, until)
        interactions = await conn.fetch(
            """SELECT i.* FROM interactions i JOIN impressions m USING (impression_id)
               WHERE m.occurred_at > $1 AND m.occurred_at <= $2""", since, until)
        events = [{"requestId": r["request_id"], "impressionId": r["impression_id"],
                   "customerId": r["customer_id"], "merchantId": r["merchant_id"],
                   "position": r["position"], "eventType": "IMPRESSION",
                   "occurredAt": r["occurred_at"]} for r in impressions]
        events += [{"impressionId": r["impression_id"], "eventType": r["interaction_type"],
                    "occurredAt": r["occurred_at"]} for r in interactions]
        labels = label_from_events(events, as_of=now)

        # Fresh from Redis, not the serving cache: another replica may have learned last.
        text = await store.r.get(MODEL_KEY)
        model, learned = (load(text) if text else new_model()), 0
        for r in impressions:
            vector = await store.r.hget(CTX_PREFIX + r["request_id"], r["merchant_id"])
            if vector is None or r["impression_id"] not in labels:
                continue  # served before the bandit ran, or its context expired
            model.learn_one(json.loads(vector), labels[r["impression_id"]])
            learned += 1
        if learned:
            await store.r.set(MODEL_KEY, dump(model))
            _cached = (time.monotonic(), model)
        await store.r.set(WATERMARK_KEY, until.isoformat())
        return learned
    finally:
        await store.r.delete(LOCK_KEY)


async def loop(store) -> None:
    while True:
        await asyncio.sleep(settings.online_bandit_learn_interval_seconds)
        try:
            learned = await learn(store)
            if learned:
                log.info("bandit learned %d impressions", learned)
        except Exception:  # noqa: BLE001 - a failed pass must not kill the API
            log.exception("bandit learning pass failed")
