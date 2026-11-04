from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import yaml

from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.runtime.processes import ManagedProcess, safe_environment
from trading_agent.runtime.prompts import load_prompts, prompt_hashes
from trading_agent.storage.permissions import verify_app_permissions

REQUIRED_ISOLATION_CHECKS = frozenset(
    {
        "ordinary_user",
        "input_readonly",
        "scratch_writable",
        "sudo_denied",
        "guest_management_denied",
        "no_host_mounts",
        "no_credentials",
        "file_bridge_bounds",
        "research_public_access",
        "sensitive_addresses_denied",
        "formal_egress_denied",
        "ipv6_disabled",
        "shared_child_lifetime",
        "stale_attempt_denied",
        "owned_crash_recovery",
        "no_local_fallback",
        "server_auth_enforced",
        "protected_transport",
        "template_no_inherited_allow",
        "resource_limits",
    }
)
STABLE_MEMORY = {
    "USER.md": "用户有ADHD；回复简洁。仅交易研究，不执行实盘订单。\n",
    "MEMORY.md": (
        "事实通过当前任务领域API读取；遵守冻结时点。"
        "不得改写已发布预测、历史评分、审核或沙箱策略。\n"
    ),
}
MODEL_CHECK_TIMEOUT_SECONDS = 10
MODEL_CACHE_SECONDS = 30
MODEL_REFRESH_SECONDS = 20


def hermes_home(config):
    return (config.hermes.home or Path.home() / ".hermes").resolve()


def profile_home(config):
    name = config.hermes.trading_profile
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name == "default":
        raise DomainError("PROFILE_CONFIGURATION_INVALID", "研究必须使用独立的命名Profile", 503)
    return hermes_home(config) / "profiles" / name


def integration_root():
    packaged = Path(__file__).resolve().parents[1] / "resources/hermes"
    return (
        packaged
        if packaged.exists()
        else Path(__file__).resolve().parents[3] / "integrations/hermes"
    )


def tree_hash(root):
    if (
        not root.is_dir()
        or not (root / "__init__.py").is_file()
        or not (root / "plugin.yaml").is_file()
    ):
        raise DomainError("DEPLOYMENT_FILE_INVALID", "部署插件缺少源码或manifest", 503)
    files = {}
    for path in sorted(root.rglob("*")):
        if path.suffix in {".py", ".yaml"} and "__pycache__" not in path.parts:
            if path.is_symlink():
                raise DomainError("DEPLOYMENT_FILE_INVALID", "部署源码不可通过符号链接替换", 503)
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return content_hash(files)


def trading_profile_config(config):
    profile = {
        # Schema of the pinned Hermes revision; avoid a first-run migration breaking the lock.
        "_config_version": 48,
        "model": {"default": config.hermes.model, "provider": config.hermes.provider},
        "fallback_providers": [],
        "platform_toolsets": {"cli": ["trading_research", "terminal"]},
        "plugins": {"enabled": ["trading-research", "trading-cube"]},
        # This bounded role has only a few tools. Expose their schemas directly
        # so native discovery does not consume the child's bounded turn budget.
        "tools": {"tool_search": {"enabled": "off"}},
        "terminal": {
            "backend": "cubesandbox",
            "cwd": "/workspace/scratch",
            "timeout": 60,
            "container_persistent": False,
        },
        "delegation": {
            "provider": config.hermes.provider,
            "model": config.hermes.model,
            "max_iterations": 6,
            "max_concurrent_children": 4,
            "oneshot_max_children": 4,
            "max_spawn_depth": 1,
            "fallback_providers": [],
        },
        "memory": {"memory_enabled": True, "user_profile_enabled": True},
        "mcp_servers": {},
    }
    if config.hermes.model_base_url is not None:
        profile["model"]["base_url"] = config.hermes.model_base_url
        profile["delegation"]["base_url"] = config.hermes.model_base_url
    if config.hermes.reasoning_effort is not None:
        profile["agent"] = {"reasoning_effort": config.hermes.reasoning_effort}
        profile["delegation"]["reasoning_effort"] = config.hermes.reasoning_effort
    if config.hermes.trading_telegram_enabled:
        # Telegram dialogue has no attempt token: do not expose formal research writes or terminal.
        # Hermes otherwise auto-enables plugin toolsets even with an empty platform list.
        profile["platform_toolsets"]["telegram"] = []
        profile["known_plugin_toolsets"] = {"telegram": ["trading_research"]}
        profile["platforms"] = {
            "telegram": {
                "enabled": True,
                "gateway_restart_notification": False,
                "extra": {"drop_pending_on_cold_boot": False},
            }
        }
    return profile


