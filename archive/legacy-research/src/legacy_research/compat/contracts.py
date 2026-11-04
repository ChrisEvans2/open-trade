from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import UUID

import yaml
from pydantic import Field, field_validator, model_validator

from trading_agent.config import StrictModel
from trading_agent.domain.errors import DomainError
from trading_agent.domain.time import utc


class LegacyPolicy(StrictModel):
    enabled: bool = False
    repository: Path
    apply_changes: bool = False
    test_python: str = "/opt/trading-test/bin/python"
    test_timeout_seconds: int = Field(default=90, ge=1, le=120)
    max_source_bytes: int = Field(default=20_000_000, ge=1024, le=50_000_000)

    @model_validator(mode="after")
    def bounded_paths(self):
        if not self.repository.is_absolute() or self.repository.is_symlink():
            raise ValueError("旧项目必须使用真实绝对目录")
        if not self.test_python.startswith("/") or any(c.isspace() for c in self.test_python):
            raise ValueError("测试解释器必须是沙箱内的绝对可执行路径")
        return self


def load_policy(path: Path) -> LegacyPolicy:
    policy = LegacyPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not policy.enabled:
        raise DomainError("LEGACY_COMPAT_DISABLED", "旧项目兼容入口尚未启用", 409)
    return policy


def project_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(p in {".", ".."} for p in relative.parts)
    ):
        raise DomainError("LEGACY_PATH_FORBIDDEN", "路径必须位于旧项目授权目录", 403)
    if "\\" in name or str(relative) != name:
        raise DomainError("LEGACY_PATH_FORBIDDEN", "路径必须使用规范项目相对路径", 403)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DomainError("LEGACY_PATH_FORBIDDEN", "旧项目兼容入口不跟随符号链接", 403)
    if not current.resolve().is_relative_to(root.resolve()):
        raise DomainError("LEGACY_PATH_FORBIDDEN", "路径超出旧项目", 403)
    return current


def strategy_path(name: str) -> bool:
    return name.startswith("src/trading_system/strategy/") and name.endswith(".py")


class CodeChange(StrictModel):
    path: str = Field(min_length=1, max_length=240)
    before_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=100_000)

    @field_validator("path")
    @classmethod
    def allowed_file(cls, value):
        if not strategy_path(value) and not (
            value.startswith("tests/")
            and PurePosixPath(value).name.startswith("test_")
            and value.endswith(".py")
        ):
            raise ValueError("仅允许策略Python源码和新增Python测试")
        # Check normalization independently of filesystem access.
        relative = PurePosixPath(value)
        if str(relative) != value or ".." in relative.parts or "\\" in value:
            raise ValueError("策略文件路径不规范")
        return value


class IterationRequest(StrictModel):
    iteration_id: UUID
    evidence_snapshot_id: UUID
    hypothesis: str = Field(min_length=1, max_length=2000)
    risk: str = Field(min_length=1, max_length=800)
    rollback: str = Field(min_length=1, max_length=800)
    changes: Annotated[tuple[CodeChange, ...], Field(min_length=1, max_length=8)]
    memory_recall_id: UUID | None = None
    memory_experience_ids: Annotated[tuple[UUID, ...], Field(max_length=5)] = ()

    @model_validator(mode="after")
    def unique_and_bounded(self):
        if len(set(self.memory_experience_ids)) != len(self.memory_experience_ids):
            raise ValueError("经验引用ID不得重复")
        if self.memory_experience_ids and self.memory_recall_id is None:
            raise ValueError("经验引用必须携带冻结召回清单ID")
        if len({item.path for item in self.changes}) != len(self.changes):
            raise ValueError("同一迭代不能重复修改文件")
        if sum(len(item.content.encode()) for item in self.changes) > 200_000:
            raise ValueError("策略补丁超过200KB")
        if not any(strategy_path(item.path) for item in self.changes):
            raise ValueError("迭代必须包含策略修改")
        return self


def iteration_payload(request: IterationRequest):
    """Preserve existing idempotency fingerprints for requests without memory references."""
    payload = request.model_dump()
    if request.memory_recall_id is None:
        payload.pop("memory_recall_id")
        payload.pop("memory_experience_ids")
    return payload


def review_range(start: datetime, end: datetime, decision: datetime):
    start, end, decision = utc(start), utc(end), utc(decision)
    if start >= end or end > decision:
        raise DomainError("INVALID_REVIEW_RANGE", "成交复盘必须使用已发生的半开时间区间")
    return start, end, decision
