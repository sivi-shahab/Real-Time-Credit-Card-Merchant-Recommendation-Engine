# ADR-0001 — Python-first implementation of a polyglot specification

**Status:** Accepted · 2026-09-24

## Context
The SDD specifies Spring Boot for ingestion/candidate/recommendation/BFF, Kafka Streams
for the feature engine, and FastAPI only for ranking. Building five JVM services plus a
Streams topology before any end-to-end behaviour exists delays the first verifiable
slice by weeks, and the SDD itself (§19) recommends proving the data path first.

## Decision
Implement Fase 1–3 in a single Python codebase: FastAPI for serving and admin, an
aiokafka consumer for the feature engine, Postgres for master data, Redis for the online
store, Vue 3 for the dashboard. Kafka, the event envelope, the topic names, and the REST
contract stay exactly as specified.

## Consequences
- The contracts (topics, envelope, OpenAPI paths, feature schema) are the ported surface,
  so a service can be rewritten in Spring Boot behind the same contract without touching
  callers.
- Kafka Streams' `exactly_once_v2` is not available; the consumer is at-least-once with
  idempotent handling instead (see ADR-0003).
- One language means one test suite and one deployment story for the prototype.