def bridge_settings(config):
    return {"api_url": f"http://127.0.0.1:{config.runtime.api_port}/v1"}


def expected_lock(config):
    prompts = prompt_hashes(load_prompts(mode=config.prediction.mode))
    from trading_agent.prediction.service import prediction_engine_hash

    engine_hash = prediction_engine_hash(config, prompts)
    source = integration_root()
    return {
        "schema_version": 1,
        "hermes_commit": config.hermes.reference_commit,
        "provider": config.hermes.provider,
        "model": config.hermes.model,
        "engine_hash": engine_hash,
        "prompt_hashes": prompts,
        "controller_profile": config.hermes.controller_profile,
        "trading_profile": config.hermes.trading_profile,
        "notification_target": config.notification.target,
        "bridge_settings": bridge_settings(config),
        "cube": config.sandbox.model_dump(
            mode="json", exclude={"isolation_verified", "isolation_report"}
        ),
        "plugin_hashes": {
            name: tree_hash(source / name)
            for name in ("trading-bridge", "trading-research", "trading-cube")
        },
        "profile_hash": content_hash(trading_profile_config(config)),
        "preflight_hash": hashlib.sha256((source / "verify_runtime.py").read_bytes()).hexdigest(),
        "launcher_hash": hashlib.sha256((source / "run_hermes.py").read_bytes()).hexdigest(),
        "memory_hashes": {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in STABLE_MEMORY.items()
        },
    }


