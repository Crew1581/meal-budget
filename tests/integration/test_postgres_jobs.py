import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb
from test_generation_http import (
    HEADERS,
    RecordingTransport,
    async_client,
    envelope,
    example_request,
)
from test_generation_http import app_factory as app_factory
from test_generation_jobs import completion_event, finished

from meal_budget.api.generations import GENERATION_PATH, JOBS_PATH
from meal_budget.catalog import load_catalog
from meal_budget.migrate_jobs import migrate
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.services import job_store as store_module
from meal_budget.services.deepseek import DeepSeekProvider
from meal_budget.services.job_store import JobStore
from meal_budget.services.menu_generation import MenuGenerationService

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def opened(settings):
    store = await JobStore.open(settings)
    try:
        yield store
    finally:
        await store.close()
        assert store.pool.closed


async def verified_result(settings):
    async with DeepSeekProvider(settings, transport=RecordingTransport()) as provider:
        return await MenuGenerationService(provider, settings).generate(
            GenerationRequest.model_validate(example_request()), load_catalog(settings.catalog_path)
        )


@asynccontextmanager
async def blocked_claim(worker, monkeypatch, postgres_url):
    """Реальный statement timeout UPDATE, с барьерами до SQL и после ошибки."""
    entered, release, timed_out = (asyncio.Event() for _ in range(3))
    claim = worker.store.claim_next
    async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as admin:
        await admin.execute("""
            CREATE FUNCTION block_claim() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock(880008);
                RETURN NEW;
            END; $$
        """)
        await admin.execute("SELECT pg_advisory_lock(880008)")

        async def observed(now):
            entered.set()
            await release.wait()
            await admin.execute(
                "CREATE TRIGGER block_claim BEFORE UPDATE ON generation_jobs "
                "FOR EACH ROW WHEN (NEW.state = 'running') EXECUTE FUNCTION block_claim()"
            )
            try:
                return await claim(now)
            except psycopg.errors.QueryCanceled:
                timed_out.set()
                raise

        monkeypatch.setattr(worker.store, "claim_next", observed)
        try:
            yield entered, release, timed_out
        finally:
            release.set()
            await admin.execute("SELECT pg_advisory_unlock(880008)")
            await admin.execute("DROP TRIGGER IF EXISTS block_claim ON generation_jobs")


def test_claim_timeout_exposes_unavailable_and_preserves_jobs(
    pg_settings, postgres_url, app_factory, monkeypatch, caplog
):
    async def scenario():
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        version = load_catalog(pg_settings.catalog_path).catalog_version
        async with opened(pg_settings) as store:
            completed = await store.create(payload, version, 10, now)
            await store.claim_next(now)
            await store.complete(completed, await verified_result(pg_settings), now)
            failed = await store.create(payload, version, 10, now)
            await store.fail(failed, "provider_timeout", now)
        app, transport, providers = app_factory(generation_jobs_db_timeout=1)
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            store = worker.store
            async with blocked_claim(worker, monkeypatch, postgres_url) as (
                entered,
                release,
                error,
            ):
                accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                running = accepted.json()["id"]
                await asyncio.wait_for(entered.wait(), 5)
                # Состояние durable running также может остаться после неизвестного COMMIT claim.
                assert str((await JobStore.claim_next(store, now))["id"]) == running
                queued = (
                    await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                ).json()["id"]
                release.set()
                await asyncio.wait_for(worker.task, 5)
                assert error.is_set() and not worker.available and not worker.slot.busy
            # БД вновь доступна, но worker автоматически не перезапускается.
            async with store.pool.connection() as connection:
                assert (await (await connection.execute("SELECT 1 AS ok")).fetchone())["ok"] == 1
            for job_id, state in ((running, "running"), (queued, "queued")):
                response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                assert response.status_code == 503
                assert response.json()["detail"]["code"] == "worker_unavailable"
                assert (await store.get(job_id)).state == state
            for job_id, state in ((completed, "completed"), (failed, "failed")):
                response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                assert response.status_code == 200 and response.json()["state"] == state
            assert (await client.get(f"{JOBS_PATH}/{uuid4()}", headers=HEADERS)).status_code == 404
            rejected = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            assert rejected.status_code == 503
            assert rejected.json()["detail"]["code"] == "worker_unavailable"
            assert not transport.requests
        assert store.pool.closed and transport.closed and providers[0]._client.is_closed
        restarted, second_transport, _ = app_factory()
        async with async_client(restarted) as client:
            event = completion_event(restarted, monkeypatch)
            assert (await finished(restarted, queued, event)).state == "completed"
            response = await client.get(f"{JOBS_PATH}/{running}", headers=HEADERS)
            assert response.json()["error_code"] == "interrupted"
            assert len(second_transport.requests) == 1

    asyncio.run(scenario())
    events = [record for record in caplog.records if record.name.endswith("generation_jobs")]
    assert [record.getMessage() for record in events] == ["worker_unavailable"]
    assert all(record.exc_info is None and record.args == () for record in events)
    assert postgres_url not in caplog.text


