#!/usr/bin/env python3
"""Tiny MCP server providing an end_interview tool.

When Claude calls end_interview, this server writes a sentinel file that
swarm.py polls for. swarm.py then sends SIGINT to Claude Code, ending the
interactive session automatically.
"""

import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("interview")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="end_interview",
            description=(
                "End the interview session. Call this after "
                "PROJECT.md has been written."
            ),
            inputSchema={"type": "object", "properties": {}},
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "end_interview":
        raise ValueError(f"Unknown tool: {name}")

    sentinel = os.environ.get("INTERVIEW_SENTINEL", "")
    if sentinel:
        with open(sentinel, "w") as f:
            f.write("done")

    return CallToolResult(
        content=[TextContent(type="text", text="Interview complete. Session ending.")]
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
