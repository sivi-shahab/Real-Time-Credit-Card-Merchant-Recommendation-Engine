"""Recommendation API + admin BFF. OpenAPI 3.1, consistent errors, RBAC on the backend."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel, Field

from rec.api import bff, jobs, learning_settings, ml_jobs, service
from rec.api.auth import Principal, customer_self, principal, rate_limited, require
from rec.core.models import (
    FEATURE_SCHEMA_VERSION,
    RANKING_CONFIG_VERSION,
    RecommendationResponse,
)
from rec.core.ranking import DEFAULT_RESULTS, MAX_RESULTS
from rec.generator.config import DatasetConfig
from rec.ml import bandit, guardrail, registry, uplift
from rec.ml import client as ranking_client
from rec.ml.vectorize import FEATURE_SCHEMA_VERSION as VECTOR_SCHEMA_VERSION
from rec.obs import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    LEARNING_SWITCH,
    metrics_body,
    setup_logging,
    setup_tracing,
    trace_id_var,
)
from rec.settings import settings
from rec.simulator.runner import InvalidTransition, manager
from rec.store import pg
from rec.store.redis_store import MODEL_FIELD, OnlineStore

store = OnlineStore()
log = logging.getLogger("api")


async def record_learning_config() -> dict | None:
    """T-10: export the switches in force as gauges (a change alerts), and audit when a
    replica starts with values unlike the last audited ones: env drift before the first
    approved change (ADR-0011), since approved changes are audited as they are made.
    Returns the new values when it audited a change."""
    # ponytail: replicas starting together may both audit the same change; dedupe with an
    # advisory lock if the duplicate rows matter.
    current = learning_settings.effective()
    for name, value in current.items():
        LEARNING_SWITCH.labels(name).set(float(value))
    conn = await pg.pool()
    last = await conn.fetchval(
        """SELECT changes FROM audit_events WHERE action = 'config.learning'
           ORDER BY id DESC LIMIT 1""")
    previous = json.loads(last)["after"] if last else None
    if previous == current:
        return None
    await pg.audit("system:config", "System", "config.learning", "settings/learning",
                   outcome="AUTOMATIC", changes={"before": previous, "after": current})
    return current


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await pg.pool()
    await learning_settings.refresh(force=True)  # approved values win over env (ADR-0011)
    await record_learning_config()
    # Serving-side only: the learning loops and training run in the worker (ADR-0012).
    watchers = [asyncio.create_task(task) for task in (
        guardrail.loop(store), learning_settings.loop())]
    yield
    for watcher in watchers:
        watcher.cancel()
    await ranking_client.close()
    await store.close()
    await pg.close()


app = FastAPI(
    title="Credit Card Merchant Recommendation Engine",
    version="1.0.0",
    openapi_version="3.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(bff.router)


@app.middleware("http")
async def correlation(request: Request, call_next):
    # With tracing on, the traceId in logs, errors and audit rows is the OpenTelemetry
    # trace id, so any of them opens the whole request in the tracing UI.
    span = trace.get_current_span()
    context = span.get_span_context()
    trace_id = request.headers.get("x-correlation-id") or (
        format(context.trace_id, "032x") if context.is_valid else str(uuid.uuid4()))
    if context.is_valid and trace_id != format(context.trace_id, "032x"):
        span.set_attribute("correlation.id", trace_id)
    request.state.trace_id = trace_id
    trace_id_var.set(trace_id)
    t0 = time.perf_counter()
    response = await call_next(request)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    HTTP_LATENCY.labels(route).observe(time.perf_counter() - t0)
    response.headers["x-correlation-id"] = trace_id
    return response


setup_tracing("rec-api", app)  # after the middleware above, so its span encloses them


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    """§12.1 — code, message, fieldErrors, traceId."""
    return Response(
        content=json.dumps({
            "code": _code(exc.status_code),
            "message": exc.detail,
            "fieldErrors": [],
            "traceId": getattr(request.state, "trace_id", None),
        }),
        status_code=exc.status_code,
        media_type="application/json",
        headers=exc.headers or {},
    )


def _code(status_code: int) -> str:
    return {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
            409: "CONFLICT", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED"}.get(
        status_code, "ERROR")


# ===================================================================== customer API


@app.get("/api/v1/customer/{customer_id}/recommendations", tags=["customer"],
         response_model=RecommendationResponse,
         dependencies=[Depends(rate_limited("recommendations"))])
async def get_recommendations(
    customer_id: str,
    p: Annotated[Principal, Depends(customer_self)],
    cityCode: str | None = None,
    channel: str | None = Query(None, pattern="^(ONLINE|OFFLINE)$"),
    limit: int = Query(DEFAULT_RESULTS, ge=1, le=MAX_RESULTS),
    refresh: bool = False,
):
    try:
        response, _ = await service.recommend_safe(
            store, customer_id, city_code=cityCode, channel=channel, limit=limit,
            use_cache=not refresh,
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"customer {customer_id} not found")
    try:  # S-5: feedback is later checked against exactly this slate
        await store.record_served(customer_id, response.requestId, response.modelVersion,
                                  [item.merchantId for item in response.recommendations])
    except Exception as exc:  # noqa: BLE001 - serving never fails for it; feedback does
        log.warning("served slate not recorded, feedback for it will be refused: %s", exc)
    # pydantic-core serialises directly; FastAPI's jsonable_encoder walk cost ~2 ms here
    return Response(response.model_dump_json(), media_type="application/json")


class ImpressionItem(BaseModel):
    impressionId: str | None = None
    merchantId: str
    position: int = Field(ge=0, description="0-based place in the response (rank - 1)")


class ImpressionBatch(BaseModel):
    """What the app displayed from one recommendation response."""
    requestId: str
    customerId: str
    # Ignored: the served model is taken from the response record, not from the client.
    modelVersion: str | None = None
    items: list[ImpressionItem] = Field(default_factory=list, max_length=MAX_RESULTS)


def _own_feedback(p: Principal, customer_id: str) -> None:
    """Feedback trains models (ADR-0007), so only the customer it describes may send it.
    No staff role has a reason to, and letting one would open a poisoning path (T-7)."""
    if p.kind != "customer" or p.subject != customer_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "feedback is accepted only from the customer it describes")


@app.post("/api/v1/feedback/impressions", status_code=202, tags=["feedback"],
          dependencies=[Depends(rate_limited("feedback"))])
async def record_impressions(batch: ImpressionBatch,
                             p: Annotated[Principal, Depends(principal)]):
    _own_feedback(p, batch.customerId)
    try:
        served = await store.served(batch.customerId, batch.requestId)
    except Exception:  # noqa: BLE001 - cannot verify, so do not accept (fail closed)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "impressions cannot be verified right now; retry later")
    if not served:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "this request was not served to this customer, or has expired")
    for item in batch.items:
        if served.get(item.merchantId) != str(item.position):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"merchant {item.merchantId} was not served at position "
                                f"{item.position} in this response")
    conn = await pg.pool()
    await conn.executemany(
        """INSERT INTO impressions (impression_id, request_id, customer_id, merchant_id,
             position, model_version, occurred_at) VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT (impression_id) DO NOTHING""",
        [(i.impressionId or str(uuid.uuid4()), batch.requestId, batch.customerId,
          i.merchantId, i.position, served[MODEL_FIELD],
          datetime.now(UTC)) for i in batch.items])
    await store.incr_metric("impressions", len(batch.items))
    return {"accepted": len(batch.items)}


class Interaction(BaseModel):
    impressionId: str
    customerId: str
    merchantId: str
    interactionType: str = Field(pattern="^(CLICK|PROMO_ACTIVATION|REDEMPTION)$")


@app.post("/api/v1/feedback/interactions", status_code=202, tags=["feedback"],
          dependencies=[Depends(rate_limited("feedback"))])
async def record_interaction(body: Interaction, p: Annotated[Principal, Depends(principal)]):
    _own_feedback(p, body.customerId)
    conn = await pg.pool()
    shown = await conn.fetchrow(
        "SELECT customer_id, merchant_id FROM impressions WHERE impression_id = $1",
        body.impressionId)
    if shown is None or (shown["customer_id"], shown["merchant_id"]) != (
            body.customerId, body.merchantId):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "no such impression of this merchant for this customer")
    await conn.execute(
        """INSERT INTO interactions (interaction_id, impression_id, customer_id, merchant_id,
             interaction_type, occurred_at) VALUES ($1,$2,$3,$4,$5,$6)""",
        str(uuid.uuid4()), body.impressionId, body.customerId, body.merchantId,
        body.interactionType, datetime.now(UTC))
    await store.incr_metric(f"interaction_{body.interactionType}")
    return {"accepted": 1}


# ===================================================================== datasets


@app.post("/admin/v1/datasets", status_code=202, tags=["datasets"])
async def create_dataset(cfg: DatasetConfig, request: Request,
                         p: Annotated[Principal, Depends(require("dataset:create"))]):
    dataset_id = await jobs.create_dataset_job(
        cfg, request.headers.get("idempotency-key"))
    await pg.audit(p.subject, p.role, "dataset.create", f"dataset/{dataset_id}",
                   changes=json.loads(cfg.model_dump_json()),
                   trace_id=request.state.trace_id)
    return {"datasetId": dataset_id, "status": "QUEUED"}


@app.get("/admin/v1/datasets", tags=["datasets"])
async def list_datasets(p: Annotated[Principal, Depends(require("dataset:read"))],
                        limit: int = Query(50, le=200)):
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT dataset_id, status, created_at, updated_at, error,
                  manifest->'rowCounts' AS row_counts
           FROM dataset_jobs ORDER BY created_at DESC LIMIT $1""", limit)
    return [dict(r) | {"row_counts": json.loads(r["row_counts"]) if r["row_counts"] else None}
            for r in rows]


