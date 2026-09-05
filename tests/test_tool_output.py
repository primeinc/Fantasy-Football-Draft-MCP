"""Every tool answers as text, never as structured output.

A tool that returns `str` gets an output schema of `{"result": <str>}` from the
SDK by default, and Claude Code renders that wrapper: one line of escaped JSON
with `\n` where the newlines were. `structured_output=False` on every tool
keeps the answer a text block.
"""
from ffdraft import server


def test_no_tool_declares_an_output_schema():
    tools = server.mcp._tool_manager.list_tools()
    assert len(tools) >= 50
    with_schema = sorted(t.name for t in tools if getattr(t, "output_schema", None))
    assert with_schema == []
