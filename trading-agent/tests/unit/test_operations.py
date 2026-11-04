import json
import logging
import queue

import pytest

from trading_agent.cli import parser
from trading_agent.runtime.logging import BoundedQueueHandler, SafeJSON


def test_safe_stack_retains_frames_without_exception_text():
    import sys

    try:
        raise RuntimeError("fixture-password-in-error")
    except RuntimeError:
        record = logging.LogRecord(
            "trading_agent.api",
            logging.ERROR,
            "/fixture/secret",
            1,
            "api_request_failed",
            (),
            sys.exc_info(),
        )
    record.duration_ms = 2.5
    record.route = "/v1/forecasts/{ident}"
    channel = queue.Queue(1)
    BoundedQueueHandler(channel).handle(record)
    decoded = json.loads(SafeJSON().format(channel.get_nowait()))
    assert decoded["exception_type"] == "RuntimeError"
    assert decoded["stack_frames"] == ["test_safe_stack_retains_frames_without_exception_text"]
    assert decoded["duration_ms"] == 2.5
    assert "fixture-password" not in str(decoded) and "/fixture/secret" not in str(decoded)


def test_log_disk_failure_is_visible_and_never_prints_payload(tmp_path, monkeypatch, capsys):
    from trading_agent.runtime import logging as module

    class DiskFull:
        def write(self, value):
            raise OSError("fixture-password-disk-full")

        def flush(self):
            return

        def close(self):
            return

    sink = module.GuardedFileHandler(tmp_path / "log.jsonl", maxBytes=1000, backupCount=1)
    sink.stream.close()
    sink.stream = DiskFull()
    handler = BoundedQueueHandler(queue.Queue(1))
    monkeypatch.setattr(module, "_logging_status", (handler, sink))
    record = logging.LogRecord(
        "trading_agent.test", logging.ERROR, "", 1, "runtime_error", (), None
    )
    sink.handle(record)
    assert module.logging_status() == {"healthy": False, "dropped_logs": 0, "write_failures": 1}
    assert "fixture-password" not in str(capsys.readouterr())
    sink.close()


def test_log_queue_removes_secrets_and_preserves_stable_identifiers():
    channel = queue.Queue(1)
    handler = BoundedQueueHandler(channel)
    record = logging.LogRecord(
        "sqlalchemy",
        logging.ERROR,
        "/secret/path",
        99,
        "database password=fixture-secret",
        (),
        None,
    )
    record.request_id = "request-safe"
    record.payload = {"operator": "fixture-operator-token"}
    record.safe_stack = {"secret": "fixture-operator-token"}
    handler.handle(record)
    handler.handle(record)
    assert handler.dropped == 1
    text = SafeJSON().format(channel.get_nowait())
    assert "fixture-secret" not in text and "fixture-operator-token" not in text
    assert "secret/path" not in text
    assert json.loads(text)["request_id"] == "request-safe"
    record.msg = "runtime_loop_retry"
    record.error_code = "DATABASE_UNAVAILABLE"
    handler.handle(record)
    decoded = json.loads(SafeJSON().format(channel.get_nowait()))
    assert decoded["event"] == "runtime_loop_retry"
    assert decoded["error_code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.parametrize(
    "words",
    [
        ["status"],
        ["forecast", "latest"],
        ["schedule", "pause"],
        ["config", "check"],
        ["doctor", "--notify"],
        ["review", "show", "00000000-0000-0000-0000-000000000001"],
    ],
)
def test_cli_json_flag_works_after_subcommand(words):
    assert parser().parse_args([*words, "--json"]).json is True


def test_cli_json_argument_error_does_not_echo_invalid_value(monkeypatch, capsys):
    import sys

    words = ["forecast", "show", "fixture-sensitive-invalid-id", "--json"]
    monkeypatch.setattr(sys, "argv", ["ta", *words])
    with pytest.raises(SystemExit) as caught:
        parser().parse_args(words)
    output = capsys.readouterr()
    assert caught.value.code == 2
    assert json.loads(output.out)["error"]["code"] == "CLI_ARGUMENT_INVALID"
    assert "fixture-sensitive" not in output.out + output.err


@pytest.mark.parametrize(
    "name",
    [
        "TA_OPERATOR_TOKEN",
        "TA_CONTROLLER_TOKEN",
        "TA_MIGRATION_DATABASE_URL",
        "TA_BACKUP_DATABASE_URL",
        "TA_RESTORE_DATABASE_URL",
    ],
)
def test_runtime_refuses_human_credentials_before_constructing_dependencies(monkeypatch, name):
    from trading_agent.config import AppConfig
    from trading_agent.domain.errors import DomainError
    from trading_agent.runtime.factory import build_runtime

    monkeypatch.setenv(name, "fixture-human-secret")
    with pytest.raises(DomainError) as caught:
        build_runtime(AppConfig())
    assert caught.value.code == "RUNTIME_SECRET_SCOPE" and "fixture-human-secret" not in str(
        caught.value
    )
