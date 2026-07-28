"""
Vercel serverless entrypoint.

Vercel's Python runtime looks for an ASGI application called `app` here and
routes every request to it. `vercel.json` rewrites all paths to this handler, so
both /healthz and /mcp/<secret> land here.

SECURITY - why MCP_TRANSPORT is forced below.

This file only ever runs in a web context. If MCP_TRANSPORT were left unset the
server would fall back to stdio defaults and publish an UNPROTECTED /mcp
endpoint, exposing the whole ledger to anyone who guessed the domain. Forcing
http here means the shared-secret guard in src.server always applies and the
function refuses to boot without a secret. It must be set before the import.
"""

import os
import sys
from pathlib import Path

# Must come before importing src.server - the guard runs at import time.
os.environ["MCP_TRANSPORT"] = "http"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.server import mcp  # noqa: E402

app = mcp.streamable_http_app()
