from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass

from fastapi import Request

from trading_agent.domain.errors import DomainError
from trading_agent.jobs.service import token_hash


@dataclass(frozen=True)
class Identity:
    role: str
    actor_id: str


@dataclass(frozen=True)
class CredentialHashes:
    controller: str
    operator: str

    def __post_init__(self):
        if not all(
            re.fullmatch(r"[0-9a-f]{64}", value) for value in (self.controller, self.operator)
        ) or hmac.compare_digest(self.controller, self.operator):
            raise ValueError("身份摘要必须是两个不同的SHA256值")

    @classmethod
    def from_environment(cls):
        return cls(
            os.environ.get("TA_CONTROLLER_TOKEN_SHA256", ""),
            os.environ.get("TA_OPERATOR_TOKEN_SHA256", ""),
        )

    def authenticate(self, token: str) -> Identity | None:
        digest = token_hash(token)
        if hmac.compare_digest(digest, self.controller):
            return Identity("controller", "controller")
        if hmac.compare_digest(digest, self.operator):
            return Identity("operator", "operator")
        return None


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value or len(value) > 256 or " " in value:
        raise DomainError("UNAUTHENTICATED", "需要有效的Bearer身份", 401)
    return value


async def require_identity(request: Request, roles=("controller", "operator")) -> Identity:
    token = bearer(request)
    identity = request.app.state.services.credentials.authenticate(token)
    if identity is None:
        # A valid research identity is authenticated but lacks controller/operator privileges.
        await request.app.state.services.jobs.authenticate(token, "context")
        raise DomainError("FORBIDDEN", "研究任务无权访问此接口", 403)
    if identity.role not in roles:
        raise DomainError("FORBIDDEN", "当前身份无权执行该操作", 403)
    return identity


async def require_attempt(request: Request, action: str):
    token = bearer(request)
    if request.app.state.services.credentials.authenticate(token):
        raise DomainError("FORBIDDEN", "该接口仅接受当前研究任务令牌", 403)
    attempt = await request.app.state.services.jobs.authenticate(token, action)
    if run := request.headers.get("X-Analysis-Run"):
        if action not in {"analysis", "usage"}:
            rounds = getattr(request.app.state.services.predictions, "rounds", None)
            if (
                rounds is None
                or not attempt.payload.get("analysis_round")
                or attempt.payload.get("analysis_managed")
            ):
                raise DomainError("ANALYSIS_SCOPE", "角色引用不属于当前任务", 403)
            attempt = await rounds.resolve(attempt, run)
    request.state.research_attempt = attempt
    return attempt