@app.get("/admin/v1/datasets/{dataset_id}", tags=["datasets"])
async def get_dataset(dataset_id: str,
                      p: Annotated[Principal, Depends(require("dataset:read"))]):
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM dataset_jobs WHERE dataset_id=$1", dataset_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dataset not found")
    out = dict(row)
    out["config"] = json.loads(out["config"])
    out["manifest"] = json.loads(out["manifest"]) if out["manifest"] else None
    return out


class SizeEstimate(BaseModel):
    estimatedTransactionRows: int
    estimatedBytes: int
    estimatedSeconds: float


@app.post("/admin/v1/datasets/estimate", tags=["datasets"])
async def estimate_dataset(cfg: DatasetConfig,
                           p: Annotated[Principal, Depends(require("dataset:read"))]):
    """UI-002 — show a size estimate before the operator submits."""
    rows = int(cfg.transactionCount * (1 + cfg.refundRate + cfg.reversalRate))
    return SizeEstimate(
        estimatedTransactionRows=rows,
        estimatedBytes=rows * 420 + cfg.customerCount * 220 + cfg.merchantCount * 180,
        estimatedSeconds=round(rows / 12000 + cfg.customerCount / 50000, 2),
    )


# ===================================================================== simulations


class SimulationRequest(BaseModel):
    datasetId: str
    targetTps: int = Field(200, ge=1)
    speedMultiplier: float = Field(1.0, gt=0, le=100)


