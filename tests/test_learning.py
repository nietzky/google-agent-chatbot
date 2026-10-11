import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chatbot.learning import AutomaticMemory, Extraction, Fact, learning_status, redact
from chatbot.memory import MemoryStore


def fact(**changes):
    return Fact(
        **{
            "category": "application",
            "subject": "billing-api",
            "attribute": "runtime",
            "value": "Billing API uses Python 3.12.",
            "evidence": "Our billing API uses Python 3.12",
            "product_id": None,
            "existing_key": None,
            "confidence": 0.99,
            **changes,
        }
    )


async def setup_worker(tmp_path):
    settings = SimpleNamespace(
        memory_db=tmp_path / "memory.db", auto_memory_enabled=True, memory_extraction_model="test"
    )
    sessions = SimpleNamespace(get_session=AsyncMock(return_value=None))
    worker = AutomaticMemory(settings, None, sessions)
    worker.memory = await MemoryStore.open(settings.memory_db)
    await worker.memory.db.execute("INSERT INTO memory_learning_state(id,enabled) VALUES(1,1)")
    return worker


async def enqueue(
    worker,
    question="Our billing API uses Python 3.12",
    user="alice",
    session="one",
    status="completed",
):
    identifier = await worker.memory.begin_interaction(user, session, question)
    await worker.memory.finish_interaction(identifier, status)
    return identifier


async def test_successful_turn_queues_once_persists_and_corrects(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        identifier = await enqueue(w)
        await w.memory.finish_interaction(identifier, "completed")
        w.extract = AsyncMock(return_value=Extraction(facts=[fact()]))
        assert await w.process_one()
        assert not await w.process_one()
        rows = await w.memory.list("alice")
        assert len(rows) == 1 and rows[0]["origin"] == "automatic"
        assert rows[0]["source_interaction_id"] == identifier
        assert rows[0]["subject"] == "billing-api"
        assert await w.memory.list("bob") == []
        # Correction replaces the same app attribute instead of accumulating stale facts.
        await enqueue(w, "Billing API now uses Python 3.13", session="two")
        w.extract.return_value = Extraction(
            facts=[
                fact(
                    value="Billing API uses Python 3.13.",
                    evidence="Billing API now uses Python 3.13",
                    existing_key=rows[0]["key"],
                )
            ]
        )
        await w.process_one()
        rows = await w.memory.list("alice")
        assert len(rows) == 1 and "3.13" in rows[0]["value"]
        assert await learning_status(w.memory, "alice") == {"done": 2}
    finally:
        await w.memory.close()
    reopened = await MemoryStore.open(tmp_path / "memory.db")
    try:
        assert "3.13" in (await reopened.list("alice"))[0]["value"]
    finally:
        await reopened.close()


async def test_failure_cancel_and_disabled_learning_do_not_queue(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        await enqueue(w, status="failed")
        await enqueue(w, status="cancelled")
        await w.memory.db.execute("UPDATE memory_learning_state SET enabled=0")
        await enqueue(w)
        assert not await w.process_one()
    finally:
        await w.memory.close()


async def test_candidate_validation_rejects_invented_quotes_secrets_and_low_confidence(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        await enqueue(w)
        w.extract = AsyncMock(
            return_value=Extraction(
                facts=[
                    fact(evidence="Our app uses Java"),
                    fact(confidence=0.4),
                    fact(value="password=hunter2"),
                    fact(product_id="invented"),
                    fact(existing_key="someone-elses-memory"),
                ]
            )
        )
        await w.process_one()
        assert await w.memory.list("alice") == []
        assert await learning_status(w.memory, "alice") == {"done": 1}
    finally:
        await w.memory.close()


async def test_forget_and_delete_during_extraction_prevent_resurrection(tmp_path):
    w = await setup_worker(tmp_path)
    other = await MemoryStore.open(tmp_path / "memory.db")
    try:
        await enqueue(w)
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_):
            entered.set()
            await release.wait()
            return Extraction(facts=[fact()])

        w.extract = delayed
        task = asyncio.create_task(w.process_one())
        await entered.wait()
        await other.forget("alice", "application:billing-api:runtime")
        release.set()
        await task
        assert await other.list("alice") == []
        await enqueue(w, session="two")
        entered.clear()
        release.clear()
        task = asyncio.create_task(w.process_one())
        await entered.wait()
        await other.delete_session_history("alice", "two")
        release.set()
        await task
        assert await other.list("alice") == []
    finally:
        await other.close()
        await w.memory.close()


async def test_crash_recovery_retries_and_expiry(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        await enqueue(w)
        # An expired lease from a crashed process is recovered.
        await w.memory.db.execute("UPDATE memory_jobs SET status='running',available_at=0")
        w.extract = AsyncMock(side_effect=TimeoutError())
        for _ in range(3):
            await w.memory.db.execute("UPDATE memory_jobs SET available_at=0")
            assert await w.process_one()
        assert await learning_status(w.memory, "alice") == {"failed": 1}
        assert not await w.process_one()
        await enqueue(w, session="two")
        w.extract = AsyncMock(return_value=Extraction(facts=[fact()]))
        await w.process_one()
        await w.memory.db.execute("UPDATE memories SET expires_at='2000-01-01T00:00:00Z'")
        assert await w.memory.list("alice") == []
    finally:
        await w.memory.close()


async def test_structured_request_redacts_and_opt_out_skips_model(tmp_path):
    w = await setup_worker(tmp_path)
    parse = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=Extraction(facts=[])))]
        )
    )
    w.client = SimpleNamespace(
        with_options=lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))
        )
    )
    try:
        await enqueue(w, "Our app uses Python. password=hunter2")
        await w.process_one()
        request = json.loads(parse.call_args.kwargs["messages"][1]["content"])
        assert "hunter2" not in str(request) and "[REDACTED]" in str(request)
        await enqueue(w, "Do not remember my app's configuration")
        await w.process_one()
        assert parse.await_count == 1
        assert "private" not in redact("postgres://admin:private@db.example/app")
        assert "hunter2" not in redact('{"password": "hunter2"}')
        assert "abcdefghijk" not in redact("Authorization: Bearer abcdefghijk")
    finally:
        await w.memory.close()


async def test_old_job_cannot_overwrite_explicit_newer_memory(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        await enqueue(w)
        await w.memory.save(
            "alice", "application:billing-api:runtime", "Python 3.14", "takeaway", "Now Python 3.14"
        )
        # Simulate a delayed, already-extracted result predating the explicit correction.
        await w.memory.db.execute("UPDATE interactions SET created_at='2000-01-01T00:00:00Z'")
        w.extract = AsyncMock(return_value=Extraction(facts=[fact()]))
        await w.process_one()
        assert (await w.memory.list("alice"))[0]["value"] == "Python 3.14"
    finally:
        await w.memory.close()


async def test_conversation_delete_removes_derived_fact_but_keeps_explicit_preferences(tmp_path):
    w = await setup_worker(tmp_path)
    try:
        await enqueue(w)
        w.extract = AsyncMock(return_value=Extraction(facts=[fact()]))
        await w.process_one()
        await w.memory.save("alice", "response_style", "brief", "preference", "Keep it brief")
        await w.memory.delete_session_history("alice", "one")
        rows = await w.memory.list("alice")
        assert len(rows) == 1 and rows[0]["key"] == "response_style"
        assert await learning_status(w.memory, "alice") == {}
    finally:
        await w.memory.close()