def test_post_paused_in_cleanup_is_rejected_after_claim_timeout(
    pg_settings, postgres_url, app_factory, monkeypatch
):
    async def scenario():
        paused, resume = asyncio.Event(), asyncio.Event()
        app, transport, _ = app_factory(generation_jobs_db_timeout=1)
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            async with blocked_claim(worker, monkeypatch, postgres_url) as (
                entered,
                release,
                error,
            ):
                prior = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                prior_id = prior.json()["id"]
                await asyncio.wait_for(entered.wait(), 5)
                cleanup = worker.cleanup

                async def pause_cleanup():
                    await cleanup()
                    if asyncio.current_task() is post:
                        paused.set()
                        await resume.wait()

                monkeypatch.setattr(worker, "cleanup", pause_cleanup)
                post = asyncio.create_task(
                    client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                )
                try:
                    await asyncio.wait_for(paused.wait(), 5)
                    release.set()
                    await asyncio.wait_for(worker.task, 5)
                    assert error.is_set() and not worker.available
                    resume.set()
                    rejected = await asyncio.wait_for(post, 5)
                    assert rejected.status_code == 503
                    assert rejected.json()["detail"]["code"] == "worker_unavailable"
                    assert (await worker.store.get(prior_id)).state == "queued"
                    async with worker.store.pool.connection() as connection:
                        cursor = await connection.execute("SELECT id FROM generation_jobs")
                        assert [str(row["id"]) for row in await cursor.fetchall()] == [prior_id]
                    assert not transport.requests
                finally:
                    resume.set()
                    post.cancel()
                    await asyncio.gather(post, return_exceptions=True)

    asyncio.run(scenario())


def test_commit_in_progress_precedes_worker_unavailability(
    pg_settings, postgres_url, app_factory, monkeypatch
):
    async def scenario():
        committed, deliver = asyncio.Event(), asyncio.Event()
        app, transport, _ = app_factory(generation_jobs_db_timeout=1)
        async with async_client(app) as client:
            worker = app.state.generation_jobs
            async with blocked_claim(worker, monkeypatch, postgres_url) as (
                entered,
                release,
                error,
            ):
                prior = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                await asyncio.wait_for(entered.wait(), 5)
                create = worker.store.create

                async def committed_create(*args):
                    job_id = await create(*args)
                    committed.set()
                    await deliver.wait()
                    return job_id

                monkeypatch.setattr(worker.store, "create", committed_create)
                post = asyncio.create_task(
                    client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                )
                try:
                    await asyncio.wait_for(committed.wait(), 5)
                    release.set()
                    await asyncio.wait_for(error.wait(), 5)
                    # Переход unavailable ожидает завершения уже начатого admission.
                    assert not worker.task.done()
                    deliver.set()
                    response = await asyncio.wait_for(post, 5)
                    assert response.status_code == 202
                    await asyncio.wait_for(worker.task, 5)
                    for job_id in (prior.json()["id"], response.json()["id"]):
                        assert (await worker.store.get(job_id)).state == "queued"
                        status = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                        assert status.status_code == 503
                        assert status.json()["detail"]["code"] == "worker_unavailable"
                    assert not transport.requests
                finally:
                    deliver.set()
                    post.cancel()
                    await asyncio.gather(post, return_exceptions=True)

    asyncio.run(scenario())


def test_clean_migrations_repeat_apply_rollback_and_startup(postgres_url, app_factory):
    app, transport, _ = app_factory(generation_jobs_database_url=postgres_url)
    with pytest.raises(RuntimeError, match="миграции"), TestClient(app):
        pass
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT to_regclass('generation_jobs')").fetchone()[0] is None
    assert not transport.requests
    migrate(postgres_url)
    migrate(postgres_url)
    with psycopg.connect(postgres_url) as connection:
        fields = dict(
            connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'generation_jobs'"
            ).fetchall()
        )
        assert fields["id"] == "uuid"
        assert (
            fields["created_at"]
            == fields["started_at"]
            == fields["finished_at"]
            == "timestamp with time zone"
        )
        assert fields["request_json"] == fields["result_json"] == "jsonb"
        assert connection.execute(
            "SELECT migration_id FROM _yoyo_migration ORDER BY migration_id"
        ).fetchall() == [
            ("001_generation_jobs_postgresql",),
            ("002_installations_app_admission",),
        ]
    migrate(postgres_url, rollback=True)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT to_regclass('generation_jobs')").fetchone()[0] is None
    migrate(postgres_url)


