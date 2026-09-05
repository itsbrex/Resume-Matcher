"""Real owned-server smoke with synthetic configuration, never provider traffic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

import httpx
import pytest

from app.config import save_config_file, save_api_keys_to_config, settings
from e2e_monitor.bundle import Bundle
from e2e_monitor.flow import seed_master_db
from e2e_monitor.servers import Servers

_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE_CONNECTION = socket.create_connection


@pytest.fixture
def loopback_only(monkeypatch: pytest.MonkeyPatch, deny_external_network: Any) -> None:
    del deny_external_network

    def connect(sock: socket.socket, address: Any) -> Any:
        assert isinstance(address, tuple) and address[0] in (
            "127.0.0.1",
            "::1",
        ), "Only owned loopback connections allowed"
        return _CONNECT(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> Any:
        assert isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")
        return _CONNECT_EX(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", _CREATE_CONNECTION)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("source", ["encrypted", "environment", "local"])
def test_owned_backend_reads_seed_and_selected_credentials_then_tears_down(
    tmp_path: Path,
    sample_resume: dict[str, Any],
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    loopback_only: None,
) -> None:
    del loopback_only
    key = "" if source == "local" else "synthetic-monitor-key"
    provider = "ollama" if source == "local" else "openai"
    save_config_file(
        {
            "provider": provider,
            "model": "synthetic-monitor-model",
            "api_base": "http://127.0.0.1:1",
            "reasoning_effort": "",
            "unrelated_setting": "do not copy",
        }
    )
    if source == "encrypted":
        save_api_keys_to_config(
            {"openai": key, "anthropic": "synthetic-unselected-key"}
        )
    monkeypatch.setattr(
        settings, "llm_api_key", key if source == "environment" else "unrelated-env-key"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-inherited-key")
    bundle = Bundle(tmp_path, f"run-{source}")
    bundle.ensure()
    resume_id = asyncio.run(seed_master_db(bundle.data_dir, sample_resume))
    servers = Servers(bundle, backend_port=free_port(), frontend_port=free_port())
    processes: list[subprocess.Popen[bytes]] = []
    try:
        assert servers.boot(with_frontend=False) == {"frontend_up": False}
        processes = list(servers.procs)
        response = httpx.get(
            f"{servers.api_base}/resumes", params={"resume_id": resume_id}, timeout=5
        )
        assert response.status_code == 200
        assert (
            response.json()["data"]["processed_resume"]["personalInfo"]["name"]
            == sample_resume["personalInfo"]["name"]
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,hashlib; from app.llm import get_llm_config; c=get_llm_config(); print(json.dumps({'provider':c.provider,'model':c.model,'key_hash':hashlib.sha256(c.api_key.encode()).hexdigest()}))",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env={
                "PATH": os.environ.get("PATH", ""),
                "DATA_DIR": str(bundle.data_dir),
                "LLM_API_KEY": "",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            },
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        selected = json.loads(probe.stdout.strip().splitlines()[-1])
        assert selected == {
            "provider": provider,
            "model": "synthetic-monitor-model",
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        }
        config_text = (bundle.data_dir / "config.json").read_text()
        assert (
            "synthetic-monitor-key" not in config_text and "api_key" not in config_text
        )
        assert "unrelated_setting" not in config_text
    finally:
        servers.teardown()
    assert processes and all(proc.poll() is not None for proc in processes)
    assert not (bundle.data_dir / ".secret_key").exists()
    assert not servers.procs and not servers.log_files


def test_public_sweep_runs_real_http_flow_with_owned_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loopback_only: None,
) -> None:
    del loopback_only
    from e2e_monitor import __main__ as cli, servers as server_module
    from app import llm
    from unittest.mock import AsyncMock

    save_config_file(
        {
            "provider": "openai",
            "model": "synthetic-monitor-model",
            "reasoning_effort": "",
        }
    )
    save_api_keys_to_config({"openai": "synthetic-monitor-key"})
    monkeypatch.setenv("RM_E2E_MONITOR", "1")
    monkeypatch.setattr(cli, "_ARTIFACTS", tmp_path / "bundles")
    monkeypatch.setattr(cli, "_jds", lambda: [("synthetic", "Python backend engineer")])
    monkeypatch.setattr(cli, "_BASELINE", tmp_path / "no-baseline.json")
    monkeypatch.setattr(
        llm,
        "complete_json",
        AsyncMock(return_value={"score": 4, "reasons": "Grounded synthetic result"}),
    )
    # Replace only provider boundaries in the child. Uvicorn, routes, persistence,
    # hashes, HTTP client, seed, sweep orchestration and teardown remain real.
    script = tmp_path / "synthetic_monitor_backend.py"
    script.write_text(
        """
import sys
from typing import Any
import uvicorn
from app.main import app
from app.routers import resumes
from app.schemas.refinement import RefinementResult
from app.schemas import ImproveDiffResult
async def keywords(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        'required_skills': ['Python'],
        'preferred_skills': [],
        'keywords': ['backend'],
    }
async def targets(*args: Any, **kwargs: Any) -> dict[str, list[Any]]:
    return {'targets': []}
