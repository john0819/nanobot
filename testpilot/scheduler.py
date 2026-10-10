"""Separate leased Job poller. No model calls, no invocation of unresolved writes."""

import asyncio
from pathlib import Path
from uuid import uuid4

from testpilot.domain import PendingExecution
from testpilot.memory import MemoryRepository
from testpilot.recoverable_runner import RetainedRunner, UnknownDispatchError
from testpilot.storage.postgres import LeaseLostError, Ledger, TaskRow
from testpilot.storage_contracts import ArtifactFactory, local_artifacts


class JobScheduler:
    def __init__(self, ledger: Ledger, root: Path, image: str,
                 lease_seconds: int = 30, poll_delay: float = 5,
                 artifacts: ArtifactFactory = local_artifacts) -> None:
        if not 0.01 <= poll_delay <= 30 or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid scheduler configuration")
        self.ledger, self.root, self.image = ledger, root.resolve(), image
        self.lease_seconds, self.poll_delay = lease_seconds, poll_delay
        self.artifacts = artifacts
        self.worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.ledger.ready()
        self.worker = asyncio.create_task(self._loop("scheduler_" + uuid4().hex))

    async def stop(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def _loop(self, owner: str) -> None:
        next_sweep = 0.0
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now >= next_sweep:
                    await self.ledger.expire_approvals()
                    await MemoryRepository(self.ledger).expire()
                    await self.ledger.expire_paused()
                    next_sweep = now+1
                task = await self.ledger.claim(owner, self.lease_seconds, kind="external")
                if task is not None:
                    await self.reconcile(task)
                else:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)  # Durable lease permits another scheduler to take over.

    async def reconcile(self, task: TaskRow) -> None:
        operation = None
        current = asyncio.current_task()
        assert current is not None
        lost = False

        async def heartbeat() -> None:
            nonlocal lost
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                try:
                    await self.ledger.heartbeat(task, self.lease_seconds)
                except Exception:
                    lost = True
                    current.cancel()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            operation = await self.ledger.find_operation(task)
            if operation is None:
                if task.cancel_requested:
                    await self.ledger.finish(task, cancelled=True)
                else:
                    await self.ledger.finish(task, error="JOB_HANDLE_MISSING")
                return
            store = self.artifacts(self.root, self.ledger.tenant_id, task.id)
            backend = RetainedRunner(task, operation, store, self.image, self.root / "jobs" / self.ledger.tenant_id)
            polls, expired = await self.ledger.job_poll(task)
            if task.cancel_requested or expired:
                try:
                    record = await backend.cancel()
                    await self.ledger.cancel_operation(task, operation, record)
                    await self.ledger.finish(task, cancelled=task.cancel_requested,
                                             error=None if task.cancel_requested else "JOB_DEADLINE")
                except UnknownDispatchError:
                    await self.ledger.outcome(task, operation, None)
                    await self.ledger.finish(task, error="CANCELLATION_UNCONFIRMED")
                return
            if operation.result is not None:
                await self.ledger.requeue(task)
                return
            observation = await backend.poll()  # Exactly one query, never dispatch()/start().
            if isinstance(observation, PendingExecution):
                delay = min(30, self.poll_delay * (1 if polls == 0 else 3 if polls == 1 else 6))
                await self.ledger.park(task, operation, observation, delay)
            else:
                await self.ledger.outcome(task, operation, observation)
                await self.ledger.requeue(task)
        except asyncio.CancelledError:
            if lost:
                return  # Expired ownership must not terminate the scheduler's future polling loop.
            try:
                await self.ledger.release(task)
            except Exception:
                pass
            raise
        except LeaseLostError:
            pass
        except Exception:
            try:
                if operation:
                    await self.ledger.outcome(task, operation, None)
                await self.ledger.finish(task, error="OPERATION_UNKNOWN")
            except Exception:
                pass
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
