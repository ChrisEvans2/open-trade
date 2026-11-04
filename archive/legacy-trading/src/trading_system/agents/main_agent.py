from __future__ import annotations

from trading_system.agents.workspace import AgentWorkspace, HermesBus
from trading_core.schemas import AgentMessage
from trading_system.telegram.commands import CommandRouter


class MainControlAgent:
    def __init__(self, name: str, workspace: AgentWorkspace, bus: HermesBus, command_router: CommandRouter):
        self.name = name
        self.workspace = workspace
        self.bus = bus
        self.command_router = command_router

    def receive_human_command(self, command: str) -> str:
        result = self.command_router.handle(command)
        self.workspace.append_outbox(
            AgentMessage(
                sender=self.name,
                recipient="human",
                type="command_result",
                payload={"command": command, "result": result},
            )
        )
        return result

    def forward_report_to_human(self, report: str) -> None:
        self.workspace.append_outbox(
            AgentMessage(sender=self.name, recipient="human", type="cycle_report", payload={"report": report})
        )
