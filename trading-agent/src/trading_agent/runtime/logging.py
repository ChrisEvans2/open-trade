import json
import logging
import logging.handlers
import math
import queue
import re
import time
import traceback
from datetime import UTC, datetime

IDENTIFIERS = (
    "job_id",
    "attempt_id",
    "request_id",
    "event_id",
    "runtime_instance_id",
    "forecast_id",
    "snapshot_id",
    "bundle_id",
    "child_id",
    "agent_session_id",
    "outbox_id",
    "claim_id",
    "error_code",
    "stage",
    "kind",
    "timeframe",
    "engine_hash",
    "input_hash",
    "result_hash",
    "refresh_reason",
    "delivery_status",
    "cleanup_status",
    "actor_type",
)
NUMBERS = (
    "fence",
    "duration_ms",
    "status_code",
    "attempt_no",
    "output_bytes",
    "cache_hits",
    "api_calls",
    "input_tokens",
    "output_tokens",
    "timeout_seconds",
)
_logging_status = None


def attempt_event(event, attempt, *, level=logging.INFO, **values):
    logging.getLogger("trading_agent.execution").log(
        level,
        event,
        extra={
            "runtime_instance_id": str(attempt.runtime_instance_id),
            "job_id": str(attempt.job_id),
            "attempt_id": str(attempt.id),
            "request_id": str(attempt.id),
            "fence": attempt.fence,
            "kind": attempt.kind,
            **values,
        },
    )


class SafeJSON(logging.Formatter):
    def format(self, record):
        event = record.msg if isinstance(record.msg, str) else "external_event"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", event):
            event = "external_event"
        value = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "component": record.name
            if re.fullmatch(r"trading_agent\.[a-zA-Z0-9_.]{1,100}", record.name)
            else "external",
            "event": event,
            "message": event,
        }
        for key in IDENTIFIERS:
            item = getattr(record, key, None)
            if isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", item):
                value[key] = item
        for key in NUMBERS:
            item = getattr(record, key, None)
            if type(item) in {int, float} and math.isfinite(item) and 0 <= item <= 10**12:
                value[key] = item
        route = getattr(record, "route", None)
        if isinstance(route, str) and re.fullmatch(r"/[a-zA-Z0-9_/{}/-]{1,160}", route):
            value["route"] = route
        if record.exc_info:
            # Retain control-flow frames, never exception text, local variables or source lines.
            value["exception_type"] = record.exc_info[0].__name__[:80]
            value["stack_frames"] = [
                frame.name[:80] for frame in traceback.extract_tb(record.exc_info[2])[-12:]
            ]
        elif hasattr(record, "safe_stack"):
            stack = record.safe_stack
            if isinstance(stack, dict):
                name = stack.get("exception_type")
                if isinstance(name, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,79}", name):
                    value["exception_type"] = name
                frames = stack.get("stack_frames")
                if isinstance(frames, list) and all(
                    isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9_<>{}-]{1,80}", item)
                    for item in frames
                ):
                    value["stack_frames"] = frames[-12:]
        # Exclude exception text and arbitrary extras, including URLs and payloads.
        return json.dumps(value, ensure_ascii=False)


class BoundedQueueHandler(logging.handlers.QueueHandler):
    def __init__(self, channel):
        super().__init__(channel)
        self.dropped = 0

    def prepare(self, record):
        safe = json.loads(SafeJSON().format(record))
        prepared = logging.LogRecord(record.name, record.levelno, "", 0, safe["event"], (), None)
        prepared.created = record.created
        for key in (*IDENTIFIERS, *NUMBERS, "route"):
            if key in safe:
                setattr(prepared, key, safe[key])
        prepared.safe_stack = {
            key: safe[key] for key in ("exception_type", "stack_frames") if key in safe
        }
        return prepared

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


class GuardedFileHandler(logging.handlers.RotatingFileHandler):
    failures = 0

    def handleError(self, record):
        self.failures += 1  # Exposed by readyz; never print failed records or recurse into logging.


class BoundedListener(logging.handlers.QueueListener):
    def stop(self):
        thread = self._thread
        if thread is None:
            return
        try:
            self.enqueue_sentinel()
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.enqueue_sentinel()
        thread.join(timeout=3)
        if not thread.is_alive():
            self._thread = None
            for handler in self.handlers:
                handler.close()


def logging_status():
    if _logging_status is None:
        return {"healthy": True, "dropped_logs": 0, "write_failures": 0}
    handler, sink = _logging_status
    return {
        "healthy": sink.failures == 0,
        "dropped_logs": handler.dropped,
        "write_failures": sink.failures,
    }


def configure_logging(config):
    global _logging_status
    folder = config.data_dir.resolve() / "logs"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    for path in folder.glob("runtime.jsonl.*"):
        if path.is_file() and path.stat().st_mtime < time.time() - 30 * 86400:
            path.unlink()
    sink = GuardedFileHandler(
        folder / "runtime.jsonl",
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    sink.setFormatter(SafeJSON())
    (folder / "runtime.jsonl").chmod(0o600)
    channel = queue.Queue(config.log_queue_size)
    handler = BoundedQueueHandler(channel)
    # Sanitize before records cross the queue, not just when writing the file.
    handler.setFormatter(SafeJSON())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    _logging_status = (handler, sink)
    listener = BoundedListener(channel, sink)
    listener.start()
    return listener