def test_lifecycle_slot_and_no_connection_held_during_provider(
    pg_settings, app_factory, monkeypatch
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=envelope())

        app, transport, _ = app_factory(RecordingTransport(handler), generation_jobs_pool_size=1)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            assert accepted.status_code == 202 and accepted.json()["state"] == "queued"
            job_id = accepted.json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
            store = app.state.generation_jobs.store
            assert store.pool.get_stats()["pool_available"] == 1
            # Пул всего из одного соединения: GET и SQL доступны во время провайдера.
            response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
            assert response.json()["state"] == "running"
            async with store.pool.connection() as connection:
                cursor = await connection.execute("SELECT 1 AS value")
                assert (await cursor.fetchone())["value"] == 1
            assert (
                await client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            ).status_code == 429
            release.set()
            result = await finished(app, job_id, event)
            assert result.state == "completed" and result.result.calculation.within_budget
            assert len(transport.requests) == 1
        assert store.pool.closed and transport.closed

    asyncio.run(scenario())


def test_parallel_posts_queue_limit_and_sync_slot(pg_settings, app_factory, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if len(transport.requests) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json=envelope())

        app, transport, _ = app_factory(RecordingTransport(handler))
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            synchronous = asyncio.create_task(
                client.post(GENERATION_PATH, headers=HEADERS, json=example_request())
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                responses = await asyncio.gather(
                    *[
                        client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                        for _ in range(12)
                    ]
                )
                assert sum(response.status_code == 202 for response in responses) == 4
                assert sum(response.status_code == 429 for response in responses) == 8
                assert len(transport.requests) == 1
                ids = [
                    response.json()["id"] for response in responses if response.status_code == 202
                ]
                assert len(set(ids)) == 4
                for job_id in ids:
                    assert (await app.state.generation_jobs.get(job_id)).state == "queued"
                release.set()
                assert (await synchronous).status_code == 200
                for job_id in ids:
                    assert (await finished(app, job_id, event)).state == "completed"
                assert len(transport.requests) == 5
            finally:
                release.set()
                await asyncio.gather(synchronous, return_exceptions=True)

    asyncio.run(scenario())


def test_atomic_claim_and_concurrent_admission(pg_settings):
    async def scenario():
        payload = GenerationRequest.model_validate(example_request())
        now = datetime.now(UTC)
        async with opened(pg_settings) as store:
            # Отдельные соединения пула; admission защищен транзакционным advisory lock.
            ids = await asyncio.gather(
                *[store.create(payload, "version", 4, now) for _ in range(12)]
            )
            assert len([job_id for job_id in ids if job_id]) == 4
            claims = await asyncio.gather(*[store.claim_next(now) for _ in range(12)])
            rows = [row for row in claims if row]
            assert len(rows) == len({row["id"] for row in rows}) == 4
            assert all(row["state"] == "running" for row in rows)
            assert not await store.has_queued()

    asyncio.run(scenario())


def test_persisted_result_recovery_and_catalog_version(pg_settings, app_factory, monkeypatch):
    async def scenario():
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        version = load_catalog(pg_settings.catalog_path).catalog_version
        async with opened(pg_settings) as store:
            interrupted = await store.create(payload, version, 10, now)
            assert str((await store.claim_next(now))["id"]) == interrupted
            queued = await store.create(payload, version, 10, now)
            mismatch = await store.create(payload, "different-catalog", 10, now)
        app, transport, _ = app_factory()
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            assert (await finished(app, queued, event)).state == "completed"
            for job_id, code in (
                (interrupted, "interrupted"),
                (mismatch, "catalog_version_mismatch"),
            ):
                body = (await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)).json()
                assert body["state"] == "failed" and body["error_code"] == code
            assert len(transport.requests) == 1
        restarted, transport, _ = app_factory()
        async with async_client(restarted) as client:
            body = (await client.get(f"{JOBS_PATH}/{queued}", headers=HEADERS)).json()
            assert body["state"] == "completed"
            assert body["result"]["calculation"]["total_cost"] == 89.9
            assert not transport.requests

    asyncio.run(scenario())