async def diffs(*args: Any, **kwargs: Any) -> ImproveDiffResult:
    return ImproveDiffResult(changes=[])
async def refine(*args: Any, **kwargs: Any) -> RefinementResult:
    return RefinementResult(refined_data=kwargs['initial_tailored'])
async def auxiliary(
    *args: Any, **kwargs: Any
) -> tuple[None, None, str, None, list[str]]:
    return None, None, 'Synthetic backend role', None, []
resumes.extract_job_keywords = keywords
resumes.generate_skill_target_plan = targets
resumes.generate_resume_diffs = diffs
resumes.refine_resume = refine
resumes._generate_auxiliary_messages = auxiliary
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]))
"""
    )
    real_popen = subprocess.Popen

    def start(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        env = dict(kwargs["env"])
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        return real_popen(
            [sys.executable, str(script), command[-1]], **{**kwargs, "env": env}
        )

    monkeypatch.setattr(server_module.subprocess, "Popen", start)
    result = cli.main(["sweep", "--no-frontend", "--backend-port", str(free_port())])
    bundles = list((tmp_path / "bundles").iterdir())
    trace = json.loads((bundles[0] / "flow-trace.json").read_text())
    assert result == 0, trace
    assert [step["stage"] for step in trace["stages"]] == [
        "seed-master",
        "boot",
        "tailor:synthetic",
        "judge:synthetic",
        "render:synthetic",
        "teardown",
    ]
    assert all(step["ms"] >= 0 for step in trace["stages"])
    assert trace["stages"][1]["ms"] > 0 and trace["stages"][2]["ms"] > 0
    assert trace["stages"][4]["skipped"] is True
    assert not (bundles[0] / "data" / ".secret_key").exists()
    summary = json.loads((bundles[0] / "summary.json").read_text())
    assert summary["variations"] == 1 and summary["min_judge_score"] == 4


def test_occupied_backend_port_is_not_reused_or_terminated(tmp_path: Path) -> None:
    bundle = Bundle(tmp_path, "occupied")
    bundle.ensure()
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        servers = Servers(bundle, backend_port=owner.getsockname()[1])
        with pytest.raises(RuntimeError, match="already in use"):
            servers.boot(with_frontend=False)
        assert owner.fileno() != -1
        assert not servers.procs and not servers.log_files


@pytest.mark.parametrize(
    "failure", [RuntimeError("synthetic boot failure"), KeyboardInterrupt()]
)
def test_public_sweep_persists_stage_failure_and_teardown_on_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    from e2e_monitor import __main__ as cli
    from unittest.mock import Mock

    save_config_file({"provider": "ollama", "model": "synthetic-local-model"})
    monkeypatch.setenv("RM_E2E_MONITOR", "1")
    monkeypatch.setattr(cli, "_ARTIFACTS", tmp_path / "bundles")
    monkeypatch.setattr(cli, "_BASELINE", tmp_path / "absent.json")
    teardown = Mock()
    monkeypatch.setattr(Servers, "boot", Mock(side_effect=failure))
    monkeypatch.setattr(Servers, "teardown", teardown)
    with pytest.raises(type(failure)):
        cli.main(["sweep", "--no-frontend"])
    teardown.assert_called_once()
    run = next((tmp_path / "bundles").iterdir())
    trace = json.loads((run / "flow-trace.json").read_text())
    boot = next(step for step in trace["stages"] if step["stage"] == "boot")
    assert boot["ok"] is False
    assert boot["cancelled"] == isinstance(failure, KeyboardInterrupt)
    assert boot["ms"] >= 0
    public_text = "\n".join(
        (run / name).read_text() for name in ("flow-trace.json", "summary.json")
    )
    if str(failure):
        assert str(failure) not in public_text

    diagnostic_path = run / "logs" / "private-diagnostics.log"
    diagnostic = diagnostic_path.read_text()
    assert type(failure).__name__ in diagnostic
    if str(failure):
        assert str(failure) in diagnostic


def test_frontend_proxy_targets_the_owned_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from e2e_monitor import servers as server_module
    from unittest.mock import Mock

    save_config_file({"provider": "ollama", "model": "synthetic-local-model"})
    bundle = Bundle(tmp_path, "frontend-env")
    bundle.ensure()
    commands: list[dict[str, Any]] = []

    def start(_command: list[str], **kwargs: Any) -> Any:
        commands.append(kwargs)
        process = Mock()
        process.poll.return_value = None
        return process

    monkeypatch.setattr(server_module.subprocess, "Popen", start)
    monkeypatch.setattr(server_module.shutil, "which", lambda _: "/synthetic/tool")
    monkeypatch.setattr(server_module, "_port_is_free", lambda _: True)
    monkeypatch.setattr(Servers, "_wait", lambda *_args, **_kwargs: True)
    servers = Servers(bundle, backend_port=18000, frontend_port=13000)
    try:
        assert servers.boot()["frontend_up"] is True
        assert commands[1]["env"]["BACKEND_ORIGIN"] == "http://127.0.0.1:18000"
        assert commands[0]["env"]["FRONTEND_BASE_URL"] == "http://127.0.0.1:13000"
    finally:
        servers.teardown()