@app.post("/admin/v1/simulations", status_code=202, tags=["simulations"])
async def create_simulation(body: SimulationRequest, request: Request,
                            p: Annotated[Principal, Depends(require("simulation:control"))]):
    try:
        run = await manager.create(body.datasetId, body.targetTps, body.speedMultiplier,
                                   request.headers.get("idempotency-key"))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    await pg.audit(p.subject, p.role, "simulation.create", f"simulation/{run['run_id']}",
                   changes=body.model_dump(), trace_id=request.state.trace_id)
    return run


@app.get("/admin/v1/simulations", tags=["simulations"])
async def list_simulations(p: Annotated[Principal, Depends(require("simulation:control"))]):
    return await manager.list()


@app.get("/admin/v1/simulations/{run_id}", tags=["simulations"])
async def get_simulation(run_id: str,
                         p: Annotated[Principal, Depends(require("simulation:control"))]):
    try:
        return await manager.get(run_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")


@app.post("/admin/v1/simulations/{run_id}/{command}", tags=["simulations"])
async def control_simulation(run_id: str, command: str, request: Request,
                             p: Annotated[Principal, Depends(require("simulation:control"))]):
    if command not in ("start", "pause", "resume", "stop"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown command")
    try:
        run = await manager.command(run_id, command)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await pg.audit(p.subject, p.role, f"simulation.{command}", f"simulation/{run_id}",
                   trace_id=request.state.trace_id)
    return run


# ===================================================================== explorer


@app.get("/admin/v1/transactions", tags=["explorer"])
async def list_transactions(
    p: Annotated[Principal, Depends(require("transaction:read"))],
    customerId: str | None = None,
    merchantId: str | None = None,
    outcome: str | None = None,
    transactionId: str | None = None,
    occurredFrom: datetime | None = None,
    occurredTo: datetime | None = None,
    cursor: int | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT event_id, transaction_id, customer_id, merchant_id, txn_type, amount_minor,
                  occurred_at, received_at, outcome, reject_code, reject_detail, correlation_id,
                  extract(epoch from received_at)*1000 AS cursor_key
           FROM transaction_log
           WHERE ($1::text IS NULL OR customer_id=$1)
             AND ($2::text IS NULL OR merchant_id=$2)
             AND ($3::text IS NULL OR outcome=$3)
             AND ($4::text IS NULL OR transaction_id=$4)
             AND ($5::timestamptz IS NULL OR occurred_at >= $5)
             AND ($6::timestamptz IS NULL OR occurred_at <= $6)
             AND ($7::bigint IS NULL OR extract(epoch from received_at)*1000 < $7)
           ORDER BY received_at DESC LIMIT $8""",
        customerId, merchantId, outcome, transactionId, occurredFrom, occurredTo, cursor, limit)
    items = [dict(r) for r in rows]
    return {"items": items,
            "nextCursor": int(items[-1]["cursor_key"]) if len(items) == limit else None}


@app.get("/admin/v1/transactions/{event_id}", tags=["explorer"])
async def get_transaction(event_id: str,
                          p: Annotated[Principal, Depends(require("transaction:read"))]):
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM transaction_log WHERE event_id=$1", event_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    out = dict(row)
    out["envelope"] = _redact(json.loads(out["envelope"]))
    return out


def _redact(envelope: dict) -> dict:
    """UI-004 — the raw envelope is shown with sensitive fields removed."""
    payload = dict(envelope.get("payload", {}))
    for field in ("pan", "cvv", "cardNumber", "customerName", "phone", "email"):
        payload.pop(field, None)
    return envelope | {"payload": payload}


@app.get("/admin/v1/customers/{customer_id}/features", tags=["explorer"])
async def get_customer_features(customer_id: str, request: Request,
                                p: Annotated[Principal, Depends(customer_self)],
                                asOf: datetime | None = None):
    row = await pg.customer(customer_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found")
    merchant_city = await pg.merchant_city_map()
    features = await store.features(customer_id, asOf or datetime.now(UTC),
                                    merchant_city)
    await pg.audit(p.subject, p.role, "customer.features.read", f"customer/{customer_id}",
                   trace_id=request.state.trace_id)  # UI-005: profile access is audited
    return features | {"cityCode": row["city_code"], "cardTier": row["card_tier"],
                       "personalizationAllowed": row["personalization_allowed"]}


@app.get("/admin/v1/customers", tags=["explorer"])
async def list_customers(p: Annotated[Principal, Depends(require("customer:read"))],
                         search: str | None = None, limit: int = Query(25, le=100)):
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT c.customer_id, c.city_code, c.card_tier, c.segment,
                  c.personalization_allowed,
                  (SELECT count(*) FROM transaction_log t
                    WHERE t.customer_id=c.customer_id AND t.outcome='APPLIED') AS event_count
           FROM customers c
           WHERE ($1::text IS NULL OR c.customer_id ILIKE '%'||$1||'%')
           ORDER BY event_count DESC, c.customer_id LIMIT $2""", search, limit)
    return [dict(r) for r in rows]


class PreviewRequest(BaseModel):
    customerId: str
    cityCode: str | None = None
    channel: str | None = None
    limit: int = Field(DEFAULT_RESULTS, ge=1, le=MAX_RESULTS)


@app.post("/admin/v1/recommendations/preview", tags=["explorer"])
async def preview(body: PreviewRequest, request: Request,
                  p: Annotated[Principal, Depends(require("recommendation:preview"))]):
    """AC-007 — preview writes no impression and never touches the production cache."""
    try:
        response, debug = await service.recommend(
            store, body.customerId, city_code=body.cityCode, channel=body.channel,
            limit=body.limit, use_cache=False, preview=True, explain=True)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found")
    await pg.audit(p.subject, p.role, "recommendation.preview", f"customer/{body.customerId}",
                   trace_id=request.state.trace_id)
    return {"response": response, "debug": debug, "preview": True}


# ===================================================================== master data


@app.get("/admin/v1/merchants", tags=["catalog"])
async def list_merchants(p: Annotated[Principal, Depends(require("merchant:read"))],
                         cityCode: str | None = None, search: str | None = None,
                         limit: int = Query(50, le=500), offset: int = 0):
    return await pg.merchants(cityCode, search, limit, offset)


class MerchantPatch(BaseModel):
    merchantName: str | None = None
    status: str | None = Field(None, pattern="^(ACTIVE|INACTIVE)$")
    rating: float | None = Field(None, ge=0, le=5)
    version: int


@app.patch("/admin/v1/merchants/{merchant_id}", tags=["catalog"])
async def patch_merchant(merchant_id: str, body: MerchantPatch, request: Request,
                         p: Annotated[Principal, Depends(require("merchant:write"))]):
    conn = await pg.pool()
    row = await conn.fetchrow(
        """UPDATE merchants SET merchant_name=COALESCE($2, merchant_name),
             status=COALESCE($3, status), rating=COALESCE($4, rating),
             version=version+1, updated_at=now()
           WHERE merchant_id=$1 AND version=$5 RETURNING *""",
        merchant_id, body.merchantName, body.status, body.rating, body.version)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "version conflict or merchant not found")
    await store.invalidate_all_recommendations()  # SERV-004
    await pg.audit(p.subject, p.role, "merchant.update", f"merchant/{merchant_id}",
                   changes=body.model_dump(exclude_none=True),
                   trace_id=request.state.trace_id)
    return dict(row)


@app.get("/admin/v1/promotions", tags=["catalog"])
async def list_promotions(p: Annotated[Principal, Depends(require("promotion:read"))],
                          limit: int = Query(50, le=500), offset: int = 0):
    return await pg.promotions(limit, offset)


class PromotionPatch(BaseModel):
    status: str | None = Field(None, pattern="^(DRAFT|ACTIVE|PAUSED|EXPIRED)$")
    endsAt: datetime | None = None
    campaignQuota: int | None = Field(None, ge=0)
    version: int


@app.patch("/admin/v1/promotions/{promotion_id}", tags=["catalog"])
async def patch_promotion(promotion_id: str, body: PromotionPatch, request: Request,
                          p: Annotated[Principal, Depends(require("promotion:write"))]):
    conn = await pg.pool()
    row = await conn.fetchrow(
        """UPDATE promotions SET status=COALESCE($2,status), ends_at=COALESCE($3,ends_at),
             campaign_quota=COALESCE($4,campaign_quota), version=version+1, updated_at=now()
           WHERE promotion_id=$1 AND version=$5 RETURNING *""",
        promotion_id, body.status, body.endsAt, body.campaignQuota, body.version)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "version conflict or promotion not found")
    await store.invalidate_all_recommendations()
    await pg.audit(p.subject, p.role, "promotion.update", f"promotion/{promotion_id}",
                   changes=body.model_dump(exclude_none=True),
                   trace_id=request.state.trace_id)
    return dict(row)


