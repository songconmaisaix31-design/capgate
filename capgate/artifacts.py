"""产物库：内容寻址 + 契约校验 + 流式通道。

规矩（架构约束 2）：env 之间禁止直连，一切产物经这里中转。
这样可追溯、可重放、可计费；出问题能定位到"谁吐了什么"。

落盘路径：``var/artifacts/<task_id>/<name>@<version>-<sha256>.json``，文件名用完整
SHA-256（同名即同内容，杜绝八位摘要冲突）。写文件先落同目录临时文件再原子硬链接
安装，进程被 kill 也不会留下半截 JSON。

流式通道（issue #6）：声明 ``stream: true`` 的 env 按 chunk 写、按 chunk 读，仍经
本库中转。producer 每写一条 chunk 立即落盘到 ``<name>@<version>.stream``
（JSON-lines），并用内存里的 ``asyncio.Event`` 通知；consumer 增量读同一个
``.stream`` 文件——**chunk 数据走磁盘，通知走内存**，所以下游能在上游写出最终
快照之前就消费到先到的 chunk。

最终快照：``seal`` 把 chunk 组装成最终产物（dict chunk 浅合并成对象，其余拼成
列表），sha256 取**最终快照**的实际落盘字节哈希。断流用 ``mark_truncated`` 打上
明确标记，快照仍落盘（可重放），``Artifact.truncated`` 为真。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .models import Artifact, ArtifactContract, utcnow

# Windows 下 os.open 默认文本模式会翻译换行；显式二进制，避免 .stream 内容被改写。
_O_BINARY = getattr(os, "O_BINARY", 0)


class SchemaError(ValueError):
    pass


class StreamClosed(ValueError):
    """对已经 seal 的流继续写。"""

    pass


def _assemble(chunks: list[Any]) -> Any:
    """把 chunk 列表组装成最终快照：dict chunk 浅合并，其余拼成列表。"""
    if not chunks:
        return []
    if all(isinstance(c, dict) for c in chunks):
        merged: dict[str, Any] = {}
        for chunk in chunks:
            merged.update(chunk)
        return merged
    return chunks


class StreamWriter:
    """流式产物写入器（producer 侧）：每条 chunk 立即落盘，``seal`` 时组装最终快照。

    用法::

        writer = store.open_stream(task_id, name, env_id=env.id, run_id=run_id)
        writer.write_chunk({"url": ...})
        writer.write_chunk({"links": ...})
        artifact = writer.seal()

    断流在 ``seal`` 前调 ``mark_truncated()``，快照仍落盘，但 ``Artifact.truncated``
    为真，下游据此知道拿到的是半截数据。
    """

    def __init__(
        self,
        store: ArtifactStore,
        task_id: str,
        name: str,
        version: int = 1,
        env_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.store = store
        self.task_id = task_id
        self.name = name
        self.version = version
        self.env_id = env_id
        self.run_id = run_id
        self.chunks: list[Any] = []
        self.truncated = False
        self._sealed = False
        self._new_data = asyncio.Event()
        self.seal_time: float | None = None
        rel = Path(task_id) / f"{name}@{version}.stream"
        self.stream_rel = str(rel).replace("\\", "/")
        self.stream_path = store.root / rel
        self.stream_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.stream_path.unlink()
        # 二进制 + 持久 fd：每条 chunk 只 write，不反复 open/close；os.write 直写 OS，
        # 无用户态缓冲，另一侧 reader 立刻能读到。显式 O_BINARY 避免 Windows 换行翻译。
        self._fd = os.open(
            self.stream_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o644
        )

    @property
    def event(self) -> asyncio.Event:
        return self._new_data

    @property
    def sealed(self) -> bool:
        return self._sealed

    def write_chunk(self, chunk: Any) -> None:
        if self._sealed:
            raise StreamClosed(f"流式产物 {self.name} 已经 seal，不能再写 chunk")
        line = json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        self.chunks.append(chunk)
        self._new_data.set()

    def mark_truncated(self) -> None:
        self.truncated = True

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)

    def seal(self) -> Artifact:
        """组装最终快照并原子落盘，返回可重放的 Artifact（sha = 最终快照哈希）。"""
        if self._sealed:
            return self._artifact  # type: ignore[return-value]
        snapshot = _assemble(self.chunks)
        self._artifact = self.store.put(
            self.task_id,
            self.name,
            snapshot,
            version=self.version,
            env_id=self.env_id,
            run_id=self.run_id,
            stream=True,
            truncated=self.truncated,
        )
        self._sealed = True
        self.seal_time = time.monotonic()
        self._new_data.set()
        return self._artifact


class StreamReader:
    """流式产物读取器（consumer 侧）：从 ``.stream`` 文件增量读 chunk。

    chunk 数据从磁盘读，只借助 writer 的内存事件获知"有新数据"；因此下游能在
    上游 seal 之前就消费到先到的 chunk。
    """

    def __init__(self, writer: StreamWriter) -> None:
        self.writer = writer
        self._offset = 0
        self.merged: dict[str, Any] = {}
        self.first_chunk_read_at: float | None = None
        self._first_read_before_seal = False
        # 二进制读 + 持久 fd：从当前偏移增量读，不反复 open/close。显式 O_BINARY。
        self._fd = os.open(self.writer.stream_path, os.O_RDONLY | _O_BINARY)

    @property
    def truncated(self) -> bool:
        return self.writer.truncated

    @property
    def sealed(self) -> bool:
        return self.writer.sealed

    @property
    def first_chunk_before_seal(self) -> bool:
        """下游是否在 producer 写出最终快照之前就读到了第一个 chunk。

        这个标记在读到第一个 chunk 的那一刻捕获，不依赖 seal 之后再去比较时间。
        """
        return self._first_read_before_seal

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)

    def read_available(self) -> list[Any]:
        """读出自上次以来新落盘的 chunk，并把 dict chunk 浅合并进 ``merged``。"""
        chunks: list[Any] = []
        os.lseek(self._fd, self._offset, os.SEEK_SET)
        data = b""
        while True:
            part = os.read(self._fd, 65536)
            if not part:
                break
            data += part
        self._offset = os.lseek(self._fd, 0, os.SEEK_CUR)
        for raw in data.split(b"\n"):
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            chunk = json.loads(line)
            chunks.append(chunk)
            if isinstance(chunk, dict):
                self.merged.update(chunk)
        if chunks and self.first_chunk_read_at is None:
            self.first_chunk_read_at = time.monotonic()
            self._first_read_before_seal = not self.writer.sealed
        return chunks

    async def wait_for(self, until: list[str]) -> None:
        """增量读，直到 ``until`` 里的 key 都在 ``merged`` 中出现，或流已 seal。

        超时由外层（supervisor 的 budget 超时）兜底：producer 若永久不 seal，
        consumer 的 adapter.run 会撞上 budget 超时，不会无限等。
        """
        while True:
            self.read_available()
            if all(k in self.merged for k in until):
                return
            if self.sealed:
                return
            await self.writer.event.wait()
            self.writer.event.clear()


class StreamSession:
    """一个 env 执行期间活跃的流式通道集合。"""

    def __init__(
        self,
        writers: dict[str, StreamWriter] | None = None,
        readers: dict[str, StreamReader] | None = None,
    ) -> None:
        self.writers = writers or {}
        self.readers = readers or {}


# 当前 env 执行期间活跃的流式通道（supervisor 设置，scripted adapter 读取）。
current_stream: contextvars.ContextVar[StreamSession | None] = contextvars.ContextVar(
    "capgate_current_stream", default=None
)


class ArtifactStore:
    def __init__(self, root: Path | str, schemas: Path | str | None = None) -> None:
        self.root = Path(root)
        self.schemas = Path(schemas) if schemas else None
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写

    def put(
        self,
        task_id: str,
        name: str,
        payload: Any,
        version: int = 1,
        env_id: str | None = None,
        run_id: str | None = None,
        *,
        stream: bool = False,
        truncated: bool = False,
    ) -> Artifact:
        # 截断的流式快照不满足 schema（缺字段），跳过校验；正常产物仍严格校验。
        if not truncated:
            self.validate(name, payload)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        # 用完整 SHA-256 命名：同名即同内容，杜绝八位摘要冲突覆盖另一份文件。
        rel = Path(task_id) / f"{name}@{version}-{sha}.json"
        self._write_atomic(rel, raw)
        return Artifact(
            name=name,
            version=version,
            sha256=sha,
            path=str(rel).replace("\\", "/"),
            bytes=len(raw),
            producer_env=env_id,
            producer_run=run_id,
            created_at=utcnow(),
            stream=stream,
            truncated=truncated,
        )

    def _write_atomic(self, rel: Path, raw: bytes) -> None:
        """同目录临时文件 + 原子硬链接安装，杜绝半截文件。

        用完整 SHA-256 命名后，同名即同内容（内容寻址）：目标已存在时仅在字节
        完全一致才算成功，摘要冲突或其他任何 I/O 错误都直接失败，绝不静默覆盖。
        """
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(raw)
            try:
                os.link(tmp, dest)  # 原子安装：dest 要么完整出现，要么不存在
            except FileExistsError:
                if dest.read_bytes() != raw:  # 内容寻址下只接受字节一致的重复写
                    raise
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ 流式

    def open_stream(
        self,
        task_id: str,
        name: str,
        version: int = 1,
        env_id: str | None = None,
        run_id: str | None = None,
    ) -> StreamWriter:
        return StreamWriter(self, task_id, name, version=version, env_id=env_id, run_id=run_id)

    def read_stream(self, writer: StreamWriter) -> StreamReader:
        return StreamReader(writer)

    # ------------------------------------------------------------------ 读

    def load(self, artifact: Artifact) -> Any:
        return json.loads((self.root / artifact.path).read_text(encoding="utf-8"))

    def load_path(self, rel_path: str) -> Any:
        return json.loads((self.root / rel_path).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 删除

    def delete(self, rel_path: str) -> bool:
        """删除一个产物文件，顺手清掉因此变空的目录。返回是否真的删了文件。"""
        target = self.root / rel_path
        if not target.exists():
            return False
        target.unlink()
        parent = target.parent
        while parent != self.root:
            try:
                parent.rmdir()  # 只删空目录；非空会抛 OSError
            except OSError:
                break
            parent = parent.parent
        return True

    # ------------------------------------------------------------------ 契约

    def schema_for(self, name: str) -> dict[str, Any] | None:
        if not self.schemas:
            return None
        for candidate in (self.schemas / f"{name}.schema.json", self.schemas / f"{name}.json"):
            if candidate.exists():
                return json.loads(candidate.read_text(encoding="utf-8"))
        return None

    def validate(self, name: str, payload: Any) -> None:
        """有 schema 就严格校验；没有 schema 就只要求是合法 JSON 结构。"""
        schema = self.schema_for(name)
        if schema is None:
            if not isinstance(payload, (dict, list)):
                raise SchemaError(f"产物 {name} 必须是对象或数组，收到 {type(payload).__name__}")
            return
        try:
            import jsonschema  # type: ignore
        except ImportError:  # 降级：只校验顶层 required
            required = schema.get("required", []) if isinstance(schema, dict) else []
            missing = [k for k in required if not isinstance(payload, dict) or k not in payload]
            if missing:
                raise SchemaError(f"产物 {name} 缺少必填字段：{missing}") from None
            return
        try:
            jsonschema.validate(payload, schema)
        except Exception as exc:
            raise SchemaError(f"产物 {name} 不符合 schema：{exc}") from exc

    @staticmethod
    def missing_inputs(contract: ArtifactContract, have: set[str]) -> list[str]:
        return [ref for ref in contract.consumes if ArtifactContract.parse(ref)[0] not in have]
