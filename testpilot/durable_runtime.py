"""PG-owned Worker leases and operation replay/reconciliation around nanobot."""

import asyncio
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from testpilot.artifacts import ArtifactStore, ArtifactUnavailableError
from testpilot.context import ContextBlock, ContextBudgetError, select
from testpilot.domain import ExecutionRecord, PendingExecution
from testpilot.evidence import build_report, check_execution
from testpilot.knowledge import KnowledgeUnavailableError, MCPKnowledgeClient, evidence
from testpilot.memory import MemoryRepository, verify_source
from testpilot.progress import ProgressStalledError
from testpilot.recoverable_runner import RetainedRunner, UnknownDispatchError
from testpilot.runtime_contracts import RunControls, RuntimeYieldError
from testpilot.storage.postgres import (
    BudgetExhaustedError,
    LeaseLostError,
    Ledger,
    OperationRow,
    TaskRow,
)
from testpilot.storage_contracts import ArtifactFactory, local_artifacts

DurableTaskRunner = Callable[["DurableExecutor", RunControls], Awaitable[dict[str, object]]]


class DurableExecutor:
    def __init__(self, ledger: Ledger, task: TaskRow, operation: OperationRow,
                 store: ArtifactStore, backend: RetainedRunner, lease_seconds: int = 30,
                 asynchronous: bool = False) -> None:
        self.ledger, self.task, self.operation = ledger, task, operation
        self.store, self.backend = store, backend
        self.task_id, self.target, self.operation_id = task.id, task.target, operation.id
        self.record = operation.execution()
        self._lock = asyncio.Lock()
        self.lease_seconds = lease_seconds
        self.asynchronous = asynchronous
        self.pending: PendingExecution | None = None

    async def execute(self) -> ExecutionRecord | PendingExecution:
        async with self._lock:
            await self.ledger.heartbeat(self.task, self.lease_seconds)
            if self.record is not None:
                check = await asyncio.to_thread(check_execution, self.record, self.store, self.task_id, self.target)
                if check.gaps:
                    raise UnknownDispatchError("stored execution evidence invalid")
                return self.record
            if self.pending is not None:
                return self.pending
            try:
                if self.operation.state == "PREPARED":
                    if self.task.knowledge_required and not await self.ledger.knowledge_ids(self.task):
                        raise KnowledgeUnavailableError("Required authorized knowledge missing")
                    await self.ledger.dispatch(self.task, self.operation)
                    self.backend.note_dispatch()
                    # Fail closed if PG/lease is unavailable immediately before invoking Docker.
                    fresh = await self.ledger.heartbeat(self.task, self.lease_seconds)
                    if fresh.cancel_requested:
                        raise BudgetExhaustedError("cancel before dispatch")
                    record = await self.backend.submit() if self.asynchronous else await self.backend.dispatch()
                else:
                    record = await self.backend.poll() if self.asynchronous else await self.backend.reconcile()
            except UnknownDispatchError:
                await self.ledger.outcome(self.task, self.operation, None)
                raise
            if isinstance(record, PendingExecution):
                self.pending = record
                return record
            # Artifacts were saved before this transaction. DB failure leaves an orphan, not published evidence.
            await self.ledger.outcome(self.task, self.operation, record)
            self.record = record
            return record