class EligibilityProbe(BaseModel):
    promotionId: str
    customerId: str


@app.post("/admin/v1/promotions/eligibility-preview", tags=["catalog"])
async def eligibility_preview(body: EligibilityProbe,
                              p: Annotated[Principal, Depends(require("promotion:read"))]):
    """UI-007 — explain why a promo would or would not be offered."""
    from rec.core.eligibility import evaluate
    from rec.core.models import CardTier, Customer

    promo_rows = await pg.promotions(limit=10000)
    promo = next((x for x in promo_rows if x.promotionId == body.promotionId), None)
    cust = await pg.customer(body.customerId)
    if promo is None or cust is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "promotion or customer not found")
    merchants = await pg.merchants(search=promo.merchantId, limit=1)
    if not merchants:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "merchant not found")
    redemptions = await pg.customer_redemption_counts(body.customerId)
    ok, reason = evaluate(
        promo,
        Customer(customerId=cust["customer_id"], cityCode=cust["city_code"],
                 cardTier=CardTier(cust["card_tier"]),
                 personalizationAllowed=cust["personalization_allowed"]),
        merchants[0], now=datetime.now(UTC),
        customer_redemptions=redemptions.get(promo.merchantId, 0))
    return {"eligible": ok, "reasonCode": reason, "promotion": promo}


