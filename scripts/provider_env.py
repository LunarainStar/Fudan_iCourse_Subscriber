#!/usr/bin/env python3
"""Print ``export VAR=...`` lines for every env var that the configured
model providers need (``api_key_env`` / ``base_url_env`` from
``config.MODEL_PROVIDERS``) plus anything listed in
``EXTRA_SECRET_ENV_VARS``, with values taken from the SECRETS_CONTEXT
JSON the workflow passes in via ``${{ toJSON(secrets) }}``.

This lets users add custom providers with custom env-var names in
``src/runtime/config.py`` without touching the workflow YAML, where
secrets must otherwise be referenced one by one.  Usage in a workflow
step (unset the context before launching the app so the full secrets
blob never reaches the Python process):

    env:
      SECRETS_CONTEXT: ${{ toJSON(secrets) }}
    run: |
      eval "$(python scripts/provider_env.py)"
      unset SECRETS_CONTEXT
      python -u main.py
"""

from __future__ import annotations

import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime.config import MODEL_PROVIDERS

# Secrets that are not model-provider keys but are still consumed by
# main.py. They are exported from here rather than listed in the workflow
# YAML on purpose: editing .github/workflows/*.yml makes GitHub flag the
# workflow as possibly-malicious and hold every run (schedule included)
# at `action_required` until a human approves it.
EXTRA_SECRET_ENV_VARS = ("KDOCS_TOKEN",)


def main():
    try:
        ctx = json.loads(os.environ.get("SECRETS_CONTEXT") or "{}")
    except ValueError:
        print("echo '::warning::SECRETS_CONTEXT is not valid JSON'")
        return

    # Secret names are case-insensitive on the GitHub side; normalise.
    lookup = {str(k).upper(): v for k, v in ctx.items() if v}

    emitted: set[str] = set()
    for provider in MODEL_PROVIDERS:
        for field in ("api_key_env", "base_url_env"):
            name = provider.get(field)
            if not name or name in emitted:
                continue
            emitted.add(name)
            value = lookup.get(name.upper())
            if value:
                print(f"export {name}={shlex.quote(str(value))}")

    for name in EXTRA_SECRET_ENV_VARS:
        if name in emitted:
            continue
        emitted.add(name)
        value = lookup.get(name.upper())
        if value:
            print(f"export {name}={shlex.quote(str(value))}")


if __name__ == "__main__":
    main()
