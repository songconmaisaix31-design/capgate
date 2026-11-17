"""产物库：内容寻址 + 契约校验。

规矩（架构约束 2）：env 之间禁止直连，一切产物经这里中转。
这样可追溯、可重放、可计费；出问题能定位到"谁吐了什么"。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Artifact, ArtifactContract, utcnow


class SchemaError(ValueError):
    pass


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
    ) -> Artifact:
        self.validate(name, payload)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        rel = Path(task_id) / f"{name}@{version}-{sha[:8]}.json"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return Artifact(
            name=name,
            version=version,
            sha256=sha,
            path=str(rel).replace("\\", "/"),
            bytes=len(raw),
            producer_env=env_id,
            producer_run=run_id,
            created_at=utcnow(),
        )

    def load(self, artifact: Artifact) -> Any:
        return json.loads((self.root / artifact.path).read_text(encoding="utf-8"))

    def load_path(self, rel_path: str) -> Any:
        return json.loads((self.root / rel_path).read_text(encoding="utf-8"))

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
                raise SchemaError(f"产物 {name} 缺少必填字段：{missing}")
            return
        try:
            jsonschema.validate(payload, schema)
        except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
            raise SchemaError(f"产物 {name} 不符合 schema：{exc}") from exc

    @staticmethod
    def missing_inputs(contract: ArtifactContract, have: set[str]) -> list[str]:
        return [ref for ref in contract.consumes if ArtifactContract.parse(ref)[0] not in have]