# ===================================================================== ops


# ===================================================================== models


class TrainingRequest(BaseModel):
    datasetId: str
    numRounds: int = Field(300, ge=10, le=5000)
    testFraction: float = Field(0.25, gt=0.05, lt=0.9)
    xgboost: dict[str, Any] | None = None
    # Optuna trials on a validation slice of the training period; 0 = no tuning.
    tuneTrials: int = Field(0, ge=0, le=200)


@app.post("/admin/v1/training-jobs", status_code=202, tags=["models"])
async def create_training_job(body: TrainingRequest, request: Request,
                              p: Annotated[Principal, Depends(require("training:run"))]):
    try:
        job_id = await ml_jobs.create_training_job(
            body.datasetId,
            {"numRounds": body.numRounds, "testFraction": body.testFraction,
             "xgboost": body.xgboost, "tuneTrials": body.tuneTrials},
            request.headers.get("idempotency-key"))
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    await pg.audit(p.subject, p.role, "training.start", f"trainingJob/{job_id}",
                   changes=body.model_dump(exclude_none=True),
                   trace_id=request.state.trace_id)
    return {"jobId": job_id, "status": "QUEUED"}


@app.get("/admin/v1/training-jobs", tags=["models"])
async def list_training_jobs(p: Annotated[Principal, Depends(require("model:read"))],
                             limit: int = Query(25, le=100)):
    return await ml_jobs.list_jobs(limit)


@app.get("/admin/v1/training-jobs/{job_id}", tags=["models"])
async def get_training_job(job_id: str,
                           p: Annotated[Principal, Depends(require("model:read"))]):
    job = await ml_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "training job not found")
    return job


@app.get("/admin/v1/models", tags=["models"])
async def list_models(p: Annotated[Principal, Depends(require("model:read"))],
                      limit: int = Query(50, le=200)):
    return {"deployment": await registry.deployment(),
            "activeBaseline": registry.BASELINE_VERSION,
            "servingFeatureSchemaVersion": VECTOR_SCHEMA_VERSION,
            "models": await registry.list_models(limit)}


@app.get("/admin/v1/models/{model_version}", tags=["models"])
async def get_model(model_version: str,
                    p: Annotated[Principal, Depends(require("model:read"))]):
    model = await registry.get_model(model_version)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    return model


class PromoteRequest(BaseModel):
    mode: str = Field("SHADOW", pattern="^(SHADOW|CANARY|FULL)$")
    canaryPercent: int = Field(10, ge=0, le=100)
    note: str | None = None


