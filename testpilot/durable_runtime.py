"""PG-owned Worker leases and operation replay/reconciliation around nanobot."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from testpilot.artifacts import ArtifactStore
from testpilot.domain import ExecutionRecord
from testpilot.evidence import build_report, check_execution
from testpilot.recoverable_runner import RetainedRunner, UnknownDispatchError
from testpilot.runtime_contracts import RunControls
from testpilot.storage.postgres import (
    BudgetExhaustedError,
    LeaseLostError,
    Ledger,
    OperationRow,
    TaskRow,
)

DurableTaskRunner = Callable[["DurableExecutor", RunControls], Awaitable[dict[str, object]]]


class DurableExecutor:
    def __init__(self, ledger: Ledger, task: TaskRow, operation: OperationRow,
                 store: ArtifactStore, backend: RetainedRunner, lease_seconds: int = 30) -> None:
        self.ledger, self.task, self.operation = ledger, task, operation
        self.store, self.backend = store, backend
        self.task_id, self.target, self.operation_id = task.id, task.target, operation.id
        self.record = operation.execution()
        self._lock = asyncio.Lock()
        self.lease_seconds = lease_seconds

    async def execute(self) -> ExecutionRecord:
        async with self._lock:
            await self.ledger.heartbeat(self.task, self.lease_seconds)
            if self.record is not None:
                check = check_execution(self.record, self.store, self.task_id, self.target)
                if check.gaps:
                    raise UnknownDispatchError("stored execution evidence invalid")
                return self.record
            try:
                if self.operation.state == "PREPARED":
                    await self.ledger.dispatch(self.task, self.operation)
                    self.backend.note_dispatch()
                    # Fail closed if PG/lease is unavailable immediately before invoking Docker.
                    fresh = await self.ledger.heartbeat(self.task, self.lease_seconds)
                    if fresh.cancel_requested:
                        raise BudgetExhaustedError("cancel before dispatch")
                    record = await self.backend.dispatch()
                else:
                    record = await self.backend.reconcile()
            except UnknownDispatchError:
                await self.ledger.outcome(self.task, self.operation, None)
                raise
            # Artifacts were saved before this transaction. DB failure leaves an orphan, not published evidence.
            await self.ledger.outcome(self.task, self.operation, record)
            self.record = record
            return record


class WorkerPool:
    def __init__(self, ledger: Ledger, root: Path, image: str, run: DurableTaskRunner,
                 workers: int = 2, lease_seconds: int = 30) -> None:
        if not 1 <= workers <= 4 or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid worker configuration")
        self.ledger, self.root, self.image, self.run = ledger, root.resolve(), image, run
        self.workers, self.lease_seconds = workers, lease_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self.stopping = False

    async def start(self) -> None:
        await self.ledger.ready()
        for _ in range(self.workers):
            owner = "worker_" + uuid4().hex
            self._tasks.append(asyncio.create_task(self._poll(owner)))

    async def stop(self) -> None:
        self.stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _poll(self, owner: str) -> None:
        while not self.stopping:
            try:
                task = await self.ledger.claim(owner, self.lease_seconds)
                if task is not None:
                    await self._work(task)
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # PG failures stop new dispatch. The row lease or queue remains the recovery source.
                await asyncio.sleep(1)

    async def _work(self, task: TaskRow) -> None:
        current = asyncio.current_task()
        assert current is not None
        lost = False
        cancel = task.cancel_requested
        backend: RetainedRunner | None = None
        executor: DurableExecutor | None = None
        operation: OperationRow | None = None

        async def cleanup_cancel() -> None:
            try:
                record = await backend.cancel() if backend else None
                if operation:
                    await self.ledger.cancel_operation(task, operation, record)
                await self.ledger.finish(task, cancelled=True)
            except LeaseLostError:
                raise
            except Exception:
                if operation:
                    await self.ledger.outcome(task, operation, None)
                await self.ledger.finish(task, error="CANCELLATION_UNCONFIRMED")

        async def heartbeat() -> None:
            nonlocal lost, cancel
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                try:
                    fresh = await self.ledger.heartbeat(task, self.lease_seconds)
                    if fresh.cancel_requested:
                        cancel = True
                        current.cancel()
                        return
                except Exception:
                    lost = True
                    current.cancel()
                    return

        timer = asyncio.create_task(heartbeat())
        try:
            store = ArtifactStore(self.root / self.ledger.tenant_id / task.id / "artifacts")
            operation = await self.ledger.find_operation(task) if cancel else await self.ledger.operation(task)
            if operation is not None:
                backend = RetainedRunner(task, operation, store, self.image, self.root / "jobs" / self.ledger.tenant_id)
                executor = DurableExecutor(self.ledger, task, operation, store, backend, self.lease_seconds)
            if cancel:
                await cleanup_cancel()
                return
            assert executor is not None
            if operation is not None and operation.state in {"DISPATCHING", "UNKNOWN", "SUCCEEDED"}:
                # Resolve external facts first, including after model budget exhaustion.
                # This path can only replay/reconcile; it cannot create another Job.
                await executor.execute()
            remaining = (task.created_at + timedelta(seconds=120) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0 or task.model_rounds >= 4:
                raise BudgetExhaustedError("task budget exhausted")
            controls = RunControls(
                checkpoint=lambda body: self.ledger.checkpoint(task, body),
                before_model=lambda: self.ledger.reserve_round(task),
                max_iterations=4-task.model_rounds,
            )
            report = await asyncio.wait_for(self.run(executor, controls), remaining)
            fresh = await self.ledger.heartbeat(task, self.lease_seconds)
            if fresh.cancel_requested:
                cancel = True
                await cleanup_cancel()
            else:
                report["model_rounds"] = fresh.model_rounds
                report["storage_profile"] = "postgres"
                limits = TypeAdapter(list[str]).validate_python(report.get("limitations", []), strict=True)
                report["limitations"] = [line.replace("不提供持久任务恢复", "通过 PG 账本恢复和原 Job 对账，完整故障矩阵尚未覆盖") for line in limits]
                await self.ledger.finish(task, report)
        except asyncio.CancelledError:
            if not lost:
                try:
                    if cancel:
                        await cleanup_cancel()
                    else:
                        await self.ledger.release(task)
                except Exception:
                    pass  # Lease expiry or persisted CANCELLING retains the external reconciliation obligation.
            if self.stopping:
                raise
        except LeaseLostError:
            pass  # Never let an old worker publish or mutate after takeover.
        except Exception as error:
            try:
                if executor is not None:
                    operation = await self.ledger.find_operation(task)
                    record = operation.execution() if operation else None
                    report = build_report(task_id=task.id, target=task.target, record=record,
                                          store=executor.store, candidate_json=None, runner_stop_reason="interrupted")
                    code = "OPERATION_UNKNOWN" if isinstance(error, UnknownDispatchError) else "TASK_BUDGET" if isinstance(error, (BudgetExhaustedError, TimeoutError)) else "TASK_EXECUTION_UNRESOLVED"
                    await self.ledger.finish(task, report, code)
                else:
                    await self.ledger.finish(task, error="CANCELLATION_UNCONFIRMED" if cancel else "TARGET_OR_RUNTIME_INCOMPATIBLE")
            except Exception:
                pass
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
