#!/usr/bin/env bash
# Resilience drill on the LOCAL compose stack (SDD 16: consumer restart, broker loss,
# Redis loss, model timeout). Needs a dataset already loaded (scripts/smoke_e2e.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
API="${API:-http://localhost:8000}"
ADMIN="Authorization: Bearer admin-token"
JSON="content-type: application/json"
DC="docker compose"
say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
field() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

CUST=$(curl -fsS "$API/admin/v1/customers?limit=1" -H "$ADMIN" | field 'd[0]["customer_id"]')
rec() { curl -sS -o /tmp/chaos_rec.json -w '%{http_code}' \
          "$API/api/v1/customer/$CUST/recommendations?refresh=true" -H "Authorization: Bearer cust-$CUST"; }

say "1. Redis loss -> popular fallback, still HTTP 200"
$DC stop redis >/dev/null
CODE=$(rec); SRC=$(field 'd["source"]' < /tmp/chaos_rec.json)
echo "status=$CODE source=$SRC"
[ "$CODE" = 200 ] && [ "$SRC" = FALLBACK ] || { echo "FAIL"; exit 1; }
$DC start redis >/dev/null; sleep 3
$DC exec -T api python scripts/rebuild_state.py >/dev/null   # re-apply anything missed
CODE=$(rec); SRC=$(field 'd["source"]' < /tmp/chaos_rec.json)
echo "after recovery: status=$CODE source=$SRC"; [ "$SRC" = LIVE ] || { echo "FAIL"; exit 1; }

say "2. Ranking service down -> baseline, never an error"
MODE=$(curl -fsS "$API/admin/v1/metrics/overview" -H "$ADMIN" | field 'd["serving"]["deploymentMode"]')
if [ "$MODE" = FULL ] || [ "$MODE" = CANARY ]; then
  $DC stop ranking >/dev/null
  CODE=$(rec); echo "status=$CODE model=$(field 'd["modelVersion"]' < /tmp/chaos_rec.json)"
  [ "$CODE" = 200 ] || { echo "FAIL"; exit 1; }
  $DC start ranking >/dev/null
else
  echo "SKIP: deployment mode is $MODE (promote a model with scripts/smoke_ml.sh first)"
fi

say "3. Broker loss + consumer kill mid-replay -> no loss, no double count (AC-001/008)"
NS="chaos$(date +%s)"
DS=$(curl -fsS -X POST "$API/admin/v1/datasets" -H "$ADMIN" -H "$JSON" \
  -d "{\"seed\":7,\"idNamespace\":\"$NS\",\"customerCount\":150,\"merchantCount\":60,
       \"promotionCount\":10,\"transactionCount\":3000,\"outputFormats\":[\"jsonl\"]}" | field 'd["datasetId"]')
for _ in $(seq 1 60); do
  [ "$(curl -fsS "$API/admin/v1/datasets/$DS" -H "$ADMIN" | field 'd["status"]')" = COMPLETED ] && break; sleep 2
done
RUN=$(curl -fsS -X POST "$API/admin/v1/simulations" -H "$ADMIN" -H "$JSON" \
  -d "{\"datasetId\":\"$DS\",\"targetTps\":300}" | field 'd["run_id"]')
curl -fsS -X POST "$API/admin/v1/simulations/$RUN/start" -H "$ADMIN" >/dev/null
sleep 3
echo "killing the consumer (SIGKILL, no graceful commit)"; $DC kill stream >/dev/null
sleep 2; $DC start stream >/dev/null
echo "stopping the broker"; $DC stop kafka >/dev/null; sleep 8; $DC start kafka >/dev/null
for _ in $(seq 1 120); do
  S=$(curl -fsS "$API/admin/v1/simulations/$RUN" -H "$ADMIN" | field 'd["status"]+" "+str(d["offset_pos"])+"/"+str(d["total_events"])')
  echo "run: $S"
  case "$S" in
    COMPLETED*) break ;;
    FAILED*) echo "resuming from checkpoint"; sleep 5
             curl -fsS -X POST "$API/admin/v1/simulations/$RUN/resume" -H "$ADMIN" >/dev/null ;;
  esac
  sleep 5
done
[ "${S%% *}" = COMPLETED ] || { echo "FAIL: replay did not complete"; exit 1; }
echo "waiting for consumer lag to reach 0"   # a stalled consumer also looks "stable"
for _ in $(seq 1 120); do
  LAG=$($DC exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
        --describe --group feature-engine 2>/dev/null | awk '$6 ~ /^[0-9]+$/ {s+=$6} END {print s+0}')
  echo "lag=$LAG"; [ "$LAG" = 0 ] && break; sleep 3
done
$DC exec -T api python scripts/reconcile.py "$DS" --limit 150

say "CHAOS OK"