@app.post("/admin/v1/models/{model_version}/promote", tags=["models"])
async def promote_model(model_version: str, body: PromoteRequest, request: Request,
                        p: Annotated[Principal, Depends(require("model:promote"))]):
    """Separation of duties: only an Approver promotes, and only a gated model.
    Offline metrics alone never promote (ML-006)."""
    # Warm before flipping: a model the ranking service cannot load must not be promoted,
    # and a cold artifact load can blow the serving timeout on the first requests.
    try:
        model = await registry.get_model(model_version)
        await ranking_client.warm(model_version, model and model["artifact_sha256"])
    except ranking_client.RankingUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"ranking service cannot load {model_version}: {exc.reason}")
    try:
        deployment = await registry.promote(
            model_version, mode=body.mode, canary_percent=body.canaryPercent,
            actor=p.subject, note=body.note)
    except registry.PromotionRefused as exc:
        await pg.audit(p.subject, p.role, "model.promote", f"model/{model_version}",
                       outcome="REJECTED", changes=body.model_dump() | {"reason": str(exc)},
                       trace_id=request.state.trace_id)
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await store.invalidate_all_recommendations()  # SERV-004: model version is in the key
    await pg.audit(p.subject, p.role, "model.promote", f"model/{model_version}",
                   changes=body.model_dump(), trace_id=request.state.trace_id)
    return deployment


@app.post("/admin/v1/models/{model_version}/rollback", tags=["models"])
async def rollback_model(model_version: str, request: Request,
                         p: Annotated[Principal, Depends(require("model:rollback"))],
                         note: str | None = None):
    deployment = await registry.rollback(p.subject, note=note)
    await store.invalidate_all_recommendations()
    await pg.audit(p.subject, p.role, "model.rollback", f"model/{model_version}",
                   changes={"note": note, "now": deployment.get("mode")},
                   trace_id=request.state.trace_id)
    return deployment


@app.get("/admin/v1/models/guardrail/status", tags=["models"])
async def guardrail_status(p: Annotated[Principal, Depends(require("model:read"))]):
    """Live-arm health the automatic rollback acts on (SDD 17.2 step 14)."""
    return await guardrail.status(store.r)


@app.get("/admin/v1/models/shadow/summary", tags=["models"])
async def shadow_summary(p: Annotated[Principal, Depends(require("model:read"))],
                         hours: int = Query(24, ge=1, le=720),
                         modelVersion: str | None = None):
    return await registry.shadow_summary(hours, modelVersion)


@app.get("/admin/v1/learning/status", tags=["models"])
async def learning_status(p: Annotated[Principal, Depends(require("model:read"))]):
    """ADR-0007/0010 at a glance: what the learning loops are set to and what they did."""
    conn = await pg.pool()
    last_auto = await conn.fetchrow(
        """SELECT job_id, dataset_id, status, created_at,
                  result->>'modelVersion' AS model_version,
                  (result->>'approved')::boolean AS approved
           FROM training_jobs WHERE params->>'trigger' = 'auto'
           ORDER BY created_at DESC LIMIT 1""")
    arms = {r["arm"]: r["customers"] for r in await conn.fetch(
        "SELECT arm, count(*) AS customers FROM promo_experiment GROUP BY arm")}
    return {
        "autoRetrain": {
            "enabled": settings.auto_retrain_interval_hours > 0,
            "intervalHours": settings.auto_retrain_interval_hours,
            "minNewImpressions": settings.auto_retrain_min_new_impressions,
            "keepExports": settings.auto_retrain_keep_exports,
            "lastJob": dict(last_auto) if last_auto else None,
        },
        "bandit": {
            "enabled": settings.online_bandit_enabled,
            "version": bandit.VERSION,
            "exploration": settings.online_bandit_exploration,
            "learnIntervalSeconds": settings.online_bandit_learn_interval_seconds,
            "learnedUntil": await store.r.get(bandit.WATERMARK_KEY),
            "shadow24h": await registry.shadow_summary(24, bandit.VERSION),
        },
        "promoHoldout": {
            "percent": settings.promo_holdout_percent,
            "arms": {"TREATMENT": arms.get("TREATMENT", 0), "HOLDOUT": arms.get("HOLDOUT", 0)},
        },
        "uplift": await uplift.latest_report(),
    }


@app.get("/admin/v1/learning/settings", tags=["models"])
async def get_learning_settings(p: Annotated[Principal, Depends(require("model:read"))]):
    return await learning_settings.overview()


class LearningChangeRequest(BaseModel):
    changes: learning_settings.LearningChanges
    reason: str = Field(min_length=3, max_length=500)


class LearningDecision(BaseModel):
    approve: bool
    note: str | None = Field(None, max_length=500)


