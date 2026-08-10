"""
Vercel entrypoint (root level).

Vercel's Python runtime auto-detects an entrypoint by filename, checking the
project root FIRST: app.py, index.py, server.py, main.py, wsgi.py, asgi.py --
then the same names inside src/, app/ and api/.

Because src/server.py matched that list before this file existed, Vercel loaded
src/server.py directly. That path never sets MCP_TRANSPORT, so the module took
the stdio branch and exported no ASGI `app` at all -- the function built and
booted but served nothing, returning 404 on every route including /healthz.

This file sits at the root, so it wins detection outright and there is no
ambiguity about which module Vercel loads.

MCP_TRANSPORT must be set BEFORE importing src.server -- the shared-secret
guard runs at import time.
"""

import os
import sys
from pathlib import Path

os.environ["MCP_TRANSPORT"] = "http"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.server import mcp  # noqa: E402

app = mcp.streamable_http_app()
