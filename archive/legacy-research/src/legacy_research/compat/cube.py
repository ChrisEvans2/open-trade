from __future__ import annotations

import asyncio
import shlex
from types import SimpleNamespace
from uuid import UUID

from legacy_research.compat.iteration import TestResult
from trading_agent.domain.errors import DomainError
from trading_agent.domain.serialization import content_hash
from trading_agent.runtime.deployment import DeploymentCheck
from trading_agent.runtime.sandboxes import CubeResources


class CompatResources(CubeResources):
    """Standalone compatibility journal; never writes a forecast attempt or business DB."""

    def __init__(self, config, key, journal):
        super().__init__(config, key)
        self.journal = journal

    def metadata(self, attempt):
        return {
            **super().metadata(attempt),
            "project": "personal-trading-agent-legacy-compat",
        }

    async def _record(self, attempt, values, *, expired=False):
        self.journal.append("CUBE_LIFECYCLE", resource=values)

    async def cleanup(self, attempt):
        raw = self.records.pop(attempt.id, None)
        if raw is None:
            return True
        cleaned = await self._destroy(raw)
        self.journal.append(
            "CUBE_CLEANED" if cleaned else "CUBE_CLEANUP_PENDING", sandbox_id=raw["sandboxID"]
        )
        return cleaned


class CubeVerifier:
    def __init__(self, config, key):
        self.config, self.key = config, key

    async def run(self, files, policy, journal):
        # Reuse the complete v1 deployment/isolation checks, with no host execution fallback.
        try:
            await asyncio.to_thread(DeploymentCheck(self.config, None).files)
        except DomainError:
            raise
        except (ValueError, OSError):
            raise DomainError(
                "LEGACY_DEPLOYMENT_INCOMPLETE", "真实部署和隔离配置尚未完成，拒绝启动策略测试", 503
            ) from None
        ident = UUID(journal.root.name)
        attempt = SimpleNamespace(id=ident, job_id=ident, fence=1, kind="FORECAST")
        resources = CompatResources(self.config, self.key, journal)
        folder = journal.root / "input"
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / "context.json").write_text('{"candidate_hash":"' + content_hash(files) + '"}')
        (folder / "task.txt").write_text("仅运行冻结的策略候选测试；不启动交易程序。")
        try:
            await resources.prepare(attempt, journal.root)
            raw = resources.records[ident]
            return await asyncio.to_thread(self._test, resources, raw, files, policy)
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "LEGACY_TEST_UNCONFIRMED", "未能确认沙箱测试结果，拒绝写回", 503
            ) from None
        finally:
            try:
                if not await resources.cleanup(attempt):
                    raise DomainError(
                        "CUBE_CLEANUP_PENDING", "策略测试沙箱尚未确认回收，拒绝写回", 503
                    )
            finally:
                await resources.http.aclose()
                (journal.root / ".cube.secrets.json").unlink(missing_ok=True)

    @staticmethod
    def _test(resources, raw, files, policy):
        from cubesandbox import Config, Sandbox

        sandbox = Sandbox(raw, Config(**resources.sdk_settings))
        root = "/workspace/scratch/legacy"
        user = resources.config.sandbox.execution_user
        command = (
            policy.test_python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_asyncio.plugin",
            "tests",
        )
        try:
            directories = {root}
            for name in files:
                parts = name.split("/")[:-1]
                for size in range(1, len(parts) + 1):
                    directories.add(root + "/" + "/".join(parts[:size]))
            setup = sandbox.commands.run(
                "mkdir -p " + " ".join(shlex.quote(path) for path in sorted(directories)),
                user="root",
                timeout=5,
            )
            if setup.exit_code:
                raise DomainError("LEGACY_TEST_SETUP_FAILED", "候选测试目录创建失败", 503)
            for name, data in files.items():
                sandbox.files.write(root + "/" + name, data, user="root")
            readonly = sandbox.commands.run(
                "chown -R root:root " + root + "; chmod -R a-w,a+rX " + root,
                user="root",
                timeout=5,
            )
            if readonly.exit_code:
                raise DomainError(
                    "LEGACY_TEST_SETUP_FAILED", "冻结的候选源码和原有测试未能设为只读", 503
                )
            invocation = (
                "ulimit -u " + str(resources.config.sandbox.max_processes) + "; "
                "env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 "
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
                "PYTHONPATH="
                + shlex.quote(root + "/src:" + root + "/packages/trading-quant/src")
                + " "
                + shlex.join(command)
            )
            result = sandbox.commands.run(
                invocation,
                cwd=root,
                user=user,
                timeout=policy.test_timeout_seconds,
            )
            if type(result.exit_code) is not int:
                raise DomainError("LEGACY_TEST_UNCONFIRMED", "沙箱没有返回可信测试退出码", 503)
            return TestResult(
                result.exit_code,
                (result.stdout + "\n" + result.stderr)[:16000],
                command,
                raw["sandboxID"],
            )
        finally:
            sandbox.close()
