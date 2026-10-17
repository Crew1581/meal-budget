"""Причины завершения и подготовка увеличенного лимита, только MockTransport."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_generation_http import HEADERS, envelope, example_request
from test_generation_http import RecordingTransport as HTTPTransport
from test_generation_http import app_factory as app_factory
from test_menu_generation import FAKE_KEY, RecordingTransport, response
from test_menu_generation import broken_diagnostic_collector as broken_diagnostic_collector
from test_menu_generation import cli_inputs as cli_inputs
from test_menu_generation import cli_transport as cli_transport
from test_menu_generation import diagnostic_report as diagnostic_report
from test_menu_generation import generated_plan as generated_plan
from test_menu_generation import generation_catalog as generation_catalog
from test_menu_generation import generation_request as generation_request
from test_menu_generation import provider_settings as provider_settings

from meal_budget import evaluate_menu, generate_menu
from meal_budget.core.config import PROJECT_ROOT, Settings
from meal_budget.evaluation import require_ready_scenario
from meal_budget.schemas.evaluation import RunSummary
from meal_budget.services.deepseek import DeepSeekProvider, InvalidResponse

MISSING = object()
PRIVATE = "untrusted-finish-reason " + FAKE_KEY


def reason_response(plan, reason):
    payload = json.loads(response(plan).content)
    choice = payload["choices"][0]
    if reason is MISSING:
        choice.pop("finish_reason")
    else:
        choice["finish_reason"] = reason
    return httpx.Response(200, json=payload)


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("stop", "stop"),
        ("length", "length"),
        ("content_filter", "content_filter"),
        ("tool_calls", "tool_calls"),
        ("insufficient_system_resource", "insufficient_system_resource"),
        (MISSING, None),
        (None, None),
        (PRIVATE, "other"),
        ("", "other"),
        (42, "other"),
        ({PRIVATE: PRIVATE}, "other"),
        ([PRIVATE], "other"),
    ],
)
def test_provider_completion_and_invalid_response_keep_safe_reason(
    provider_settings, generated_plan, reason, expected
):
    transport = RecordingTransport([reason_response(generated_plan, reason)])

    async def complete():
        async with DeepSeekProvider(provider_settings, transport=transport) as provider:
            if expected == "stop":
                return await provider.complete([])
            with pytest.raises(InvalidResponse) as error:
                await provider.complete([])
            assert error.value.violation == "incomplete_response"
            assert error.value.finish_reason == expected
            assert error.value.response_metadata.model == "served-test"
            assert error.value.response_metadata.usage["completion_tokens"] == 30
            assert PRIVATE not in str(error.value)
            return None

    completion = asyncio.run(complete())
    if completion:
        assert completion.finish_reason == "stop"
        assert json.loads(completion.content) == generated_plan
    assert len(transport.requests) == 1 and transport.closed


@pytest.mark.parametrize(
    "reasons,expected,exit_code",
    [
        (["stop"], ["stop"], 0),
        (["length", "length", "stop"], ["length", "length", "stop"], 0),
        (["length"], ["length"] * 3, 1),
        ([MISSING, PRIVATE, "stop"], [None, "other", "stop"], 0),
        ([None], [None] * 3, 1),
        (
            ["content_filter", "tool_calls", "insufficient_system_resource"],
            ["content_filter", "tool_calls", "insufficient_system_resource"],
            1,
        ),
        ([{PRIVATE: PRIVATE}], ["other"] * 3, 1),
    ],
)
def test_local_report_keeps_every_response_reason(
    diagnostic_report, generated_plan, reasons, expected, exit_code, cli_inputs, capsys, caplog
):
    # JSON и меню валидны: одного этого недостаточно для успеха при length/unknown/null.
    generated_plan["recipes"][0]["name"] = "failed-menu-must-not-be-in-report"
    report, text, transport = diagnostic_report(
        [reason_response(generated_plan, reason) for reason in reasons], expected_exit=exit_code
    )
    raw = json.loads(text)
    assert raw["response_metadata"] == [
        {"attempt": index, "finish_reason": reason}
        for index, reason in enumerate(expected, start=1)
    ]
    assert len(report.response_metadata) == len(report.metadata.responses) == len(expected)
    assert report.metadata.calls == len(transport.requests) == len(expected)
    assert all(r.usage["completion_tokens"] == 30 for r in report.metadata.responses)
    for attempt, reason in zip(report.attempts, expected, strict=True):
        if reason != "stop":
            assert attempt.stage == "parsing"
            assert attempt.violations[0].code == "incomplete_response"
    for request in transport.requests[1:]:
        correction = json.loads(json.loads(request.content)["messages"][-1]["content"])[
            "correction"
        ]
        assert correction["previous_plan"] is None
    if exit_code:
        assert report.error_code == "invalid_response"
        assert report.budget_pass is report.nutrition_pass is None
    else:
        # Файл результата, как HTTP-контракт, не расширяется.
        saved = json.loads(cli_inputs[1].read_bytes())
        assert "finish_reason" not in json.dumps(saved)
        assert "response_metadata" not in saved
    captured = capsys.readouterr()
    for forbidden in (
        PRIVATE,
        FAKE_KEY,
        "reasoning_content",
        "failed-menu-must-not-be-in-report",
        "recipes",
        "wishes",
        "Authorization",
    ):
        assert forbidden not in text + captured.out + captured.err + caplog.text


@pytest.mark.parametrize("kind", ["empty", "invalid_json", "schema", "envelope"])
def test_stop_metadata_survives_other_invalid_responses(diagnostic_report, generated_plan, kind):
    bad = response({"empty": "", "invalid_json": "{", "schema": {}, "envelope": {}}[kind])
    if kind == "envelope":
        payload = json.loads(bad.content)
        payload["choices"][0]["message"] = {}
        bad = httpx.Response(200, json=payload)
    report, _, _ = diagnostic_report([bad, response(generated_plan)], expected_exit=0)
    assert [r.finish_reason for r in report.response_metadata] == ["stop", "stop"]
    assert report.attempts[0].violation_count > 0


def test_response_metadata_survives_attempt_collector_failure(
    broken_diagnostic_collector, diagnostic_report, generated_plan
):
    report, _, _ = diagnostic_report(
        [response(generated_plan, finish="length"), response(generated_plan)], expected_exit=0
    )
    assert [r.finish_reason for r in report.response_metadata] == ["length", "stop"]
    assert all(not attempt.available for attempt in report.attempts)
    assert broken_diagnostic_collector == [1]


@pytest.mark.parametrize("kind", ["broken_json", "too_large", "http", "timeout"])
def test_unavailable_reason_is_not_invented(diagnostic_report, kind):
    reply = {
        "broken_json": httpx.Response(200, content=b'{"choices":'),
        "too_large": httpx.Response(200, content=b" " * 262145),
        "http": httpx.Response(503, json={"finish_reason": PRIVATE}),
        "timeout": httpx.ReadTimeout(PRIVATE),
    }[kind]
    report, text, _ = diagnostic_report([reply], expected_exit=1)
    expected = [] if kind in {"http", "timeout"} else [None] * 3
    assert [r.finish_reason for r in report.response_metadata] == expected
    assert report.metadata.calls == (1 if not expected else 3)
    assert PRIVATE not in text


@pytest.mark.parametrize("scenario_id", ["ordinary", "weekly"])
def test_prepared_16384_command_reaches_transport(tmp_path, monkeypatch, scenario_id):
    scenario = require_ready_scenario(scenario_id)
    transport = RecordingTransport([response(scenario.witness.model_dump(mode="json"))])
    monkeypatch.setattr(generate_menu, "PROJECT_ROOT", tmp_path)
    for key, value in {
        "CATALOG_PATH": str(PROJECT_ROOT / "data/5ka-products.json"),
        "DEEPSEEK_API_KEY": FAKE_KEY,
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_THINKING": "enabled",
        "DEEPSEEK_REASONING_EFFORT": "high",
        "DEEPSEEK_MAX_OUTPUT_TOKENS": "16384",
        "GENERATION_NUTRITION_TOLERANCE": "0.10",
        "GENERATION_TOTAL_TIMEOUT": "180",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        generate_menu,
        "DeepSeekProvider",
        lambda settings: DeepSeekProvider(settings, transport=transport),
    )
    prefix = f"evaluation/{scenario_id}-meal7-16384-run-1"
    assert (
        generate_menu.main(
            [
                "--request",
                str(PROJECT_ROOT / f"examples/evaluation/{scenario_id}.request.json"),
                "--scenario",
                scenario_id,
                "--output",
                f"{prefix}.menu.json",
                "--report",
                f"{prefix}.report.json",
            ]
        )
        == 0
    )
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content)
    assert body["max_tokens"] == 16384
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    report = evaluate_menu.read_report(Path(f"{prefix}.report.json"))
    assert report.catalog_version == "5ka-2026-10-14-observed-v3"
    assert report.response_metadata[0].finish_reason == "stop"
    assert report.budget_pass is report.nutrition_pass is True
    # Повтор не инициирует новые вызовы и не перезаписывает файлы.
    assert (
        generate_menu.main(
            [
                "--request",
                str(PROJECT_ROOT / f"examples/evaluation/{scenario_id}.request.json"),
                "--scenario",
                scenario_id,
                "--output",
                f"{prefix}.menu.json",
                "--report",
                f"{prefix}.report.json",
            ]
        )
        == 2
    )
    assert len(transport.requests) == 1


def test_defaults_and_old_report_are_unchanged(monkeypatch):
    for key in (
        "DEEPSEEK_MAX_OUTPUT_TOKENS",
        "GENERATION_TOTAL_TIMEOUT",
        "DEEPSEEK_MAX_RESPONSE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.deepseek_max_output_tokens == 8192
    assert settings.generation_total_timeout == 180
    assert settings.deepseek_max_response_bytes == 262144
    old = RunSummary.model_validate(
        {
            "scenario": "ordinary",
            "catalog_version": "old",
            "outcome": "error",
            "metadata": {"calls": 1, "duration_seconds": 1, "responses": [{"model": "old"}]},
        }
    )
    assert old.response_metadata is None


@pytest.mark.parametrize("reason,status", [("stop", 200), ("length", 502), (PRIVATE, 502)])
def test_http_contract_has_no_completion_diagnostics(app_factory, reason, status):
    async def reply(request):
        payload = envelope()
        payload["choices"][0]["finish_reason"] = reason
        return httpx.Response(200, json=payload)

    app, transport, _ = app_factory(HTTPTransport(reply))
    with TestClient(app) as client:
        result = client.post("/v1/menu-generations", headers=HEADERS, json=example_request())
        assert result.status_code == status
        body = result.json() if status == 200 else result.json()["detail"]
        assert set(body["metadata"]) == {"calls", "duration_seconds", "responses"}
        assert all(set(r) == {"model", "usage"} for r in body["metadata"]["responses"])
        schema_text = json.dumps(client.get("/openapi.json").json())
        for forbidden in ("finish_reason", "response_metadata", "attempts", PRIVATE):
            assert forbidden not in result.text + schema_text
    assert len(transport.requests) == (1 if status == 200 else 3)
