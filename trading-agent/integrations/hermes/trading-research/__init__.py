import os
from urllib.parse import urlencode

from ._http import Client, PluginError, result, schema
from ._workflow import Workflow


def register(ctx):
    from agent.secret_scope import get_secret
    from agent.subagent_lifecycle import get_active_subagent_parent

    round_mode = os.environ.get("TA_ANALYSIS_ROUND") == "1"

    def client():
        run = None
        if round_mode:
            agent = get_active_subagent_parent()
            if agent and getattr(agent, "_delegate_depth", 0):
                run = workflow.session_runs.get(str(agent.session_id))
                if run is None:
                    raise PluginError("ANALYSIS_SCOPE", "子Agent没有本轮角色绑定")
            else:
                run = workflow.parent_run
        return Client(os.environ.get("TA_API_URL"), get_secret("TA_RESEARCH_TOKEN"), run)

    workflow = Workflow(client, lambda: os.environ["TA_INPUT_DIR"])
    workflow.lifecycle = getattr(ctx, "subagent_lifecycle", None)

    def guard(tool_name="", **_):
        allowed = {
            # Native lazy discovery only exposes this session's enabled tools.
            # tool_call is unwrapped by Hermes before this hook, so the actual
            # tool must still pass the research role guard below.
            "tool_search",
            "tool_describe",
            "terminal",
            "process_manage",
            "research_context",
            "timeframes_analyze",
            "review_submit",
            "decision_submit",
            "knowledge_search",
            "knowledge_expand",
        }
        parent = get_active_subagent_parent()
        depth = getattr(parent, "_delegate_depth", 0)
        if tool_name not in allowed or (
            depth and tool_name in {"timeframes_analyze", "review_submit", "decision_submit"}
        ):
            return {"action": "block", "message": "该工具不属于当前研究角色的授权范围"}
        return None

    def context(args, **_):
        def operation():
            if (
                set(args) - {"prediction_id", "child_ref"}
                or len(args) > 1
                or any(not isinstance(value, str) for value in args.values())
            ):
                raise PluginError("CONTEXT_SCOPE_FORBIDDEN", "只允许复盘案例ID或父综合的子证据引用")
            parent = get_active_subagent_parent()
            session = (
                str(parent.session_id)
                if parent and not getattr(parent, "_delegate_depth", 0)
                else None
            )
            route = "/research/context" + ("?" + urlencode(args) if args else "")
            return client().request("GET", route, session=session)

        return result(operation)

    def submit(route):
        def handler(args, **_):
            def operation():
                workflow.parent()
                if set(args) != {"payload"} or not isinstance(args["payload"], dict):
                    raise PluginError("SUBMISSION_INVALID", "提交需要一个严格payload对象")
                workflow.parent_usage()
                if round_mode and route == "/research/decision":
                    if not workflow.parent_run:
                        raise PluginError("ANALYSIS_SCOPE", "必须先准备完整周期结果和父综合输入")
                    client().request(
                        "POST", "/research/analysis/complete", {"run": workflow.parent_run}
                    )
                    try:
                        return client().request(
                            "POST", "/research/analysis/decision", args["payload"], timeout=15
                        )
                    except PluginError as exc:
                        # A new analysis needs a new role/input, not another
                        # model turn after declaring execution complete.
                        workflow.failed = True
                        client().request(
                            "POST",
                            "/research/analysis/fail",
                            {"run": workflow.parent_run, "code": exc.code},
                        )
                        raise
                return client().request("POST", route, args["payload"])

            return result(operation)

        return handler

    ctx.register_hook("pre_tool_call", guard)
    ctx.register_hook("subagent_start", workflow.child_started)
    entries = [
        (
            "research_context",
            "读取当前冻结证据；父可传child_ref核查子报告原输入，复盘可传prediction_id展开案例",
            {"prediction_id": {"type": "string"}, "child_ref": {"type": "string"}},
            (),
            context,
        ),
        (
            "timeframes_analyze",
            "按冻结任务范围运行需要更新的原生周期子Agent，保存结构预测",
            {},
            (),
            lambda args, **_: result(workflow.analyze),
        ),
        (
            "review_submit",
            "保存预测复盘和知识新增/修订/合并/退役建议；程序核验引用事实",
            {"payload": {"type": "object"}},
            ("payload",),
            submit("/research/review"),
        ),
        (
            "decision_submit",
            "保存父Agent综合预测内容与交易意图；工具绑定身份，不直接下单",
            {"payload": {"type": "object"}},
            ("payload",),
            submit("/research/decision"),
        ),
        (
            "knowledge_search",
            "查找本任务范围内的知识点摘要，最多5条；父子Agent均可读取",
            {
                "query": {"type": "string", "maxLength": 300},
                "structure": {"type": "string"},
                "stage": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            (),
            lambda args, **_: result(
                lambda: client().request("POST", "/research/knowledge/search", args)
            ),
        ),
        (
            "knowledge_expand",
            "展开已检索知识版本的案例和操作步骤，按next_offset继续读取",
            {
                ("ref" if round_mode else "version_id"): {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            ("ref" if round_mode else "version_id",),
            lambda args, **_: result(
                lambda: client().request(
                    "GET",
                    (
                        "/research/analysis/knowledge?"
                        if round_mode
                        else "/research/knowledge/content?"
                    )
                    + urlencode(args),
                )
            ),
        ),
    ]
    for name, description, properties, required, handler in entries:
        ctx.register_tool(
            name=name,
            toolset="trading_research",
            schema=schema(name, description, properties, required),
            handler=handler,
            description=description,
        )
