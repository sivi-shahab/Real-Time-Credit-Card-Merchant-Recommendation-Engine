#!/usr/bin/env bash
# Disaster-recovery drill (SDD 19: "restore, replay, rollback sudah dipraktikkan").
# Destroys Postgres data and Redis on the LOCAL stack, restores from a fresh backup,
# rebuilds Redis, then proves counts and features match what was there before.
set -euo pipefail
cd "$(dirname "$0")/.."
API="${API:-http://localhost:8000}"
ADMIN="Authorization: Bearer admin-token"
DC="docker compose"
say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

counts() {
  $DC exec -T postgres psql -U rec -d rec -At -c "SELECT
    (SELECT count(*) FROM transaction_log)||' '||(SELECT count(*) FROM customers)||' '||
    (SELECT count(*) FROM audit_events)||' '||(SELECT count(*) FROM models)||' '||
    (SELECT count(*) FROM erased_customers)"
}
features() {  # the fields that must survive; featureAsOf moves with the clock
  for c in $CUSTOMERS; do
    curl -fsS "$API/admin/v1/customers/$c/features" -H "$ADMIN" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);f=d.get("features",d);print(sys.argv[1],f["transactionCount90d"],f["netSpend90d"])' "$c"
  done
}

# the busiest customers: an empty profile would compare equal to anything
CUSTOMERS=$($DC exec -T postgres psql -U rec -d rec -At -c "SELECT customer_id FROM transaction_log
  WHERE outcome='APPLIED' GROUP BY 1 ORDER BY count(*) DESC LIMIT 25")

say "snapshot before"
BEFORE_COUNTS=$(counts); features > /tmp/dr_before.txt
echo "counts: $BEFORE_COUNTS  (txn_log customers audit models erased)"
[ -n "$CUSTOMERS" ] || { echo "no applied transactions — run smoke_e2e.sh first"; exit 1; }

say "backup"
DIR=$(scripts/backup.sh backup | tail -1); echo "$DIR"

say "disaster: drop every table, flush Redis"
$DC exec -T postgres psql -U rec -d rec -q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
$DC exec -T redis redis-cli FLUSHALL >/dev/null
START=$(date +%s)

say "restore + rebuild"
scripts/backup.sh restore "$DIR"
RTO=$(( $(date +%s) - START ))

say "verify"
AFTER_COUNTS=$(counts); features > /tmp/dr_after.txt
echo "counts before: $BEFORE_COUNTS"
echo "counts after:  $AFTER_COUNTS"
# audit gains rows for the drill's own reads; everything else must match exactly
[ "$(echo "$BEFORE_COUNTS" | cut -d' ' -f1,2,4,5)" = "$(echo "$AFTER_COUNTS" | cut -d' ' -f1,2,4,5)" ] \
  || { echo "ROW COUNTS DIFFER"; exit 1; }
diff /tmp/dr_before.txt /tmp/dr_after.txt || { echo "FEATURES DIFFER"; exit 1; }
say "DR DRILL OK  backup=$DIR  restore+rebuild=${RTO}s  customers-checked=$(wc -l < /tmp/dr_after.txt)"
