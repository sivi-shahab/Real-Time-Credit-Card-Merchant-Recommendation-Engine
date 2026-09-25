"""SIM-001..003 — replay a dataset into Kafka with rate control and checkpoints.

State machine: CREATED -> RUNNING <-> PAUSED -> COMPLETED | STOPPED | FAILED.
Invalid commands for the current status are rejected here, not in the UI.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from aiokafka import AIOKafkaProducer

from rec.settings import settings
from rec.store import pg

TRANSITIONS = {
    "CREATED": {"RUNNING", "STOPPED"},
    "RUNNING": {"PAUSED", "STOPPED", "COMPLETED", "FAILED"},
    "PAUSED": {"RUNNING", "STOPPED"},
    "COMPLETED": set(),
    "STOPPED": set(),
    "FAILED": {"RUNNING"},  # resume from the checkpoint once the broker is back
}
SEND_ATTEMPTS = 5
COMMAND_TARGET = {"start": "RUNNING", "pause": "PAUSED", "resume": "RUNNING", "stop": "STOPPED"}


class InvalidTransition(Exception):
    pass


class SimulationManager:
    """Runs replays as asyncio tasks in-process. One task per runId."""

    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self.commands: dict[str, str] = {}

    def _dataset_path(self, dataset_id: str) -> Path:
        return Path(settings.data_dir) / dataset_id / "replay.jsonl"

    async def create(self, dataset_id: str, target_tps: int, speed_multiplier: float = 1.0,
                     idempotency_key: str | None = None) -> dict:
        if settings.environment not in ("local", "staging"):
            raise PermissionError("simulator is disabled outside local/staging (SIM-003)")
        path = self._dataset_path(dataset_id)
        if not path.exists():
            raise FileNotFoundError(f"replay file missing for dataset {dataset_id}")
        target_tps = max(1, min(target_tps, settings.max_simulator_tps))

        p = await pg.pool()
        if idempotency_key:
            existing = await p.fetchrow(
                "SELECT * FROM simulation_runs WHERE idempotency_key = $1", idempotency_key)
            if existing:
                return dict(existing)
        total = sum(1 for _ in path.open())
        run_id = str(uuid.uuid4())
        await p.execute(
            """INSERT INTO simulation_runs (run_id, dataset_id, status, target_tps, total_events,
                 speed_multiplier, idempotency_key)
               VALUES ($1,$2,'CREATED',$3,$4,$5,$6)""",
            run_id, dataset_id, target_tps, total, speed_multiplier, idempotency_key)
        return await self.get(run_id)

    async def get(self, run_id: str) -> dict:
        p = await pg.pool()
        row = await p.fetchrow("SELECT * FROM simulation_runs WHERE run_id = $1", run_id)
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    async def list(self, limit: int = 50) -> list[dict]:
        p = await pg.pool()
        return [dict(r) for r in await p.fetch(
            "SELECT * FROM simulation_runs ORDER BY created_at DESC LIMIT $1", limit)]

    async def command(self, run_id: str, command: str) -> dict:
        run = await self.get(run_id)
        target = COMMAND_TARGET[command]
        if target not in TRANSITIONS[run["status"]]:
            raise InvalidTransition(f"{command} invalid from {run['status']}")
        if command in ("start", "resume"):
            await self._set_status(run_id, "RUNNING")
            self.commands[run_id] = "run"
            if run_id not in self.tasks or self.tasks[run_id].done():
                self.tasks[run_id] = asyncio.create_task(self._replay(run_id))
        else:
            self.commands[run_id] = "pause" if command == "pause" else "stop"
            await self._set_status(run_id, target)
        return await self.get(run_id)

    async def _set_status(self, run_id: str, status: str) -> None:
        p = await pg.pool()
        await p.execute(
            "UPDATE simulation_runs SET status=$2, updated_at=now() WHERE run_id=$1",
            run_id, status)

    async def _replay(self, run_id: str) -> None:
        run = await self.get(run_id)
        path = self._dataset_path(run["dataset_id"])
        producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap,
                                    enable_idempotence=True, acks="all")
        p = await pg.pool()
        sent, failed = run["sent_count"], run["failed_count"]
        offset = run["offset_pos"]
        interval = 1.0 / max(1, run["target_tps"] * float(run["speed_multiplier"]))
        window_start, window_sent = time.perf_counter(), 0
        try:
            await producer.start()
            with path.open() as fh:
                for index, line in enumerate(fh):
                    if index < offset:  # SIM-002: resume from checkpoint
                        continue
                    cmd = self.commands.get(run_id, "run")
                    while cmd == "pause":
                        await asyncio.sleep(0.2)
                        cmd = self.commands.get(run_id, "run")
                    if cmd == "stop":
                        return
                    envelope = json.loads(line)
                    # SIM-002: never skip an event. Retry with backoff; if the broker
                    # stays down, fail with the checkpoint still on this event.
                    for attempt in range(SEND_ATTEMPTS):
                        try:
                            await producer.send_and_wait(
                                settings.topic_transactions, line.encode(),
                                key=envelope["payload"]["customerId"].encode())
                            sent += 1
                            break
                        except Exception:
                            failed += 1
                            if attempt == SEND_ATTEMPTS - 1:
                                raise
                            await asyncio.sleep(min(2 ** attempt, 10))
                    offset = index + 1
                    window_sent += 1
                    if window_sent >= 200:
                        elapsed = time.perf_counter() - window_start
                        await p.execute(
                            """UPDATE simulation_runs SET offset_pos=$2, sent_count=$3,
                                 failed_count=$4, actual_tps=$5, updated_at=now()
                               WHERE run_id=$1""",
                            run_id, offset, sent, failed,
                            round(window_sent / elapsed, 2) if elapsed > 0 else 0)
                        window_start, window_sent = time.perf_counter(), 0
                    await asyncio.sleep(interval)
            await self._set_status(run_id, "COMPLETED")
        except Exception as exc:
            await p.execute("UPDATE simulation_runs SET status='FAILED', updated_at=now() "
                            "WHERE run_id=$1", run_id)
            raise exc
        finally:
            await p.execute(
                """UPDATE simulation_runs SET offset_pos=$2, sent_count=$3, failed_count=$4,
                     updated_at=now() WHERE run_id=$1""", run_id, offset, sent, failed)
            await producer.stop()


manager = SimulationManager()
