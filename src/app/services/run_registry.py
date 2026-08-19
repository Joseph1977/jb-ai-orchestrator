# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Pod-local run registry for same-pod cancellation optimization only."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from app.utils.logger import logger


@dataclass
class RegisteredRun:
    run_pk: uuid.UUID
    execution_id: uuid.UUID
    thread_key: Optional[str]
    task: asyncio.Task
    cancel_event: asyncio.Event


class RunRegistry:
    """In-memory index of active asyncio tasks; not authoritative lifecycle state."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_run: dict[uuid.UUID, RegisteredRun] = {}
        self._by_execution: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        self._by_thread: dict[str, set[uuid.UUID]] = defaultdict(set)

    async def register(
        self,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        task: asyncio.Task,
        cancel_event: asyncio.Event,
        thread_key: Optional[str] = None,
    ) -> None:
        async with self._lock:
            entry = RegisteredRun(
                run_pk=run_pk,
                execution_id=execution_id,
                thread_key=thread_key,
                task=task,
                cancel_event=cancel_event,
            )
            self._by_run[run_pk] = entry
            self._by_execution[execution_id].add(run_pk)
            if thread_key:
                self._by_thread[thread_key].add(run_pk)

    async def unregister(self, run_pk: uuid.UUID) -> None:
        async with self._lock:
            entry = self._by_run.pop(run_pk, None)
            if entry is None:
                return
            exec_ids = self._by_execution.get(entry.execution_id)
            if exec_ids is not None:
                exec_ids.discard(run_pk)
                if not exec_ids:
                    del self._by_execution[entry.execution_id]
            if entry.thread_key:
                thread_ids = self._by_thread.get(entry.thread_key)
                if thread_ids is not None:
                    thread_ids.discard(run_pk)
                    if not thread_ids:
                        del self._by_thread[entry.thread_key]

    async def get(self, run_pk: uuid.UUID) -> Optional[RegisteredRun]:
        async with self._lock:
            return self._by_run.get(run_pk)

    async def _cancel_entries(self, entries: list[RegisteredRun]) -> int:
        count = 0
        for entry in entries:
            if entry.cancel_event.is_set() and entry.task.done():
                continue
            entry.cancel_event.set()
            if not entry.task.done():
                entry.task.cancel()
            count += 1
        return count

    async def request_cancel_for_execution(self, execution_id: uuid.UUID) -> int:
        async with self._lock:
            run_ids = list(self._by_execution.get(execution_id, ()))
            entries = [self._by_run[run_id] for run_id in run_ids if run_id in self._by_run]
        cancelled = await self._cancel_entries(entries)
        if cancelled:
            logger.info(
                "Requested local cancellation for %d run(s) on execution_id=%s",
                cancelled,
                execution_id,
            )
        return cancelled

    async def request_cancel_for_thread(self, thread_key: str) -> int:
        async with self._lock:
            run_ids = list(self._by_thread.get(thread_key, ()))
            entries = [self._by_run[run_id] for run_id in run_ids if run_id in self._by_run]
        cancelled = await self._cancel_entries(entries)
        if cancelled:
            logger.info(
                "Requested local cancellation for %d run(s) on thread_key=%s",
                cancelled,
                thread_key,
            )
        return cancelled


run_registry = RunRegistry()
