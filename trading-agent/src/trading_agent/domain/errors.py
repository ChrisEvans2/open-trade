from __future__ import annotations


class DomainError(Exception):
    """A public, non-secret error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status: int = 422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class StaleAttempt(DomainError):
    def __init__(self):
        super().__init__("ATTEMPT_EXPIRED", "任务执行资格已失效", 409)