def test_ttl_exact_decimal_and_active_protection(pg_settings):
    async def scenario():
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        payload.budget = Decimal("89.90")
        async with opened(pg_settings) as store:
            completed = await store.create(payload, "version", 10, now)
            row = await store.claim_next(now)
            assert row["request_json"]["budget"] == "89.90"
            result = await verified_result(pg_settings)
            await store.complete(completed, result, now)
            failed = await store.create(payload, "version", 10, now)
            await store.fail(failed, "provider_timeout", now)
            running = await store.create(payload, "version", 10, now - timedelta(days=10))
            await store.claim_next(now - timedelta(days=10))
            queued = await store.create(payload, "version", 10, now - timedelta(days=10))
            await store.cleanup(now + timedelta(seconds=86399), 86400)
            assert (await store.get(completed)).state == "completed"
            assert (await store.get(failed)).state == "failed"
            await store.cleanup(now + timedelta(seconds=86400), 86400)
            assert await store.get(completed) is None and await store.get(failed) is None
            assert (await store.get(running)).state == "running"
            assert (await store.get(queued)).state == "queued"

    asyncio.run(scenario())


def test_complete_and_admission_errors_rollback(pg_settings):
    async def scenario():
        payload = GenerationRequest.model_validate(example_request())
        now = datetime.now(UTC)
        async with opened(pg_settings) as store:
            job_id = await store.create(payload, "version", 4, now)
            await store.claim_next(now)
            async with store.pool.connection() as connection:
                await connection.execute("""
                    CREATE FUNCTION reject_write() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic write failure'; END; $$
                """)
                await connection.execute(
                    "CREATE TRIGGER reject_update BEFORE UPDATE ON generation_jobs "
                    "FOR EACH ROW EXECUTE FUNCTION reject_write()"
                )
            result = await verified_result(pg_settings)
            with pytest.raises(psycopg.DatabaseError):
                await store.complete(job_id, result, now)
            job = await store.get(job_id)
            assert job.state == "running" and job.result is None and job.finished_at is None
            async with store.pool.connection() as connection:
                await connection.execute("DROP TRIGGER reject_update ON generation_jobs")
                await connection.execute(
                    "CREATE TRIGGER reject_insert BEFORE INSERT ON generation_jobs "
                    "FOR EACH ROW EXECUTE FUNCTION reject_write()"
                )
            with pytest.raises(psycopg.DatabaseError):
                await store.create(payload, "version", 4, now)
            async with store.pool.connection() as connection:
                await connection.execute("DROP TRIGGER reject_insert ON generation_jobs")
            # Ошибка не оставила count/lock/транзакцию или частичный результат.
            assert await store.create(payload, "version", 4, now)
            await store.complete(job_id, result, now)
            assert (await store.get(job_id)).state == "completed"

    asyncio.run(scenario())


def test_schema_rejects_inconsistent_state(pg_settings):
    async def scenario():
        async with opened(pg_settings) as store:
            job_id = await store.create(
                GenerationRequest.model_validate(example_request()), "v", 4, datetime.now(UTC)
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                async with store.pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        "UPDATE generation_jobs SET state='completed', result_json=%s WHERE id=%s",
                        (Jsonb({}), UUID(job_id)),
                    )
            assert (await store.get(job_id)).state == "queued"

    asyncio.run(scenario())


def test_recovery_transaction_rolls_back_both_transitions(pg_settings):
    async def scenario():
        now = datetime.now(UTC)
        payload = GenerationRequest.model_validate(example_request())
        async with opened(pg_settings) as store:
            running = await store.create(payload, "version", 4, now)
            await store.claim_next(now)
            queued = await store.create(payload, "different-version", 4, now)
            async with store.pool.connection() as connection:
                await connection.execute("""
                    CREATE FUNCTION reject_recovery() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.error_code = 'catalog_version_mismatch' THEN
                            RAISE EXCEPTION 'synthetic recovery failure';
                        END IF;
                        RETURN NEW;
                    END; $$
                """)
                await connection.execute(
                    "CREATE TRIGGER reject_recovery BEFORE UPDATE ON generation_jobs "
                    "FOR EACH ROW EXECUTE FUNCTION reject_recovery()"
                )
            with pytest.raises(psycopg.DatabaseError):
                await store.recover("version", now)
            assert (await store.get(running)).state == "running"
            assert (await store.get(queued)).state == "queued"
            async with store.pool.connection() as connection:
                await connection.execute("DROP TRIGGER reject_recovery ON generation_jobs")
            await store.recover("version", now)
            assert (await store.get(running)).error_code == "interrupted"
            assert (await store.get(queued)).error_code == "catalog_version_mismatch"

    asyncio.run(scenario())


