from datetime import timedelta

from trading_agent.domain.errors import DomainError
from trading_agent.runtime.logging import attempt_event


class PriceRefreshService:
    """Refetch the original instrument's exact endpoints; no new model decision."""

    def __init__(self, jobs, market, settlement):
        self.jobs, self.market, self.settlement = jobs, market, settlement

    async def run(self, attempt):
        if attempt.kind != "PRICE_REFRESH":
            raise DomainError("FORBIDDEN", "仅价格重查任务可执行此操作", 403)
        async with self.jobs.transaction(attempt) as connection:
            forecast = await self.settlement._forecast(connection, attempt)
            now = await self.jobs.database.now(connection)
        if forecast["end_at"] + timedelta(minutes=1) > now:
            raise DomainError("PRICING_NOT_DUE", "价格重查只能处理已到期预测", 409)
        inst_id = forecast["snapshot_payload"]["groups"][0]["bars"][-1]["inst_id"]
        deadline = now + timedelta(seconds=45)
        for endpoint, key in (("START", "start_at"), ("END", "end_at")):
            bar = await self.market.pricing_bar(forecast[key], deadline, inst_id=inst_id)
            if bar is None:
                raise DomainError("PRICE_REFRESH_MISSING", "历史端点暂不可重查", 503)
            await self.settlement.save_price(attempt, endpoint, bar)
        await self.jobs.finish(attempt, result_id=forecast["id"])
        attempt_event("pricing_refresh_completed", attempt, forecast_id=str(forecast["id"]))
        return forecast["id"]
