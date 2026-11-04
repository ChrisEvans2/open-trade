from urllib.parse import urlencode
from uuid import UUID, uuid4

from ._http import Client, PluginError, result, schema


def register(ctx):
    from agent.secret_scope import get_secret

    def client(version="v1"):
        return Client(
            ctx.get_config("api_url", "http://127.0.0.1:8765/v1")
            .rstrip("/")
            .removesuffix("/v1")
            .removesuffix("/v2")
            + ("/v2" if version == "v2" else "/v1"),
            get_secret("TA_CONTROLLER_TOKEN"),
        )

    def ident(args, name):
        try:
            return str(UUID(args[name]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise PluginError("OBJECT_ID_INVALID", "对象ID必须为UUID") from None

    def read(route, version="v1"):
        return lambda args, **_: result(lambda: client(version).request("GET", route(args)))

    def latest_prediction(args):
        if set(args) - {"symbol", "timeframe", "parent"} or args.get("symbol") not in {
            "BTCUSDT",
            "ETHUSDT",
        }:
            raise PluginError("PREDICTION_SCOPE", "请选择BTCUSDT或ETHUSDT")
        timeframe = args.get("timeframe", "1h")
        parent = args.get("parent", True)
        if timeframe not in {"1h", "4h", "6h", "12h"} or type(parent) is not bool:
            raise PluginError("PREDICTION_SCOPE", "周期或父子范围无效")
        return "/predictions/latest?" + urlencode(
            {"symbol": args["symbol"], "timeframe": timeframe, "parent": str(parent).lower()}
        )

    def create(args, **_):
        def operation():
            allowed = {
                "kind",
                "prediction_id",
                "forecast_id",
                "symbol",
                "range_start",
                "range_end",
                "idempotency_key",
            }
            if set(args) - allowed:
                raise PluginError("JOB_FIELDS_INVALID", "任务包含不允许的字段")
            body = {key: value for key, value in args.items() if key != "idempotency_key"}
            return client("v2").request(
                "POST", "/jobs", body, key=args.get("idempotency_key") or str(uuid4())
            )

        return result(operation)

    entries = [
        (
            "trading_status",
            "查询交易runtime调度、任务和通知状态",
            {},
            (),
            read(lambda _: "/status"),
        ),
        (
            "trading_positions",
            "查询当前部署保存的持仓事实",
            {},
            (),
            read(lambda _: "/positions", "v2"),
        ),
        (
            "trading_prediction_latest",
            "查询BTC/ETH当前结构预测、验证状态和父Agent综合",
            {
                "symbol": {"type": "string", "enum": ["BTCUSDT", "ETHUSDT"]},
                "timeframe": {"type": "string", "enum": ["1h", "4h", "6h", "12h"]},
                "parent": {"type": "boolean"},
            },
            ("symbol",),
            read(latest_prediction, "v2"),
        ),
        (
            "trading_prediction_get",
            "查询不可改写的结构预测原件",
            {"prediction_id": {"type": "string"}},
            ("prediction_id",),
            read(lambda a: "/predictions/" + ident(a, "prediction_id"), "v2"),
        ),
        (
            "trading_intent_get",
            "查询交易意图及确定性执行状态",
            {"intent_id": {"type": "string"}},
            ("intent_id",),
            read(lambda a: "/intents/" + ident(a, "intent_id"), "v2"),
        ),
        (
            "trading_latest",
            "查询最近正式预测、结算和当前策略",
            {},
            (),
            read(lambda _: "/forecasts/latest"),
        ),
        (
            "trading_forecast_get",
            "查询预测原件及结算修订链",
            {"forecast_id": {"type": "string"}},
            ("forecast_id",),
            read(lambda a: "/forecasts/" + ident(a, "forecast_id")),
        ),
        (
            "trading_job_create",
            "向独立预测Hermes申请解释或指定区间复盘；实验受配置门禁保护，不下单",
            {
                "kind": {"type": "string", "enum": ["explain", "review", "experiment"]},
                "prediction_id": {"type": "string"},
                "forecast_id": {"type": "string"},
                "symbol": {"type": "string", "enum": ["BTCUSDT", "ETHUSDT"]},
                "range_start": {"type": "string"},
                "range_end": {"type": "string"},
                "idempotency_key": {"type": "string", "maxLength": 128},
            },
            ("kind",),
            create,
        ),
        (
            "trading_job_get",
            "查询领域任务状态",
            {"job_id": {"type": "string"}},
            ("job_id",),
            read(lambda a: "/jobs/" + ident(a, "job_id")),
        ),
        ("trading_strategy_list", "查询策略及审核状态", {}, (), read(lambda _: "/strategies")),
        (
            "trading_experiment_get",
            "查询实验进度与报告",
            {"experiment_id": {"type": "string"}},
            ("experiment_id",),
            read(lambda a: "/experiments/" + ident(a, "experiment_id")),
        ),
        (
            "trading_review_get",
            "查询数值复盘与解释",
            {"review_id": {"type": "string"}},
            ("review_id",),
            read(lambda a: "/reviews/" + ident(a, "review_id")),
        ),
    ]
    for name, description, properties, required, handler in entries:
        ctx.register_tool(
            name=name,
            toolset="trading_bridge",
            schema=schema(name, description, properties, required),
            handler=handler,
            description=description,
        )