@app.post("/admin/v1/learning/settings/requests", status_code=201, tags=["models"])
async def request_learning_change(body: LearningChangeRequest,
                                  p: Annotated[Principal, Depends(require("learning:request"))]):
    try:
        return await learning_settings.request_change(body.changes, body.reason,
                                                      p.subject, p.role)
    except learning_settings.Refused as exc:
        raise HTTPException(exc.status, str(exc))


@app.post("/admin/v1/learning/settings/requests/{request_id}/decision", tags=["models"])
async def decide_learning_change(request_id: str, body: LearningDecision,
                                 p: Annotated[Principal, Depends(require("learning:approve"))]):
    try:
        return await learning_settings.decide(request_id, body.approve, body.note,
                                              p.subject, p.role)
    except learning_settings.Refused as exc:
        raise HTTPException(exc.status, str(exc))


@app.get("/admin/v1/metrics/overview", tags=["ops"])
async def metrics_overview(p: Annotated[Principal, Depends(require("metrics:read"))]):
    """UI-001 — unavailable metrics are returned as null, never as zero."""
    counters = await store.metrics()
    deployment = await registry.deployment()
    conn = await pg.pool()
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE outcome='APPLIED') AS applied,
                  count(*) FILTER (WHERE outcome='QUARANTINED') AS quarantined,
                  count(*) FILTER (WHERE outcome LIKE 'DUPLICATE%') AS duplicates,
                  count(*) AS total,
                  max(received_at) AS last_received,
                  avg(extract(epoch FROM received_at - occurred_at))
                    FILTER (WHERE received_at > now() - interval '15 minutes')
                    AS freshness_seconds,
                  count(*) FILTER (WHERE received_at > now() - interval '1 minute') AS last_minute
           FROM transaction_log""")
    served = counters.get("recommendation_served", 0)
    hits = counters.get("recommendation_cache_hit", 0)
    misses = counters.get("recommendation_cache_miss", 0)
    return {
        "generatedAt": datetime.now(UTC),
        "ingestion": {
            "appliedEvents": row["applied"], "quarantinedEvents": row["quarantined"],
            "duplicateEvents": row["duplicates"], "totalEvents": row["total"],
            "lastEventReceivedAt": row["last_received"],
            "throughputPerSecond": round((row["last_minute"] or 0) / 60, 2),
        },
        "freshness": {
            "eventToServingSeconds": round(row["freshness_seconds"], 2)
            if row["freshness_seconds"] is not None else None,
        },
        "serving": {
            "recommendationsServed": served,
            "modelScoredRequests": counters.get("ranking_model_scored", 0),
            "rankingDegradedRate": round(counters.get("ranking_degraded", 0) / served, 4)
            if served else None,
            "shadowEvaluations": counters.get("shadow_evaluations", 0),
            "cacheHitRate": round(hits / (hits + misses), 4) if (hits + misses) else None,
            "fallbackRate": round(counters.get("recommendation_fallback", 0) / served, 4)
            if served else None,
            "activeModel": deployment.get("model_version") or registry.BASELINE_VERSION,
            "deploymentMode": deployment.get("mode"),
            "canaryPercent": deployment.get("canary_percent"),
            "rankingConfigVersion": RANKING_CONFIG_VERSION,
            "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
        },
        "feedback": {
            "impressions": counters.get("impressions", 0),
            "clicks": counters.get("interaction_CLICK", 0),
            "redemptions": counters.get("interaction_REDEMPTION", 0),
        },
        "quarantineByReason": {k[len("quarantine_"):]: v for k, v in counters.items()
                               if k.startswith("quarantine_")},
    }


@app.get("/admin/v1/audit-events", tags=["ops"])
async def audit_events(p: Annotated[Principal, Depends(require("audit:read"))],
                       cursor: int | None = None, limit: int = Query(50, le=200)):
    conn = await pg.pool()
    rows = await conn.fetch(
        """SELECT * FROM audit_events WHERE ($1::bigint IS NULL OR id < $1)
           ORDER BY id DESC LIMIT $2""", cursor, limit)
    items = [dict(r) | {"changes": json.loads(r["changes"]) if r["changes"] else None}
             for r in rows]
    return {"items": items, "nextCursor": items[-1]["id"] if len(items) == limit else None}


# ===================================================================== erasure (AC-009)


class ErasureRequest(BaseModel):
    customerId: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)


@app.post("/admin/v1/erasure-requests", status_code=201, tags=["privacy"])
async def request_erasure(body: ErasureRequest, request: Request,
                          p: Annotated[Principal, Depends(require("erasure:request"))]):
    conn = await pg.pool()
    request_id = str(uuid.uuid4())
    try:
        await conn.execute(
            """INSERT INTO erasure_requests (request_id, customer_id, reason, requested_by)
               VALUES ($1,$2,$3,$4)""", request_id, body.customerId, body.reason, p.subject)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status.HTTP_409_CONFLICT, "an erasure request is already pending")
    await pg.audit(p.subject, p.role, "erasure.request", f"customer/{body.customerId}",
                   changes={"requestId": request_id, "reason": body.reason},
                   trace_id=request.state.trace_id)
    return {"requestId": request_id, "status": "PENDING"}


@app.get("/admin/v1/erasure-requests", tags=["privacy"])
async def list_erasures(p: Annotated[Principal, Depends(require("erasure:read"))],
                        limit: int = Query(50, le=200)):
    conn = await pg.pool()
    rows = await conn.fetch("SELECT * FROM erasure_requests ORDER BY created_at DESC LIMIT $1",
                            limit)
    return [dict(r) | {"result": json.loads(r["result"]) if r["result"] else None}
            for r in rows]


class ErasureDecision(BaseModel):
    approve: bool
    note: str | None = Field(None, max_length=500)


@app.post("/admin/v1/erasure-requests/{request_id}/decision", tags=["privacy"])
async def decide_erasure(request_id: str, body: ErasureDecision, request: Request,
                         p: Annotated[Principal, Depends(require("erasure:approve"))]):
    """Approval executes immediately: Postgres rows go in one transaction with the
    tombstone, then Redis state and cache. Audit rows are kept (immutable, and they hold
    only the pseudonymous id) — see docs/runbooks.md."""
    conn = await pg.pool()
    row = await conn.fetchrow("SELECT * FROM erasure_requests WHERE request_id=$1", request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "erasure request not found")
    if row["status"] != "PENDING":
        raise HTTPException(status.HTTP_409_CONFLICT, f"request is {row['status']}")
    if row["requested_by"] == p.subject:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requester cannot approve own request")
    customer_id = row["customer_id"]
    result: dict = {}
    if body.approve:
        await store.load_tombstones([customer_id])  # stop the stream before deleting
        async with conn.acquire() as con, con.transaction():
            await con.execute("""INSERT INTO erased_customers (customer_id, request_id)
                                 VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                              customer_id, request_id)
            for table in ("transaction_log", "impressions", "interactions",
                          "shadow_evaluations", "promo_experiment", "customers"):
                sql = f"DELETE FROM {table} WHERE customer_id=$1"  # nosec B608
                tag = await con.execute(sql, customer_id)
                result[table] = int(tag.split()[-1])
        result["redisKeys"] = (await store.erase_customer(customer_id)
                               + await bandit.erase(store, customer_id))
    await conn.execute(
        """UPDATE erasure_requests SET status=$2, decided_by=$3, decided_at=now(), result=$4
           WHERE request_id=$1""", request_id, "EXECUTED" if body.approve else "REJECTED",
        p.subject, json.dumps(result | {"note": body.note}))
    await pg.audit(p.subject, p.role, "erasure.execute" if body.approve else "erasure.reject",
                   f"customer/{customer_id}", changes={"requestId": request_id, **result},
                   trace_id=request.state.trace_id)
    return {"requestId": request_id, "status": "EXECUTED" if body.approve else "REJECTED",
            "deleted": result}


