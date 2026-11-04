from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel

from trading_core.schemas import AgentMessage


class AgentWorkspace:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.inbox_path = self.path / "inbox.jsonl"
        self.outbox_path = self.path / "outbox.jsonl"
        self.state_path = self.path / "state.json"
        self.path.mkdir(parents=True, exist_ok=True)
        for item in (self.inbox_path, self.outbox_path):
            item.touch(exist_ok=True)
        if not self.state_path.exists():
            self.write_state({})

    def write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8") or "{}")

    def append_inbox(self, message: AgentMessage) -> None:
        self._append_jsonl(self.inbox_path, message)

    def append_outbox(self, message: AgentMessage) -> None:
        self._append_jsonl(self.outbox_path, message)

    def read_inbox(self) -> list[AgentMessage]:
        return list(self._read_messages(self.inbox_path))

    def read_outbox(self) -> list[AgentMessage]:
        return list(self._read_messages(self.outbox_path))

    def clear_inbox(self) -> None:
        self.inbox_path.write_text("", encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, model: BaseModel) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(model.model_dump_json() + "\n")

    @staticmethod
    def _read_messages(path: Path) -> Iterable[AgentMessage]:
        if not path.exists():
            return []
        messages: list[AgentMessage] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                messages.append(AgentMessage.model_validate_json(line))
        return messages


class HermesBus:
    """File-backed structured bus that mirrors Hermes-style agent handoff."""

    def __init__(self, workspaces: dict[str, AgentWorkspace]):
        self.workspaces = workspaces

    def send(self, message: AgentMessage) -> None:
        if message.recipient not in self.workspaces:
            raise KeyError(f"unknown recipient workspace: {message.recipient}")
        if message.sender in self.workspaces:
            self.workspaces[message.sender].append_outbox(message)
        self.workspaces[message.recipient].append_inbox(message)
