from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from capgate.models import TaskSpec
from capgate.system import System

PUBLIC_SOURCE_COMMIT = "dbce190be32f52eb93a6fd3909fe4d05213c9ba7"
# 实验固定使用公开价目表的高峰费率，以同一套费率比较用量。
FLASH_PEAK_USD_PER_MILLION = {"input": 0.3, "output": 1.2, "cached_input": 0.006}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CAPGATE_LLM_API_KEY")
        or not os.environ.get("CAPGATE_LLM_BASE_URL")
        or not os.environ.get("CAPGATE_TEST_PUBLIC_STATS_ROOT"),
        reason="需要真实模型 API 和公开统计项目的本地 Git 工作区",
    ),
]


@pytest.mark.parametrize("schema_name", ["model_doc_answer", "codex_change"])
async def test_openai_rag_records_real_usage(
    system: System, tmp_path: Path, repo_root: Path, schema_name: str
) -> None:
    public_root = os.environ["CAPGATE_TEST_PUBLIC_STATS_ROOT"]
    process = await asyncio.create_subprocess_exec(
        "git",
        "show",
        f"{PUBLIC_SOURCE_COMMIT}:stats.go",
        cwd=public_root,
        stdout=subprocess.PIPE,
    )
    contents, _ = await process.communicate()
    assert process.returncode == 0 and b"ErrNonFiniteInput" in contents
    corpus = tmp_path / "public-corpus"
    corpus.mkdir()
    (corpus / "stats.go").write_bytes(contents)
    system.pool.get("mcp-rag").versions[0].env["CAPGATE_CORPUS"] = str(corpus)
    model_env = system.registry.get("doc-qa-model")
    model_env.agent.config["prices_usd_per_million"] = {
        "deepseek-flash": FLASH_PEAK_USD_PER_MILLION
    }
    if schema_name != "model_doc_answer":
        schemas = tmp_path / "schemas"
        schemas.mkdir()
        shutil.copyfile(
            repo_root / "schemas" / f"{schema_name}.schema.json",
            schemas / "model_doc_answer.schema.json",
        )
        system.store.schemas = schemas

    task = TaskSpec(
        goal="检索公开 Go 代码，再回答 Mean 用什么数值类型累加。答案须包含代码中的类型名。",
        tags=["模型知识问答"],
        hint_envs=["doc-qa-model"],
    )
    result = await system.orchestrator.run(task)
    if schema_name == "model_doc_answer":
        assert result.status == "ok", result.error or [run.error for run in result.runs]
        assert result.artifacts[0].name == "model_doc_answer"
        artifact = system.store.load(result.artifacts[0])
        assert "big.Rat" in artifact["answer"]
        assert artifact["sources"] == ["stats.go"]
        assert any(item.get("tool") == "mcp_rag__search" for item in result.runs[0].trace)
    else:
        assert result.status == "failed"
        assert result.error is not None and "产物 model_doc_answer" in result.error
        assert not result.artifacts
    assert result.runs[0].adapter == "openai"

    (run,) = system.ledger.runs(task.id)
    assert run["status"] == result.status
    events = [
        item["payload"] for item in system.ledger.events(task.id) if item["kind"] == "llm.usage"
    ]
    meta = json.loads(run["meta"])
    usage = meta["usage"]
    assert meta["summary"]
    assert len(events) == usage["requests"] >= 2
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"):
        assert usage[key] == pytest.approx(sum(event[key] for event in events))
        assert usage[key] > 0
    assert system.router._cost_factor(run["latency_ms"], usage["total_tokens"]) < (
        system.router._cost_factor(run["latency_ms"])
    )
    (perf,) = system.ledger.perf(env_id="doc-qa-model", task_type=task.task_type)
    assert perf["avg_tokens"] == usage["total_tokens"]
    assert perf["avg_cost"] == pytest.approx(usage["cost_usd"])
    print(f"真实 RAG 模型调用和账本用量 {json.dumps(usage, ensure_ascii=False)}")
