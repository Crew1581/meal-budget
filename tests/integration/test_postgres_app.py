import asyncio
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_app_jobs import installation
from test_generation_http import HEADERS, RecordingTransport, async_client, example_request
from test_generation_http import app_factory as app_factory
from test_generation_jobs import completion_event, finished
from test_postgres_jobs import blocked_claim, opened
from yoyo import get_backend, read_migrations

from meal_budget.api.generations import GENERATION_PATH, JOBS_PATH
from meal_budget.core.config import PROJECT_ROOT
from meal_budget.migrate_jobs import migrate
from meal_budget.schemas.generation import GenerationRequest
from meal_budget.services.app_admission import (
    APP_JOBS_PATH,
    AdmissionError,
    token_hash,
)
from meal_budget.services.job_store import database_parameters

pytestmark = pytest.mark.postgres


async def new_owner(store, settings):
    return await store.register_installation(token_hash("test-token-" + uuid4().hex), settings)


async def submit(store, settings, owner, key="same", version="v", available=True):
    return await store.create_app(
        GenerationRequest.model_validate(example_request()),
        version,
        owner,
        key,
        settings,
        available,
    )


async def counts(store):
    async with store.pool.connection() as connection:
        result = {}
        for table in ("generation_jobs", "app_daily_usage", "installation_daily_usage"):
            cursor = await connection.execute(f"SELECT * FROM {table}")
            result[table] = await cursor.fetchall()
        return result


def test_issued_token_hash_only_and_auth_isolation(pg_settings, app_factory, caplog):
    async def scenario():
        app, transport, _ = app_factory(generation_app_enabled=True)
        async with async_client(app) as client:
            issued, headers = await installation(client)
            store = app.state.generation_jobs.store
            async with store.pool.connection() as connection:
                rows = await (await connection.execute("SELECT * FROM installations")).fetchall()
                assert len(rows) == 1
                row = rows[0]
                assert set(row) == {"id", "created_at", "token_hash"}
                assert (
                    row["token_hash"] == token_hash(issued["token"])
                    and len(row["token_hash"]) == 32
                )
                assert issued["token"] not in str(row)
            for bad in ({}, HEADERS, {"Authorization": "Bearer mbi_" + "a" * 43}):
                for method, path in (
                    ("POST", APP_JOBS_PATH),
                    ("GET", APP_JOBS_PATH + "/" + str(uuid4())),
                ):
                    response = await client.request(method, path, headers=bad)
                    assert response.status_code == 401
                    assert issued["token"] not in response.text
            for path in (GENERATION_PATH, JOBS_PATH):
                assert (
                    await client.post(path, headers=headers, json=example_request())
                ).status_code == 401
            assert not transport.requests
            assert issued["token"] not in caplog.text

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


def test_parallel_replays_use_one_record_and_one_quota(pg_settings):
    async def scenario():
        async with opened(pg_settings) as store:
            owner = await new_owner(store, pg_settings)
            # Без процессного lock: реальные параллельные транзакции/соединения.
            replies = await asyncio.gather(*[submit(store, pg_settings, owner) for _ in range(12)])
            assert len({reply[0] for reply in replies}) == 1
            assert sum(not replay for _, replay in replies) == 1
            snapshot = await counts(store)
            assert len(snapshot["generation_jobs"]) == 1
            assert snapshot["app_daily_usage"][0]["accepted"] == 1
            assert snapshot["installation_daily_usage"][0]["accepted"] == 1
            other = await new_owner(store, pg_settings)
            other_id, replay = await submit(store, pg_settings, other)
            assert other_id != replies[0][0] and not replay
            changed = GenerationRequest.model_validate({**example_request(), "wishes": "other"})
            with pytest.raises(AdmissionError, match="idempotency_conflict"):
                await store.create_app(changed, "v", owner, "same", pg_settings, True)
            assert (await counts(store))["app_daily_usage"][0]["accepted"] == 2
            case_sensitive_id, replay = await submit(store, pg_settings, owner, key="Same")
            assert case_sensitive_id != replies[0][0] and not replay

    asyncio.run(scenario())


