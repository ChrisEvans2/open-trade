import os

from trading_agent.api.auth import CredentialHashes
from trading_agent.domain.errors import DomainError
from trading_agent.market.authenticated import create_market_client
from trading_agent.market.liquidations import LiquidationSampler
from trading_agent.market.trade_collector import TradeMinuteCollector
from trading_agent.runtime.deployment import DeploymentCheck
from trading_agent.runtime.lifecycle import Runtime
from trading_agent.runtime.sandboxes import CubeResources
from trading_agent.storage.database import Database


def build_runtime(config):
    if any(
        os.environ.get(key)
        for key in (
            "TA_OPERATOR_TOKEN",
            "TA_CONTROLLER_TOKEN",
            "TA_MIGRATION_DATABASE_URL",
            "TA_BACKUP_DATABASE_URL",
            "TA_RESTORE_DATABASE_URL",
        )
    ):
        raise DomainError(
            "RUNTIME_SECRET_SCOPE", "runtime不能持有人工、主控原始令牌或迁移/备份/恢复凭据", 503
        )
    credentials = CredentialHashes.from_environment()
    resources = CubeResources(config, os.environ.get("CUBE_API_KEY", ""))
    database = Database(os.environ.get("TA_DATABASE_URL", ""), config.runtime)
    return Runtime(
        config,
        database,
        credentials,
        resources,
        create_market_client(config.market, feature_config=config.features),
        DeploymentCheck(config, database),
        sampler_factory=LiquidationSampler,
        trade_collector_factory=TradeMinuteCollector,
    )