class DeploymentCheck:
    def __init__(self, config, database):
        self.config, self.database = config, database
        self.git_checked = False
        self.model_checked_at = 0.0
        self.model_ready = False
        self.model_lock = asyncio.Lock()
        self.model_refresh = None

    async def check_model(self, *, force=False):
        async with self.model_lock:
            if not force and time.monotonic() - self.model_checked_at < MODEL_CACHE_SECONDS:
                return self.model_ready
            return await self._check_model()

    async def _check_model(self):
        python = self.config.hermes.python_executable
        if python is None or not python.is_absolute():
            return False
        process = None
        try:
            process = await ManagedProcess.start(
                [
                    str(python),
                    str(integration_root() / "verify_runtime.py"),
                    str(self.config.hermes.repository),
                    self.config.hermes.provider,
                    self.config.hermes.model,
                ],
                env=safe_environment(
                    extra={
                        "HERMES_HOME": str(profile_home(self.config)),
                        "HOME": str(profile_home(self.config)),
                        "PYTHONNOUSERSITE": "1",
                    }
                ),
            )
            result = await process.wait(MODEL_CHECK_TIMEOUT_SECONDS)
            self.model_ready = (
                result.returncode == 0 and json.loads(result.stdout).get("configured") is True
            )
        except (ValueError, TypeError, AttributeError, OSError, TimeoutError):
            self.model_ready = False
        finally:
            if process is not None:
                await process.terminate()
        self.model_checked_at = time.monotonic()
        return self.model_ready

    def _schedule_model_refresh(self, delay):
        if self.model_refresh is None or self.model_refresh.done():
            self.model_refresh = asyncio.create_task(self._refresh_model_loop(delay))

    async def _refresh_model_loop(self, delay):
        await asyncio.sleep(delay)
        while True:
            await self.check_model(force=True)
            await asyncio.sleep(MODEL_REFRESH_SECONDS)

    async def ready(self):
        """Keep readiness bounded; refresh credentials before the cached result expires."""
        return await self(cached_model=True)

    async def close(self):
        if self.model_refresh is not None:
            self.model_refresh.cancel()
            await asyncio.gather(self.model_refresh, return_exceptions=True)

    def files(self):
        config = self.config
        config.market.require_instrument()
        if (
            not config.hermes.provider
            or not config.hermes.model
            or not config.notification.target
            or not config.hermes.controller_profile
            or config.hermes.controller_profile == config.hermes.trading_profile
        ):
            raise DomainError(
                "DEPLOYMENT_CONFIGURATION_INCOMPLETE", "部署模型或消息目标未配置", 503
            )
        manifest = json.loads(config.hermes.deployment_lock.read_text(encoding="utf-8"))
        expected = expected_lock(config)
        if {key: manifest.get(key) for key in expected} != expected:
            raise DomainError("DEPLOYMENT_LOCK_MISMATCH", "部署清单与代码、提示词或配置不一致", 503)
        home = profile_home(config)
        profile = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        if content_hash(profile) != expected["profile_hash"]:
            raise DomainError("PROFILE_LOCK_MISMATCH", "交易Profile已偏离锁定配置", 503)
        for name in ("trading-research", "trading-cube"):
            if tree_hash(home / "plugins" / name) != expected["plugin_hashes"][name]:
                raise DomainError("PLUGIN_LOCK_MISMATCH", "研究插件与部署锁不一致", 503)
        for name, expected_hash in expected["memory_hashes"].items():
            if hashlib.sha256((home / "memories" / name).read_bytes()).hexdigest() != expected_hash:
                raise DomainError("PROFILE_LOCK_MISMATCH", "稳定偏好文件已偏离部署锁", 503)
        controller_name = config.hermes.controller_profile
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", controller_name):
            raise DomainError("PROFILE_CONFIGURATION_INVALID", "主控Profile名称无效", 503)
        controller = hermes_home(config)
        if controller_name != "default":
            controller = controller / "profiles" / controller_name
        if (
            tree_hash(controller / "plugins/trading-bridge")
            != expected["plugin_hashes"]["trading-bridge"]
        ):
            raise DomainError("PLUGIN_LOCK_MISMATCH", "主控插件与部署锁不一致", 503)
        controller_config = yaml.safe_load((controller / "config.yaml").read_text(encoding="utf-8"))
        plugins = controller_config.get("plugins", {})
        settings = plugins.get("entries", {}).get("trading-bridge", {}).get("settings", {})
        if "trading-bridge" not in plugins.get("enabled", []) or settings != bridge_settings(
            config
        ):
            raise DomainError("PLUGIN_LOCK_MISMATCH", "主控插件的API地址未绑定本runtime", 503)
        report_path = config.sandbox.isolation_report
        if not config.sandbox.isolation_verified or report_path is None:
            raise DomainError("CUBE_ISOLATION_NOT_VERIFIED", "尚未保存真实Cube隔离验收证据", 503)
        raw = report_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest.get("isolation_report_hash"):
            raise DomainError("CUBE_ISOLATION_NOT_VERIFIED", "隔离验收报告hash与部署锁不一致", 503)
        report = json.loads(raw)
        checks = report.get("checks", {})
        if (
            report.get("kind") != "live"
            or report.get("isolation_config_hash") != content_hash(expected["cube"])
            or report.get("template") != config.sandbox.template
            or report.get("sdk_version") != config.sandbox.sdk_version
            or not REQUIRED_ISOLATION_CHECKS.issubset(checks)
            or any(checks[name] is not True for name in REQUIRED_ISOLATION_CHECKS)
        ):
            raise DomainError("CUBE_ISOLATION_NOT_VERIFIED", "隔离报告不完整或含fixture证据", 503)
        return True

    async def __call__(self, *, cached_model=False):
        try:
            await asyncio.to_thread(self.files)
            if not self.git_checked:
                repository = self.config.hermes.repository
                if repository is None or not repository.is_absolute():
                    return False
                process = await ManagedProcess.start(
                    ["git", "-C", str(repository), "rev-parse", "HEAD"], env=safe_environment()
                )
                try:
                    result = await process.wait(3)
                finally:
                    await process.terminate()
                if (
                    result.returncode != 0
                    or result.stdout.decode("ascii", errors="replace").strip()
                    != self.config.hermes.reference_commit
                ):
                    return False
                process = await ManagedProcess.start(
                    ["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--"],
                    env=safe_environment(),
                )
                try:
                    result = await process.wait(3)
                finally:
                    await process.terminate()
                if result.returncode != 0:
                    return False
                self.git_checked = True
            async with self.database.engine.connect() as connection:
                permissions = await verify_app_permissions(connection)
            if not permissions:
                return False
            if cached_model:
                age = time.monotonic() - self.model_checked_at
                if age >= MODEL_REFRESH_SECONDS:
                    self._schedule_model_refresh(0)
                return self.model_ready and age < MODEL_CACHE_SECONDS
            ready = await self.check_model()
            if ready:
                self._schedule_model_refresh(MODEL_REFRESH_SECONDS)
            return ready
        except (DomainError, ValueError, TypeError, AttributeError, OSError, TimeoutError):
            return False
