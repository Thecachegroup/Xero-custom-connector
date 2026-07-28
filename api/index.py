"""
Vercel serverless entrypoint.

Vercel's Python runtime looks for an ASGI application called `app` in this file
and routes every request to it. `vercel.json` rewrites all paths here so the
MCP endpoint at /mcp/<secret> and the /healthz probe both land on this handler.

The server runs stateless (stateless_http=True) because serverless invocations
share nothing between calls - there is no in-process session to hold onto.

For a persistent host (Render, Fly) use `python -m src.server` instead; that
path is still supported and is the better fit for full-financial-year payroll
pulls, which can outlast a serverless function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.server import mcp  # noqa: E402

app = mcp.streamable_http_app()
