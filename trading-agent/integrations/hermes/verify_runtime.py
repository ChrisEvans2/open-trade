"""Pinned Hermes credential preflight. Outputs booleans only; never calls a model."""

import contextlib
import io
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def verify(repository, provider, model):
    sys.path.insert(0, repository)
    logging.disable(logging.CRITICAL)
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.runtime_provider import expand_direct_api_alias, resolve_runtime_provider

    home = Path(os.environ["HERMES_HOME"])
    scope = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        expected, _ = expand_direct_api_alias(provider, None)
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        if runtime.get("provider") != expected:
            return False
        if runtime.get("api_key"):
            return True
        url = urlsplit(runtime.get("base_url") or "")
        return url.hostname in {"localhost", "127.0.0.1", "::1"}
    finally:
        reset_secret_scope(scope)


if __name__ == "__main__":
    allowed = {
        "PATH",
        "HOME",
        "HERMES_HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "PYTHONNOUSERSITE",
    }
    clean = {key: value for key, value in os.environ.items() if key in allowed}
    os.environ.clear()
    os.environ.update(clean)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            configured = verify(*sys.argv[1:4])
    except Exception:
        configured = False
    print(json.dumps({"configured": configured}))
    raise SystemExit(0 if configured else 1)
