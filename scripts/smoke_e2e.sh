#!/usr/bin/env bash
# End-to-end smoke over the real stack: dataset -> Kafka replay -> features -> API.
set -euo pipefail
API="${API:-http://localhost:8000}"
ADMIN="Authorization: Bearer admin-token"
JSON="content-type: application/json"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "health"
curl -fsS "$API/health" | tee /dev/stderr | grep -q '"status": *"UP"'

say "create dataset"
NS="smoke$(date +%s)"
DS=$(curl -fsS -X POST "$API/admin/v1/datasets" -H "$ADMIN" -H "$JSON" \
  -d "{\"seed\":42,\"idNamespace\":\"$NS\",\"customerCount\":400,\"merchantCount\":120,
       \"promotionCount\":25,\"transactionCount\":8000,\"historyDays\":120,
       \"outputFormats\":[\"jsonl\"]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["datasetId"])')
echo "datasetId=$DS"

say "wait for generation"
for _ in $(seq 1 120); do
  STATUS=$(curl -fsS "$API/admin/v1/datasets/$DS" -H "$ADMIN" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = "COMPLETED" ] && break
  [ "$STATUS" = "FAILED" ] && { echo "generation failed"; exit 1; }
  sleep 2
done
echo "status=$STATUS"

say "start replay"
RUN=$(curl -fsS -X POST "$API/admin/v1/simulations" -H "$ADMIN" -H "$JSON" \
  -d "{\"datasetId\":\"$DS\",\"targetTps\":800}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
curl -fsS -X POST "$API/admin/v1/simulations/$RUN/start" -H "$ADMIN" > /dev/null
for _ in $(seq 1 180); do
  S=$(curl -fsS "$API/admin/v1/simulations/$RUN" -H "$ADMIN" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["sent_count"])')
  echo "run: $S"
  case "$S" in COMPLETED*) break;; FAILED*|STOPPED*) exit 1;; esac
  sleep 3
done

say "wait for the feature engine to drain"
PREV=-1
for _ in $(seq 1 120); do
  CUR=$(curl -fsS "$API/admin/v1/metrics/overview" -H "$ADMIN" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["ingestion"]["totalEvents"])')
  echo "consumed total=$CUR"
  [ "$CUR" = "$PREV" ] && break
  PREV=$CUR
  sleep 5
done
curl -fsS "$API/admin/v1/metrics/overview" -H "$ADMIN" | python3 -m json.tool

say "recommendation for the busiest customer"
CUST=$(curl -fsS "$API/admin/v1/customers?limit=1&search=$NS" -H "$ADMIN" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["customer_id"])')
curl -fsS "$API/api/v1/customer/$CUST/recommendations" \
  -H "Authorization: Bearer cust-$CUST" | python3 -m json.tool

say "reconcile online vs offline (AC-008)"
python3 scripts/reconcile.py "$DS" --limit 100

say "SMOKE OK  dataset=$DS customer=$CUST"
