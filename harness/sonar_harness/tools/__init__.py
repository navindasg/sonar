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
from sonar_harness.tools.calendar_read import CalendarAgendaTool
from sonar_harness.tools.calendar_write import (
    CalendarCancelTool,
    CalendarCreateTool,
    CalendarRescheduleTool,
)
from sonar_harness.tools.daily_brief import DailyBriefTool
from sonar_harness.tools.gmail_read import GmailSearchTool
from sonar_harness.tools.note_capture import NoteCaptureTool
from sonar_harness.tools.rag_backend import RagBackend
from sonar_harness.tools.rag_tools import RagNoteContextTool, RagSearchTool
from sonar_harness.tools.state_read import StateReadTool
from sonar_harness.tools.todo_add import TodoAddTool
from sonar_harness.tools.todo_done import TodoDoneTool
from sonar_harness.tools.todo_list import TodoListTool
from sonar_harness.tools.web_search import WebSearchTool

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
    "TodoDoneTool",
    "TodoListTool",
    "NoteCaptureTool",
    "GmailSearchTool",
    "CalendarAgendaTool",
    "CalendarCreateTool",
    "CalendarRescheduleTool",
    "CalendarCancelTool",
    "DailyBriefTool",
    "WebSearchTool",
    "default_tools",
]


def default_tools(*, rag_backend: RagBackend, vault_path: str) -> list[ToolBase]:
    """Return the session's built-in tool instances, in registration order.

    ``rag_backend`` is consumed by the two RAG tools (in-process or MCP — a
    config choice, see rag_backend.py). ``vault_path`` is read directly by
    ``todo_list`` (open checkboxes live in the vault, not the DB). The remaining
    tools (``todo_add``, ``state_read``) take their state from ``ctx`` at call
    time, not here.
    """
    return [
        RagSearchTool(backend=rag_backend),
        RagNoteContextTool(backend=rag_backend),
        StateReadTool(),
        TodoAddTool(),
        TodoDoneTool(),
        TodoListTool(vault_path=vault_path),
        NoteCaptureTool(vault_path=vault_path),  # append-only capture into Sonar/
        # External inputs. These read their credentials / config from
        # env + ~/.config/sonar at call time, so they need no constructor args;
        # when unconfigured they return a "not connected" string, not an error.
        GmailSearchTool(),
        CalendarAgendaTool(),
        CalendarCreateTool(),
        CalendarRescheduleTool(),
        CalendarCancelTool(),
        DailyBriefTool(vault_path=vault_path),  # composes agenda + todos (+ email)
        WebSearchTool(),
    ]
