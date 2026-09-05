set minimum-version := '1.55.0'
set default-list
set script-interpreter := ['.venv/Scripts/python.exe']

python := '.venv/Scripts/python.exe'

# Create .venv and install the package with dev extras
setup:
    uv venv .venv --python 3.12
    uv pip install --python {{ python }} -e ".[dev]"

# Lint and run the offline test suite
check:
    {{ python }} -m ruff check src tests
    {{ python }} -m pytest tests -q

# One-time nflverse download and board build (cache in ~/.ffdraft)
data:
    {{ python }} setup_data.py

# Run the MCP server on stdio (what Claude launches)
serve:
    {{ python }} -m ffdraft.server

# Spawn the server as a real stdio subprocess, handshake, list tools, call two
[script]
smoke:
    import asyncio
    import os

    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = os.getcwd()
    server = StdioServerParameters(
        command=os.path.join(root, ".venv", "Scripts", "python.exe"),
        args=["-m", "ffdraft.server"],
        env={"FFDRAFT_SEASON": "2026", "PYTHONPATH": os.path.join(root, "src")},
    )

    async def main() -> None:
        async with Client(stdio_client(server)) as client:
            info = client.server_info
            print("server:", info.name if info else None, "protocol:", client.protocol_version)
            tools = (await client.list_tools()).tools
            print(f"tools: {len(tools)}")
            for t in tools:
                print("  ", t.name)
            for name, args in (("list_leagues", {}), ("best_available", {"limit": 5})):
                r = await client.call_tool(name, args)
                print(f"--- {name} is_error={r.is_error}")
                print(r.content[0].text)

    asyncio.run(main())