@app.get("/admin/v1/events", tags=["ops"])
async def sse(p: Annotated[Principal, Depends(require("metrics:read"))]):
    """SSE job/operation status. The dashboard falls back to polling on disconnect."""
    async def stream():
        bus = jobs.event_bus()
        yield b": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(bus.get(), timeout=15)
                yield f"data: {json.dumps(event, default=str)}\n\n".encode()
            except TimeoutError:
                yield b": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


@app.get("/metrics", tags=["ops"], include_in_schema=False)
async def metrics():
    """Prometheus scrape. No PII in any label; reachable only on the internal network in
    production (network policy), like /health."""
    body, media_type = metrics_body()
    return Response(body, media_type=media_type)


@app.get("/health", tags=["ops"])
async def health():
    checks = {}
    try:
        await store.r.ping()
        checks["redis"] = "UP"
    except Exception as exc:
        checks["redis"] = f"DOWN: {exc}"
    try:
        conn = await pg.pool()
        await conn.fetchval("SELECT 1")
        checks["postgres"] = "UP"
    except Exception as exc:
        checks["postgres"] = f"DOWN: {exc}"
    ok = all(v == "UP" for v in checks.values())
    return Response(json.dumps({"status": "UP" if ok else "DEGRADED", "checks": checks}),
                    status_code=200 if ok else 503, media_type="application/json")


@app.get("/admin/v1/me", tags=["ops"])
async def me(p: Annotated[Principal, Depends(principal)]):
    from rec.api.auth import PERMISSIONS
    return {"subject": p.subject, "role": p.role, "kind": p.kind,
            "permissions": sorted(a for a in PERMISSIONS if p.can(a)),
            "csrfToken": p.csrf}