@pytest.mark.parametrize("global_quota", [False, True])
def test_concurrent_quota_exhaustion(pg_settings, global_quota):
    settings = pg_settings.model_copy(
        update={
            "generation_jobs_queue_limit": 100,
            "generation_app_daily_installation_limit": 100 if global_quota else 3,
            "generation_app_daily_global_limit": 3 if global_quota else 100,
        }
    )

    async def scenario():
        async with opened(settings) as store:
            owners = [await new_owner(store, settings) for _ in range(2 if global_quota else 1)]
            replies = await asyncio.gather(
                *[
                    submit(store, settings, owners[index % len(owners)], str(index))
                    for index in range(12)
                ],
                return_exceptions=True,
            )
            accepted = [reply for reply in replies if isinstance(reply, tuple)]
            rejected = [reply for reply in replies if isinstance(reply, AdmissionError)]
            assert len(accepted) == 3 and len(rejected) == 9
            expected = (
                "app_daily_quota_exceeded"
                if global_quota
                else "installation_daily_job_quota_exceeded"
            )
            assert all(
                error.code == expected and 1 <= error.retry_after <= 86400 for error in rejected
            )
            snapshot = await counts(store)
            assert len(snapshot["generation_jobs"]) == 3
            assert snapshot["app_daily_usage"][0]["accepted"] == 3
            assert sum(row["accepted"] for row in snapshot["installation_daily_usage"]) == 3
            if global_quota:
                fresh_owner = await new_owner(store, settings)
                with pytest.raises(AdmissionError, match="app_daily_quota_exceeded"):
                    await submit(store, settings, fresh_owner)
                assert (await counts(store))["app_daily_usage"][0]["accepted"] == 3

    asyncio.run(scenario())


def test_catalog_switch_worker_failure_and_owner_first(pg_settings, app_factory, monkeypatch):
    async def scenario():
        app, transport, _ = app_factory(generation_app_enabled=True, generation_jobs_db_timeout=1)
        async with async_client(app) as client:
            issued, headers = await installation(client)
            _, other = await installation(client)
            worker = app.state.generation_jobs
            async with blocked_claim(
                worker, monkeypatch, pg_settings.generation_jobs_database_url.get_secret_value()
            ) as (entered, release, timed_out):
                accepted = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
                assert accepted.status_code == 202
                job_id = accepted.json()["id"]
                await asyncio.wait_for(entered.wait(), 5)
                version = worker.catalog.catalog_version
                worker.catalog = worker.catalog.model_copy(
                    update={"catalog_version": "new-version"}
                )
                release.set()
                await asyncio.wait_for(worker.task, 5)
                assert timed_out.is_set() and not worker.available
            # Старое подтверждение доступно даже без worker и с другим каталогом.
            again = await client.post(
                APP_JOBS_PATH, headers=headers, json=dict(reversed(list(example_request().items())))
            )
            assert again.status_code == 202 and again.json() == accepted.json()
            assert (await worker.store.get(job_id)).catalog_version == version
            response = await client.get(APP_JOBS_PATH + "/" + job_id, headers=headers)
            assert (
                response.status_code == 503
                and response.json()["detail"]["code"] == "worker_unavailable"
            )
            foreign = await client.get(APP_JOBS_PATH + "/" + job_id, headers=other)
            unknown = await client.get(APP_JOBS_PATH + "/" + str(uuid4()), headers=other)
            assert (
                foreign.status_code == unknown.status_code == 404
                and foreign.json() == unknown.json()
            )
            assert (
                await client.post(
                    APP_JOBS_PATH,
                    headers={**headers, "Idempotency-Key": "new"},
                    json=example_request(),
                )
            ).status_code == 503
            assert (await counts(worker.store))["app_daily_usage"][0]["accepted"] == 1
            assert not transport.requests
        # Recovery не удаляет привязку владельца/ключа и не генерирует по новой версии.
        restarted, new_transport, _ = app_factory(generation_app_enabled=True)
        async with async_client(restarted) as client:
            event = completion_event(restarted, monkeypatch)
            await finished(restarted, job_id, event)
            response = await client.get(APP_JOBS_PATH + "/" + job_id, headers=headers)
            assert response.status_code == 200 and response.json()["state"] == "completed"
            assert issued["token"] not in response.text
            assert len(new_transport.requests) == 1

    asyncio.run(scenario())


