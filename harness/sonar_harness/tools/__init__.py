"""Tool framework + built-in tool registration.

Import ``ToolBase`` / ``ToolRegistry`` / ``ToolContext`` for the framework, and
call ``default_tools(...)`` for the built-in instances. Mirrors brook37's
``daemon/tools/__init__.py::default_tools()`` pattern: a module builds the
default list; ``ToolRegistry.load()`` wraps it with config permissions.
"""

from __future__ import annotations

from sonar_harness.tools.base import (
    Permission,
    ToolBase,
    ToolContext,
    ToolRegistry,
)
from sonar_harness.tools.rag_backend import RagBackend
from sonar_harness.tools.rag_tools import RagNoteContextTool, RagSearchTool
from sonar_harness.tools.state_read import StateReadTool
from sonar_harness.tools.todo_add import TodoAddTool

__all__ = [
    "Permission",
    "ToolBase",
    "ToolContext",
    "ToolRegistry",
    "RagBackend",
    "RagSearchTool",
    "RagNoteContextTool",
    "StateReadTool",
    "TodoAddTool",
    "default_tools",
]


def default_tools(*, rag_backend: RagBackend) -> list[ToolBase]:
    """Return the session's built-in tool instances, in registration order.

    ``rag_backend`` is consumed by the two RAG tools (in-process or MCP — a
    config choice, see rag_backend.py). The stub tools (``todo_add``,
    ``state_read``) take their state from ``ctx`` at call time, not here.
    """
    return [
        RagSearchTool(backend=rag_backend),
        RagNoteContextTool(backend=rag_backend),
        StateReadTool(),
        TodoAddTool(),
    ]
