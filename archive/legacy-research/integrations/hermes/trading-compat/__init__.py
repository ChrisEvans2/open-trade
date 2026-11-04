import json
import os
import subprocess
from pathlib import Path


def register(ctx):
    from agent.subagent_lifecycle import get_active_subagent_parent

    names = {"legacy_trade_review", "legacy_strategy_source"}

    def authorized():
        parent = get_active_subagent_parent()
        if os.environ.get("TA_ATTEMPT_ID") or getattr(parent, "_delegate_depth", 0):
            raise ValueError("正式研究attempt与子Agent不能调用旧策略写入入口")

    def guard(tool_name="", **_):
        if tool_name in names:
            try:
                authorized()
            except ValueError:
                return {"action": "block", "message": "该角色不能访问旧策略兼容入口"}
        return None

    def invoke(action, arguments, payload=None):
        try:
            authorized()
            settings = {
                key: ctx.get_config(key, "") for key in ("executable", "config_path", "policy_path")
            }
            if any(not value or not Path(value).is_absolute() for value in settings.values()):
                raise ValueError("兼容入口需要管理员配置绝对路径")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONNOUSERSITE": "1"}
            result = subprocess.run(
                [
                    settings["executable"],
                    "--config",
                    settings["config_path"],
                    "--json",
                    "legacy",
                    action,
                    "--policy",
                    settings["policy_path"],
                    *arguments,
                ],
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            response = json.loads(result.stdout)
            if result.returncode and "error" not in response:
                raise ValueError("兼容入口未返回有效结果")
            return json.dumps(response, ensure_ascii=False)
        except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
            return json.dumps(
                {
                    "error": {
                        "code": "LEGACY_COMPAT_FAILED",
                        "message": "兼容入口配置、角色或执行结果无效；检查兼容记录",
                    }
                },
                ensure_ascii=False,
            )

    def review(args, **_):
        if set(args) != {"range_start", "range_end"}:
            return invoke("invalid", [])
        return invoke("review", ["--from", args["range_start"], "--to", args["range_end"]])

    def source(args, **_):
        if set(args) != {"path"}:
            return invoke("invalid", [])
        return invoke("source", ["--path", args["path"]])

    ctx.register_hook("pre_tool_call", guard)
    tools = [
        (
            "legacy_trade_review",
            "冻结旧项目live/paper成交盈亏与持仓；不关联正式预测",
            {"range_start": {"type": "string"}, "range_end": {"type": "string"}},
            review,
            ["range_start", "range_end"],
        ),
        (
            "legacy_strategy_source",
            "读取授权旧策略源码与原始hash",
            {"path": {"type": "string"}},
            source,
            ["path"],
        ),
    ]
    for name, description, properties, handler, required in tools:
        ctx.register_tool(
            name=name,
            toolset="trading_compat",
            description=description,
            schema={
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
            handler=handler,
        )