class WorkerPool:
    def __init__(self, ledger: Ledger, root: Path, image: str, run: DurableTaskRunner,
                 workers: int = 2, lease_seconds: int = 30, poll_delay: float = 5,
                 artifacts: ArtifactFactory = local_artifacts, knowledge: MCPKnowledgeClient | None = None) -> None:
        if not 1 <= workers <= 4 or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid worker configuration")
        self.ledger, self.root, self.image, self.run = ledger, root.resolve(), image, run
        self.workers, self.lease_seconds = workers, lease_seconds
        self.poll_delay = poll_delay
        self.artifacts = artifacts
        self.knowledge = knowledge
        self._tasks: list[asyncio.Task[None]] = []
        self.stopping = False
        self._active: dict[str, asyncio.Task[None]] = {}

    def interrupt(self, task_id: str) -> None:
        task = self._active.get(task_id)
        if task is not None:
            task.cancel()  # DB fencing is authoritative; this only accelerates local pause.

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
                task = await self.ledger.claim(owner, self.lease_seconds, kind="agent")
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
        self._active[task.id] = current
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
            store = self.artifacts(self.root, self.ledger.tenant_id, task.id)
            operation = await self.ledger.find_operation(task) if cancel else await self.ledger.operation(task)
            if operation is not None:
                backend = RetainedRunner(task, operation, store, self.image, self.root / "jobs" / self.ledger.tenant_id)
                executor = DurableExecutor(self.ledger, task, operation, store, backend, self.lease_seconds, asynchronous=True)
            if cancel:
                await cleanup_cancel()
                return
            assert executor is not None
            async def search(query: str):
                if self.knowledge is None:
                    raise KnowledgeUnavailableError("Knowledge connection not configured")
                result = await self.knowledge.search(query, task.tenant_id, task.actor_id)
                content_hash = await asyncio.to_thread(store.put, result.model_dump_json().encode())
                await self.ledger.save_knowledge(task, evidence(result, content_hash))
                return result.model_copy(update={"artifact_hash": content_hash})
            if operation is not None and operation.state in {"DISPATCHING", "PENDING", "UNKNOWN", "SUCCEEDED"}:
                # Resolve external facts first, including after model budget exhaustion.
                # This path can only replay/reconcile; it cannot create another Job.
                observation = await executor.execute()
                if isinstance(observation, PendingExecution):
                    raise RuntimeYieldError(observation)
            remaining = ((task.activated_at or task.created_at) + timedelta(seconds=120) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0 or task.model_rounds >= task.model_limit:
                raise BudgetExhaustedError("task budget exhausted")
            latest_plan = await self.ledger.latest_plan(task)
            context_blocks = (ContextBlock(block_id="task", kind="TASK", text=json.dumps({
                "task_id": task.id, "run_id": task.run_id, "goal": task.goal, "target": task.target.model_dump(),
                "knowledge_required": task.knowledge_required, "known_knowledge_refs": await self.ledger.knowledge_ids(task),
                "allowed_actions": ["run_gateway_fixture", "search_knowledge", "inspect_result", "publish_report"]}),
                pinned=True, trust_level="SERVER_FACT", priority=100),
                ContextBlock(block_id="plan", kind="PLAN", text=json.dumps(latest_plan), priority=50))
            projection, _ = select(context_blocks, 4000)
            async def artifact_allowed(content_hash: str) -> bool:
                return content_hash in await self.ledger.knowledge_artifacts(task)

            async def inputs() -> list[dict[str, object]]:
                return [{"client_request_id": note["client_request_id"], "text": note["text"]}
                        for note in await self.ledger.inputs(task)]

            async def memory() -> list[dict[str, object]]:
                repository = MemoryRepository(self.ledger)
                entries = await repository.retrieve(task)
                verified: list[dict[str, object]] = []
                for entry in entries:
                    item = await repository.get(str(entry["memory_id"]), task.actor_id)
                    if item is not None and item.status == "CONFIRMED" and item.version == entry["version"]:
                        try:
                            await verify_source(self.ledger, self.root, self.artifacts, item)
                        except (ValueError, OSError, ArtifactUnavailableError):
                            continue  # Missing/corrupted evidence never becomes active memory.
                        current_item = await repository.get(item.id, task.actor_id)
                        if current_item is not None and current_item.status == "CONFIRMED" and current_item.version == item.version:
                            verified.append(entry)
                await self.ledger.checkpoint(task, {"phase": "memory_read", "refs": [{"memory_id": entry["memory_id"], "version": entry["version"]} for entry in verified]})
                return verified

            controls = RunControls(
                checkpoint=lambda body: self.ledger.checkpoint(task, body),
                before_model=lambda: self.ledger.reserve_round(task),
                max_iterations=task.model_limit-task.model_rounds,
                progress=lambda fingerprints: self.ledger.progress(task, fingerprints),
                plan=lambda plan: self.ledger.save_plan(task, plan),
                task_context=projection,
                enable_planning=task.goal != "Validate gateway fixture" or task.knowledge_required,
                knowledge=search if self.knowledge else None,
                artifact_allowed=artifact_allowed,
                memory=memory,
                before_action=lambda: self.ledger.guard_action(task),
                inputs=inputs,
                mark_inputs=lambda ids: self.ledger.consume_inputs(task, ids),
            )
            report = await asyncio.wait_for(self.run(executor, controls), remaining)
            fresh = await self.ledger.heartbeat(task, self.lease_seconds)
            if fresh.cancel_requested:
                cancel = True
                await cleanup_cancel()
            else:
                report["model_rounds"] = fresh.model_rounds
                report["storage_profile"] = "postgres"
                report["knowledge_evidence_ids"] = await self.ledger.knowledge_ids(task)
                refs = TypeAdapter(list[str]).validate_python(report.get("artifact_refs", []), strict=True)
                report["artifact_refs"] = refs + await self.ledger.knowledge_artifacts(task)
                limits = TypeAdapter(list[str]).validate_python(report.get("limitations", []), strict=True)
                report["limitations"] = [line.replace("不提供持久任务恢复", "通过 PG 账本恢复和原 Job 对账，完整故障矩阵尚未覆盖") for line in limits]
                if report["knowledge_evidence_ids"]:
                    report["limitations"] = [line.replace("未验证真实 LLM 自主规划效果，RAG 本次未参与执行证据判定。", "知识库提供版本化背景证据；执行结论仍由真实 JUnit 和固定断言决定，自主规划效果需批量评测。") for line in report["limitations"]]
                await self.ledger.finish(task, report)
        except RuntimeYieldError as signal:
            if operation is not None:
                await self.ledger.park(task, operation, signal.pending, self.poll_delay)
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
            # Diagnostic code locations only: exception text may contain tool inputs or credentials.
            logging.getLogger(__name__).error("TestPilot failure task=%s type=%s trace=%s", task.id,
                                              type(error).__name__, "".join(traceback.format_tb(error.__traceback__)))
            try:
                if executor is not None:
                    operation = await self.ledger.find_operation(task)
                    record = operation.execution() if operation else None
                    try:
                        report = build_report(task_id=task.id, target=task.target, record=record,
                                              store=executor.store, candidate_json=None, runner_stop_reason="interrupted")
                    except ArtifactUnavailableError:
                        # Cannot verify remote evidence; publish no test counts and still terminate safely.
                        report = build_report(task_id=task.id, target=task.target, record=None,
                                              store=executor.store, candidate_json=None, runner_stop_reason="interrupted")
                    code = "ARTIFACT_UNAVAILABLE" if isinstance(error, ArtifactUnavailableError) else "KNOWLEDGE_UNAVAILABLE" if isinstance(error, KnowledgeUnavailableError) else "CONTEXT_BUDGET" if isinstance(error, ContextBudgetError) else "PROGRESS_STALLED" if isinstance(error, ProgressStalledError) else "OPERATION_UNKNOWN" if isinstance(error, UnknownDispatchError) else "TASK_BUDGET" if isinstance(error, (BudgetExhaustedError, TimeoutError)) else "TASK_EXECUTION_UNRESOLVED"
                    await self.ledger.finish(task, report, code)
                else:
                    await self.ledger.finish(task, error="CANCELLATION_UNCONFIRMED" if cancel else "TARGET_OR_RUNTIME_INCOMPATIBLE")
            except Exception:
                pass
        finally:
            self._active.pop(task.id, None)
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
