import json
import subprocess
import sys

import pytest

from meal_budget.core.config import PROJECT_ROOT


def run_process(code, directory):
    # Не наследуем ключи/секреты окружения. В каждом тесте новый интерпретатор.
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=directory,
        env={"PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "missing,platform",
    [
        (["O_DIRECTORY"], "linux"),
        (["O_NOFOLLOW"], "linux"),
        (["O_DIRECTORY", "O_NOFOLLOW"], "linux"),
        ([], "win32"),
        ([], "darwin"),
    ],
)
@pytest.mark.parametrize("help_only", [False, True])
def test_unsupported_capabilities_in_separate_process(tmp_path, missing, platform, help_only):
    args = ["--help"] if help_only else ["--request", "missing.json", "--output", "menu.json"]
    code = f"""
import os, sys
for name in {missing!r}:
    delattr(os, name)
# Импорт должен пройти даже без флагов, необходимых файловой защите.
from meal_budget import generate_menu
sys.platform = {platform!r}
def forbidden(*args, **kwargs):
    raise AssertionError("file operations or provider must not be invoked")
os.open = forbidden
generate_menu.DeepSeekProvider = forbidden
sys.exit(generate_menu.main({args!r}))
"""
    result = run_process(code, tmp_path)
    assert result.returncode == (0 if help_only else 2)
    assert "Linux" in (result.stdout if help_only else result.stderr)
    assert "Traceback" not in result.stdout + result.stderr
    if not help_only:
        assert "O_DIRECTORY" in result.stderr and "O_NOFOLLOW" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_linux_cli_success_in_separate_process(tmp_path):
    code = f"""
import json, os, sys
from pathlib import Path
import httpx
from meal_budget import generate_menu
from meal_budget.services.deepseek import DeepSeekProvider
generate_menu.PROJECT_ROOT = Path.cwd()
os.environ["DEEPSEEK_API_KEY"] = "fake-platform-test-key"
os.environ["DEEPSEEK_MODEL"] = "test-model"
os.environ["DEEPSEEK_BASE_URL"] = "https://provider.test"
plan = {{"recipes": [{{"id": "r", "name": "Test", "servings": 1,
    "ingredients": [{{"product_id": "oats", "grams": 100}}],
    "steps": ["Test"], "cooking_minutes": 10}}],
    "days": [{{"day": 1, "meals": [{{"recipe_id": "r", "servings": 1}}]}}]}}
body = json.dumps({{"model": "test-model", "choices": [{{"finish_reason": "stop",
    "message": {{"content": json.dumps(plan)}}}}]}}).encode()
calls = []
def handle(request):
    calls.append(request)
    return httpx.Response(200, stream=httpx.ByteStream(body))
generate_menu.DeepSeekProvider = lambda settings: DeepSeekProvider(
    settings, transport=httpx.MockTransport(handle))
request_path = {str(PROJECT_ROOT / "examples/generation-request.json")!r}
result = generate_menu.main(["--request", request_path,
    "--output", "tests/run-1.json"])
assert len(calls) == 1
sys.exit(result)
"""
    result = run_process(code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    saved = json.loads((tmp_path / "generated/tests/run-1.json").read_text(encoding="utf-8"))
    assert saved["metadata"]["calls"] == 1
    assert saved["calculation"]["within_budget"] is True


def test_module_help_in_separate_process(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "meal_budget.generate_menu", "--help"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "Linux" in result.stdout and "generated/" in result.stdout
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
