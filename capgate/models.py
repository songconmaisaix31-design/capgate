"""核心数据模型。

设计约束（来自架构讨论）：
- env 是封装单位，同时是依赖 / 凭据 / 版本 / 隔离 / 失败单位；
- 网关 ↔ env 只有一套契约，env ↔ agent 的适配器下沉到 env 里；
- 产物有 schema，env 之间不直连；
- 每次决策都必须可解释（candidates + score + reason）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CapabilityKind = Literal["mcp", "rag", "doc", "sdk", "skill"]
FailurePolicy = Literal["abort", "degrade", "retry-alt"]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def digest(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- 能力与能力池


class CapabilityRef(BaseModel):
    """env 对能力池中某个能力的引用。

    ``version`` 是 PEP 440 说明符（``>=0.1,<0.2`` / ``*``）。解析到具体版本后
    会写进账本，保证一次运行可复现。
    """

    id: str
    kind: CapabilityKind = "mcp"
    version: str = "*"
    shared: bool = True  # True = 全池单例；False = 每个 env 实例一份
    config: dict[str, Any] = Field(default_factory=dict)


class CapabilityVersion(BaseModel):
    version: str
    transport: Literal["stdio", "http", "python"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    entry: str | None = None  # transport=python 时 "module:function"
    entries: dict[str, str] = Field(default_factory=dict)  # tool 名 -> "module:function"
    env: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)  # 允许暴露的工具白名单
    startup_timeout: float = 60.0  # 冷启动上限；Node 系 MCP 可能很慢
    call_timeout: float = Field(default=60.0, gt=0, allow_inf_nan=False)


class Capability(BaseModel):
    id: str
    kind: CapabilityKind = "mcp"
    title: str = ""
    versions: list[CapabilityVersion]

    def pick(self, specifier: str = "*") -> CapabilityVersion:
        return resolve_version(self.versions, specifier)


# ---------------------------------------------------------------- 产物契约


class ArtifactSpec(BaseModel):
    name: str
    version: int = 1
    schema_path: str | None = None


class ArtifactContract(BaseModel):
    produces: list[str] = Field(default_factory=list)  # "crawl_batch" / "crawl_batch@1"
    consumes: list[str] = Field(default_factory=list)

    @staticmethod
    def parse(ref: str) -> tuple[str, int | None]:
        if "@" in ref:
            name, _, ver = ref.partition("@")
            return name, int(ver)
        return ref, None


class Artifact(BaseModel):
    name: str
    version: int
    sha256: str
    path: str
    bytes: int
    producer_env: str | None = None
    producer_run: str | None = None
    created_at: str = Field(default_factory=utcnow)
    stream: bool = False  # 流式通道产物（chunk 序列，落盘成最终快照）
    truncated: bool = False  # 断流标记：下游拿到的是半截数据，而非完整快照


# ---------------------------------------------------------------- env


class AgentSpec(BaseModel):
    """env 自带的执行器画像。

    网关不关心 agent 是什么框架，只认 ``adapter`` + ``models``。
    ``models`` 的排序是人工先验，实际选择由 env 自己的性能画像决定。
    """

    adapter: str = "scripted"  # scripted | openai | cli
    models: list[str] = Field(default_factory=lambda: ["default"])
    recipe: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ResourceSpec(BaseModel):
    ttl_s: int = 900
    max_parallel: int = 4
    needs_network: bool = True
    warm: bool = False  # 是否优先常驻（"表现好的留下"）

    @field_validator("ttl_s", "max_parallel")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


class EnvManifest(BaseModel):
    id: str
    domain: str
    tags: list[str]
    capabilities: list[CapabilityRef] = Field(default_factory=list)
    agent: AgentSpec = Field(default_factory=AgentSpec)
    artifacts: ArtifactContract = Field(default_factory=ArtifactContract)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    prior: float = Field(default=1.0, gt=0)  # 路由先验权重
    failure_policy: FailurePolicy = "abort"
    stream: bool = False  # 声明本 env 的 produces 走流式通道（默认关闭）
    notes: str = ""

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("env 至少要有一个 tag，否则永远无法被路由到")
        return [t.strip() for t in v if t.strip()]


# ---------------------------------------------------------------- 任务


class TaskSpec(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    goal: str
    type: str | None = None  # 学习单位；缺省取 tags[0]
    tags: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    hint_envs: list[str] = Field(default_factory=list)
    allow_parallel: bool = True
    budget_s: int = 300
    taxonomy_version: str | None = None

    @property
    def task_type(self) -> str:
        return self.type or (self.tags[0] if self.tags else "general")


# ---------------------------------------------------------------- 决策与结果


class Candidate(BaseModel):
    id: str
    score: float
    sample: float | None = None
    success: int = 0
    fail: int = 0
    detail: str = ""


class Decision(BaseModel):
    stage: Literal["domain", "agent", "pipeline", "fallback", "retry"]
    strategy: str
    chosen: list[str]
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str


class RunResult(BaseModel):
    task_id: str
    env_id: str
    run_id: str
    status: Literal["ok", "failed", "cancelled", "degraded"]
    adapter: str
    model: str
    artifacts: list[Artifact] = Field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


class TaskResult(BaseModel):
    task: TaskSpec
    status: Literal["ok", "failed", "degraded"]
    runs: list[RunResult] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


# ---------------------------------------------------------------- 产物 GC


class GcItem(BaseModel):
    """一个即将被清理的产物文件。"""

    path: str
    sha256: str
    bytes: int
    task_id: str
    age_days: float


class GcReport(BaseModel):
    """`capgate gc` 的结果：删了什么、省了多少、护住了什么。"""

    dry_run: bool = False
    scanned: int = 0
    expired: int = 0
    protected: int = 0  # 过期但因引用计数/未完成任务而保留
    deleted: int = 0
    reclaimed_bytes: int = 0
    items: list[GcItem] = Field(default_factory=list)

    @property
    def reclaimed_mb(self) -> float:
        return self.reclaimed_bytes / (1024 * 1024)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(i.bytes for i in self.items)

    @property
    def reclaimable_mb(self) -> float:
        return self.reclaimable_bytes / (1024 * 1024)


# ---------------------------------------------------------------- 工具

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def expand(text: str) -> str:
    """展开 ``${VAR:default}``，让能力声明不必把绝对路径写死。"""

    def sub(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(2) or "")

    return _VAR.sub(sub, text)


def expand_version(version: CapabilityVersion) -> CapabilityVersion:
    return version.model_copy(
        update={
            "command": expand(version.command) if version.command else None,
            "args": [expand(a) for a in version.args],
            "cwd": expand(version.cwd) if version.cwd else None,
            "url": expand(version.url) if version.url else None,
            "env": {k: expand(v) for k, v in version.env.items()},
        }
    )


def resolve_version(versions: list[CapabilityVersion], specifier: str) -> CapabilityVersion:
    """按 PEP 440 说明符选最高版本；没有匹配就报错，绝不静默降级。"""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version

    try:
        spec = SpecifierSet("" if specifier in ("", "*") else specifier)
    except InvalidSpecifier as exc:  # pragma: no cover - 配置写错时给出可读错误
        raise ValueError(f"非法版本说明符 {specifier!r}: {exc}") from exc

    matched = [(Version(v.version), v) for v in versions if spec.contains(v.version)]
    if not matched:
        raise LookupError(
            f"没有满足 {specifier!r} 的版本（已注册：{[v.version for v in versions]}）"
        )
    return max(matched, key=lambda kv: kv[0])[1]
