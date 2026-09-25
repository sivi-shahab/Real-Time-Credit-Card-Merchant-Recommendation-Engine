#!/usr/bin/env bash
# Fase 4 end-to-end over the real stack: train -> gate -> promote -> serve -> degrade
# -> rollback. Requires `docker compose up -d` and a dataset that already has feedback.
set -euo pipefail
API="${API:-http://localhost:8000}"
RANK="${RANK:-http://localhost:8100}"
ML="Authorization: Bearer ml-token"
APPROVER="Authorization: Bearer approver-token"
ADMIN="Authorization: Bearer admin-token"
JSON="content-type: application/json"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
jq_py() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

DS="${1:-}"
if [ -z "$DS" ]; then
  say "pick the newest completed dataset"
  DS=$(curl -fsS "$API/admin/v1/datasets?limit=25" -H "$ADMIN" | jq_py \
    'print(next(x["dataset_id"] for x in d if x["status"]=="COMPLETED"))')
fi
echo "datasetId=$DS"

say "ranking service"
curl -fsS "$RANK/health" | python3 -m json.tool

say "start training (ML Engineer)"
JOB=$(curl -fsS -X POST "$API/admin/v1/training-jobs" -H "$ML" -H "$JSON" \
  -d "{\"datasetId\":\"$DS\",\"numRounds\":200}" | jq_py 'print(d["jobId"])')
echo "jobId=$JOB"
for _ in $(seq 1 150); do
  STATUS=$(curl -fsS "$API/admin/v1/training-jobs/$JOB" -H "$ML" | jq_py 'print(d["status"])')
  [ "$STATUS" = "COMPLETED" ] && break
  [ "$STATUS" = "FAILED" ] && {
    curl -fsS "$API/admin/v1/training-jobs/$JOB" -H "$ML" | jq_py 'print(d["error"])'; exit 1; }
  sleep 4
done
echo "training=$STATUS"

say "evaluation gates"
curl -fsS "$API/admin/v1/training-jobs/$JOB" -H "$ML" | jq_py '
r=d["result"]
print("model   ", r["modelVersion"], "approved", r["approved"])
print("ndcg@10 ", round(r["metrics"]["ndcg@10"],4), "baseline", round(r["baselineMetrics"]["ndcg@10"],4))
print("ndcg@5  ", round(r["metrics"]["ndcg@5"],4), "baseline", round(r["baselineMetrics"]["ndcg@5"],4))
print("latency ", round(r["metrics"]["inferenceLatencyMsP95"],2), "ms p95")
[print(("PASS " if g["passed"] else "FAIL "), g["name"], "|", g["detail"]) for g in r["gates"]]
print("caveat: ", r["datasetLineage"]["metricCaveat"])'
MV=$(curl -fsS "$API/admin/v1/training-jobs/$JOB" -H "$ML" | jq_py 'print(d["result"]["modelVersion"])')

say "separation of duties: ML Engineer must not promote"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "$API/admin/v1/models/$MV/promote" -H "$ML" -H "$JSON" -d '{"mode":"FULL"}')
[ "$CODE" = "403" ] || { echo "expected 403, got $CODE"; exit 1; }
echo "403 as expected"

CUST=$(curl -fsS "$API/admin/v1/customers?limit=1" -H "Authorization: Bearer analyst-token" \
  | jq_py 'print(d[0]["customer_id"])')

say "promote SHADOW (serving must not change)"
curl -fsS -X POST "$API/admin/v1/models/$MV/promote" -H "$APPROVER" -H "$JSON" \
  -d '{"mode":"SHADOW"}' | jq_py 'print("mode", d["mode"])'
for i in $(seq 1 5); do
  curl -fsS "$API/api/v1/customer/$CUST/recommendations?refresh=true&limit=$((9+i))" \
    -H "Authorization: Bearer cust-$CUST" > /dev/null
done
curl -fsS "$API/api/v1/customer/$CUST/recommendations?refresh=true" \
  -H "Authorization: Bearer cust-$CUST" \
  | jq_py 'print("served by", d["modelVersion"], "source", d["source"])'
sleep 3
curl -fsS "$API/admin/v1/models/shadow/summary?hours=1" -H "$ML" | python3 -m json.tool

say "promote FULL and serve with the model"
curl -fsS -X POST "$API/admin/v1/models/$MV/promote" -H "$APPROVER" -H "$JSON" \
  -d '{"mode":"FULL"}' | jq_py 'print("mode", d["mode"], "canary", d["canary_percent"])'
curl -fsS "$API/api/v1/customer/$CUST/recommendations?refresh=true" \
  -H "Authorization: Bearer cust-$CUST" | jq_py '
print("served by", d["modelVersion"], "source", d["source"])
assert d["source"]=="LIVE", "expected the model to rank"
[print(" ", i["rank"], i["merchantId"], round(i["score"],4), i["reasonCodes"]) for i in d["recommendations"][:5]]'

say "AC-004: kill the ranking service, recommendations must survive"
docker compose stop ranking > /dev/null
curl -fsS "$API/api/v1/customer/$CUST/recommendations?refresh=true" \
  -H "Authorization: Bearer cust-$CUST" | jq_py '
print("served by", d["modelVersion"], "source", d["source"], "items", len(d["recommendations"]))
assert d["source"]=="FALLBACK" and d["recommendations"], "must degrade to baseline, not fail"'
docker compose start ranking > /dev/null
for _ in $(seq 1 30); do curl -fsS "$RANK/health" > /dev/null 2>&1 && break; sleep 2; done

say "rollback"
curl -fsS -X POST "$API/admin/v1/models/$MV/rollback" -H "$APPROVER" \
  | jq_py 'print("mode", d["mode"], "model", d["model_version"])'

say "SMOKE ML OK  dataset=$DS model=$MV"