@pytest.mark.parametrize("unexpected_cancel", [False, True])
def test_shutdown_with_postgres_preserves_queue_and_interrupts(
    pg_settings, app_factory, monkeypatch, unexpected_cancel
):
    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        app, transport, _ = app_factory(RecordingTransport(handler))
        async with async_client(app) as client:
            running = (
                await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            ).json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
            queued = (await client.post(JOBS_PATH, headers=HEADERS, json=example_request())).json()[
                "id"
            ]
            worker = app.state.generation_jobs
            store = worker.store
            if unexpected_cancel:
                worker.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(worker.task, 5)
                assert not worker.available and not worker.slot.busy
                response = await client.get(f"{JOBS_PATH}/{queued}", headers=HEADERS)
                assert response.status_code == 503
                assert response.json()["detail"]["code"] == "worker_unavailable"
        assert store.pool.closed and transport.closed and len(transport.requests) == 1
        # До запуска нового worker очередь действительно осталась queued.
        async with opened(pg_settings) as store:
            assert (await store.get(running)).error_code == "interrupted"
            assert (await store.get(queued)).state == "queued"
        restarted, transport, _ = app_factory()
        async with async_client(restarted):
            event = completion_event(restarted, monkeypatch)
            assert (await finished(restarted, queued, event)).state == "completed"
            assert len(transport.requests) == 1

    asyncio.run(scenario())


def test_get_expired_result_is_404_on_postgres(pg_settings, app_factory, monkeypatch):
    async def scenario():
        app, _, _ = app_factory(generation_jobs_retention_seconds=60)
        async with async_client(app) as client:
            event = completion_event(app, monkeypatch)
            accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
            job_id = accepted.json()["id"]
            await finished(app, job_id, event)
            async with app.state.generation_jobs.store.pool.connection() as connection:
                await connection.execute(
                    "UPDATE generation_jobs SET finished_at=%s WHERE id=%s",
                    (datetime.now(UTC) - timedelta(seconds=61), UUID(job_id)),
                )
            response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "job_not_found"

    asyncio.run(scenario())


def test_unavailable_startup_is_safe_and_closes_pool(monkeypatch, app_factory, caplog):
    pools = []
    factory = store_module.AsyncConnectionPool

    def record(*args, **kwargs):
        pool = factory(*args, **kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(store_module, "AsyncConnectionPool", record)
    # Метод check_connection берется с фабрики, как с реального класса.
    record.check_connection = factory.check_connection
    app, transport, _ = app_factory(
        generation_jobs_database_url="postgresql://test:private-test-password@127.0.0.1:1/no_database",
        generation_jobs_db_timeout=0.1,
    )
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(RuntimeError, match="PostgreSQL") as error,
        TestClient(app),
    ):
        pass
    assert "private-test-password" not in caplog.text + str(error.value)
    assert "postgresql://" not in caplog.text + str(error.value)
    assert pools and all(pool.closed for pool in pools) and not transport.requests


def test_database_loss_during_generation_does_not_repeat(
    pg_settings, postgres_url, app_factory, monkeypatch
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        params = store_module.database_parameters(postgres_url)
        database = params["dbname"]
        # Отдельная admin-БД того же выделенного тестового контейнера.
        params["dbname"] = "meal_budget_test"
        admin = await psycopg.AsyncConnection.connect(**params, autocommit=True)

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=envelope())

        app, transport, _ = app_factory(RecordingTransport(handler), generation_jobs_db_timeout=0.2)
        try:
            async with async_client(app) as client:
                accepted = await client.post(JOBS_PATH, headers=HEADERS, json=example_request())
                job_id = accepted.json()["id"]
                await asyncio.wait_for(entered.wait(), 5)
                await admin.execute(
                    sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                        sql.Identifier(database)
                    )
                )
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                    (database,),
                )
                response = await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)
                assert response.status_code == 503 and "postgresql://" not in response.text
                release.set()
                await asyncio.wait_for(app.state.generation_jobs.task, 5)
                assert not app.state.generation_jobs.available
                assert len(transport.requests) == 1
                store = app.state.generation_jobs.store
            assert store.pool.closed
        finally:
            release.set()
            await admin.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(database))
            )
            await admin.close()
        restarted, second_transport, _ = app_factory()
        async with async_client(restarted) as client:
            body = (await client.get(f"{JOBS_PATH}/{job_id}", headers=HEADERS)).json()
            assert body["state"] == "failed" and body["error_code"] == "interrupted"
            assert not second_transport.requests

    asyncio.run(scenario())