def test_ttl_key_expiry_active_protection_and_quota_survives(pg_settings):
    settings = pg_settings.model_copy(
        update={
            "generation_jobs_retention_seconds": 60,
            "generation_app_daily_installation_limit": 2,
        }
    )

    async def scenario():
        async with opened(settings) as store:
            owner = await new_owner(store, settings)
            job_id, _ = await submit(store, settings, owner)
            now = datetime.now(UTC)
            # Старое активное задание не теряет ключ, даже через много суток.
            async with store.pool.connection() as connection:
                await connection.execute(
                    "UPDATE generation_jobs SET created_at = %s WHERE id = %s",
                    (now - timedelta(days=10), UUID(job_id)),
                )
            await store.cleanup(now, 60)
            assert await submit(store, settings, owner, available=False) == (job_id, True)
            await store.fail(job_id, "interrupted", now - timedelta(seconds=61))
            replacement, replay = await submit(store, settings, owner, version="new")
            assert replacement != job_id and not replay
            assert await store.get(job_id) is None
            assert (await counts(store))["app_daily_usage"][0]["accepted"] == 2
            await store.fail(replacement, "provider_timeout", now - timedelta(seconds=61))
            await store.cleanup(now, 60)
            with pytest.raises(AdmissionError, match="installation_daily_job_quota_exceeded"):
                await submit(store, settings, owner)
            assert not (await counts(store))["generation_jobs"]
            # Следующие UTC-сутки: старые счётчики больше не ограничивают admission.
            async with store.pool.connection() as connection:
                await connection.execute("UPDATE app_daily_usage SET day = day - 1")
                await connection.execute("UPDATE installation_daily_usage SET day = day - 1")
            assert not (await submit(store, settings, owner))[1]
            snapshot = await counts(store)
            assert (
                len(snapshot["app_daily_usage"]) == len(snapshot["installation_daily_usage"]) == 1
            )
            assert snapshot["app_daily_usage"][0]["accepted"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_admission_failure_or_cancel_rolls_back_counters(pg_settings, monkeypatch, cancel):
    async def scenario():
        async with opened(pg_settings) as store:
            owner = await new_owner(store, pg_settings)
            execute = psycopg.AsyncConnection.execute
            entered = asyncio.Event()
            if not cancel:
                async with store.pool.connection() as connection:
                    await connection.execute("""
                        CREATE FUNCTION reject_app_insert() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'private-test-error'; END; $$
                    """)
                    await connection.execute(
                        "CREATE TRIGGER reject_app_insert BEFORE INSERT ON generation_jobs "
                        "FOR EACH ROW EXECUTE FUNCTION reject_app_insert()"
                    )

            async def intercepted(connection, query, *args, **kwargs):
                if isinstance(query, str) and query.startswith("INSERT INTO generation_jobs"):
                    entered.set()
                    if cancel:
                        await asyncio.Event().wait()
                return await execute(connection, query, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", intercepted)
                task = asyncio.create_task(submit(store, pg_settings, owner))
                await asyncio.wait_for(entered.wait(), 5)
                if cancel:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError if cancel else psycopg.DatabaseError):
                    await task
            if not cancel:
                async with store.pool.connection() as connection:
                    await connection.execute("DROP TRIGGER reject_app_insert ON generation_jobs")
            snapshot = await counts(store)
            assert all(not rows for rows in snapshot.values())
            assert not (await submit(store, pg_settings, owner))[1]
            assert (await counts(store))["app_daily_usage"][0]["accepted"] == 1

    asyncio.run(scenario())


def test_terminal_commit_between_cleanup_and_replay_is_not_confirmed(pg_settings, monkeypatch):
    settings = pg_settings.model_copy(update={"generation_jobs_retention_seconds": 60})

    async def scenario():
        async with opened(settings) as store:
            owner = await new_owner(store, settings)
            old_id, _ = await submit(store, settings, owner)
            entered, release = asyncio.Event(), asyncio.Event()
            execute = psycopg.AsyncConnection.execute

            async def paused(connection, query, *args, **kwargs):
                if isinstance(query, str) and query.startswith("SELECT id, request_hash"):
                    entered.set()
                    await release.wait()
                return await execute(connection, query, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", paused)
                task = asyncio.create_task(submit(store, settings, owner))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    await store.fail(
                        old_id, "interrupted", datetime.now(UTC) - timedelta(seconds=61)
                    )
                    release.set()
                    new_id, replay = await asyncio.wait_for(task, 5)
                    assert not replay and new_id != old_id
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
            assert await store.get(old_id) is None
            assert (await counts(store))["app_daily_usage"][0]["accepted"] == 2

    asyncio.run(scenario())


def test_running_interruption_keeps_owner_key_and_quota(pg_settings, app_factory):
    async def scenario():
        entered = asyncio.Event()

        async def blocked(request):
            entered.set()
            await asyncio.Event().wait()

        app, transport, _ = app_factory(
            RecordingTransport(blocked),
            generation_app_enabled=True,
            generation_app_daily_global_limit=1,
        )
        async with async_client(app) as client:
            _, headers = await installation(client)
            accepted = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            job_id = accepted.json()["id"]
            await asyncio.wait_for(entered.wait(), 5)
        assert transport.closed and len(transport.requests) == 1
        restarted, second_transport, _ = app_factory(
            generation_app_enabled=True,
            generation_app_daily_global_limit=1,
        )
        async with async_client(restarted) as client:
            response = await client.get(APP_JOBS_PATH + "/" + job_id, headers=headers)
            assert response.json()["state"] == "failed"
            assert response.json()["error_code"] == "interrupted"
            replay = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
            assert replay.status_code == 202 and replay.json() == accepted.json()
            limited = await client.post(
                APP_JOBS_PATH,
                headers={**headers, "Idempotency-Key": "new"},
                json=example_request(),
            )
            assert limited.status_code == 429
            assert limited.json()["detail"]["code"] == "app_daily_quota_exceeded"
            assert not second_transport.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("total", [False, True])
def test_registration_limits_are_atomic(pg_settings, total):
    settings = pg_settings.model_copy(
        update={
            "installations_total_limit": 3 if total else 100,
            "installations_daily_limit": 100 if total else 3,
        }
    )

    async def scenario():
        async with opened(settings) as store:
            replies = await asyncio.gather(
                *[new_owner(store, settings) for _ in range(12)], return_exceptions=True
            )
            assert sum(isinstance(reply, str) for reply in replies) == 3
            errors = [reply for reply in replies if isinstance(reply, AdmissionError)]
            assert len(errors) == 9
            expected = (
                "installation_capacity_reached" if total else "installation_daily_quota_exceeded"
            )
            assert all(error.code == expected and error.retry_after for error in errors)
            async with store.pool.connection() as connection:
                await connection.execute(
                    "UPDATE installations SET created_at = created_at - interval '1 day'"
                )
            if total:
                with pytest.raises(AdmissionError, match=expected):
                    await new_owner(store, settings)
            else:
                assert await new_owner(store, settings)

    asyncio.run(scenario())


def test_api_quota_and_queue_rejections_do_not_charge(pg_settings, app_factory):
    async def scenario():
        app, _, _ = app_factory(
            generation_app_enabled=True,
            generation_jobs_queue_limit=1,
            generation_app_daily_installation_limit=1,
            generation_app_daily_global_limit=1,
        )
        async with async_client(app) as client:
            _, headers = await installation(client)
            _, other = await installation(client)
            worker = app.state.generation_jobs
            # Общий слот удерживается без provider, очередь из одного задания.
            assert worker.slot.try_acquire()
            try:
                accepted = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
                assert accepted.status_code == 202
                job_id = accepted.json()["id"]
                rejected = await client.post(APP_JOBS_PATH, headers=other, json=example_request())
                assert (
                    rejected.status_code == 429
                    and rejected.json()["detail"]["code"] == "generation_queue_full"
                )
                assert rejected.headers["retry-after"] == "1"
                await worker.store.fail(job_id, "interrupted", datetime.now(UTC))
                replay = await client.post(APP_JOBS_PATH, headers=headers, json=example_request())
                assert replay.json() == accepted.json()
                rejected = await client.post(APP_JOBS_PATH, headers=other, json=example_request())
                assert (
                    rejected.status_code == 429
                    and rejected.json()["detail"]["code"] == "app_daily_quota_exceeded"
                )
                assert 1 <= int(rejected.headers["retry-after"]) <= 86400
                snapshot = await counts(worker.store)
                assert snapshot["app_daily_usage"][0]["accepted"] == 1
                assert len(snapshot["installation_daily_usage"]) == 1
            finally:
                worker.slot.release()

    asyncio.run(scenario())


def test_constraints_and_incremental_rollback_preserve_developer_jobs(pg_settings, postgres_url):
    async def scenario():
        async with opened(pg_settings) as store:
            owner = await new_owner(store, pg_settings)
            app_id, _ = await submit(store, pg_settings, owner)
            dev_id = await store.create(
                GenerationRequest.model_validate(example_request()), "v", 10, datetime.now(UTC)
            )
            async with store.pool.connection() as connection:
                for query, params, error in (
                    (
                        "UPDATE installations SET token_hash = %s",
                        (b"short",),
                        psycopg.errors.CheckViolation,
                    ),
                    (
                        "INSERT INTO installations "
                        "SELECT %s, token_hash, created_at FROM installations",
                        (uuid4(),),
                        psycopg.errors.UniqueViolation,
                    ),
                    (
                        "UPDATE generation_jobs SET idempotency_key = %s WHERE id = %s",
                        ("bad key", UUID(app_id)),
                        psycopg.errors.CheckViolation,
                    ),
                    (
                        "UPDATE generation_jobs SET request_hash = NULL WHERE id = %s",
                        (UUID(app_id),),
                        psycopg.errors.CheckViolation,
                    ),
                    (
                        "UPDATE generation_jobs SET installation_id = %s WHERE id = %s",
                        (uuid4(), UUID(app_id)),
                        psycopg.errors.ForeignKeyViolation,
                    ),
                ):
                    with pytest.raises(error):
                        async with connection.transaction():
                            await connection.execute(query, params)
                with pytest.raises(psycopg.errors.UniqueViolation):
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO generation_jobs SELECT %s, state, created_at, "
                            "started_at, finished_at, request_json, catalog_version, "
                            "result_json, error_code, installation_id, idempotency_key, "
                            "request_hash FROM generation_jobs WHERE id=%s",
                            (uuid4(), UUID(app_id)),
                        )
            return dev_id

    dev_id = asyncio.run(scenario())
    # Только 002: поддерживаемый откат удаляет app-данные, оставляет старые задания.
    migrate(postgres_url, rollback=True, steps=1)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT id FROM generation_jobs").fetchall() == [(UUID(dev_id),)]
        assert connection.execute("SELECT to_regclass('installations')").fetchone()[0] is None
    migrate(postgres_url)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT installation_id FROM generation_jobs").fetchall() == [
            (None,)
        ]


def test_app_requires_new_migration_but_developer_does_not(postgres_url, app_factory):
    migrations = read_migrations(str(PROJECT_ROOT / "migrations/postgresql"))
    with (
        get_backend(
            "postgresql+psycopg:///?" + urlencode(database_parameters(postgres_url))
        ) as backend,
        backend.lock(),
    ):
        backend.apply_migrations(
            backend.to_apply(migrations).filter(
                lambda migration: migration.id == "001_generation_jobs_postgresql"
            )
        )
    app, _, _ = app_factory(generation_jobs_database_url=postgres_url, generation_app_enabled=True)
    with pytest.raises(RuntimeError, match="миграции"), TestClient(app):
        pass
    app, _, _ = app_factory(generation_jobs_database_url=postgres_url)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
