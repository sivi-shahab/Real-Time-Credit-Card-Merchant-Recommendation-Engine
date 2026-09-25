#!/usr/bin/env bash
# Backup / restore for the compose stack. Runbook: docs/runbooks.md#backup-and-restore
#
#   scripts/backup.sh backup  [dir]   -> Postgres dump + model artifacts + MLflow + checksums
#   scripts/backup.sh restore <dir>   -> verify checksums, restore, rebuild Redis from Postgres
#
# Redis is deliberately not backed up: it is derived state (SDD 3.3) and is rebuilt from
# the transaction log, which also re-applies erasure tombstones (AC-009).
set -euo pipefail
cd "$(dirname "$0")/.."
DC="docker compose"

backup() {
  local out="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
  mkdir -p "$out"
  $DC exec -T postgres pg_dump -U rec -Fc rec > "$out/rec.dump"
  tar -czf "$out/artifacts.tgz" -C data $(cd data && ls -d models mlflow.db mlartifacts 2>/dev/null)
  (cd "$out" && sha256sum rec.dump artifacts.tgz > SHA256SUMS)
  echo "$out"
}

restore() {
  local in="${1:?backup dir required}"
  (cd "$in" && sha256sum -c --quiet SHA256SUMS)
  $DC stop api stream ranking >/dev/null
  $DC exec -T postgres pg_restore -U rec -d rec --clean --if-exists --no-owner < "$in/rec.dump"
  tar -xzf "$in/artifacts.tgz" -C data
  $DC start ranking api stream >/dev/null
  for _ in $(seq 1 60); do curl -fsS localhost:8000/health >/dev/null 2>&1 && break; sleep 2; done
  $DC exec -T api python scripts/rebuild_state.py --flush
}

case "${1:-}" in
  backup) backup "${2:-}" ;;
  restore) restore "${2:-}" ;;
  *) echo "usage: $0 backup [dir] | restore <dir>" >&2; exit 2 ;;
esac
