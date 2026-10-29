"""Lets the LangGraph agent call the MCP tools. Every call is written to the audit log."""
import json
import os
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import db


@asynccontextmanager
async def mcp_session(incident_id=None):
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, args: dict) -> dict:
                db.audit(incident_id, "agent", "tool_call", {"tool": name, "args": args})
                res = await session.call_tool(name, args)
                text = "".join(getattr(c, "text", "") for c in res.content)
                if res.isError:
                    return {"error": text}
                try:
                    return json.loads(text)
                except ValueError:
                    return {"raw": text}

            yield call
