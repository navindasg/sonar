"""Tool framework: ToolBase ABC + ToolRegistry + ToolContext.

Ported from brook37 ``daemon/tools/base.py`` and adapted to the Sonar
contract (see ``harness/CONTRACTS.md`` §1). Each tool is a subclass of
``ToolBase`` that declares an OpenAI/Anthropic-shaped schema and a
``run(args, ctx)`` method. A ``ToolRegistry`` holds a set of tool
instances plus a per-tool permission table (loaded from
``config/tool_permissions.yaml``) and exposes the surface the agent loop
needs: ``schemas_for(ctx)`` and ``dispatch(name, args, ctx)``.

Sonar adaptations vs brook37:
  * Permission tiers collapse from four (admin/dev/read/write) to two:
    ``local`` (auto-run) / ``gated`` (human approval — browser/mutation).
  * No iMessage allowlist. ``ToolContext`` carries the per-turn ``turn_id``,
    the SQLite ``state`` handle, and the step-event ``emit`` sink instead of
    a sender identity.

Attribute completeness is enforced at subclass-creation time so a missing
field fails at import, not at invocation.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Literal, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from sonar_harness.state import State

log = logging.getLogger("sonar.tools")

Permission = Literal["local", "gated"]

_VALID_PERMISSIONS: tuple[Permission, ...] = ("local", "gated")

DEFAULT_PERMISSIONS_PATH = Path("config/tool_permissions.yaml")


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context threaded from the agent loop to each tool.

    ``turn_id`` attributes step-events and telemetry to one user utterance.
    ``state`` is the shared SQLite (WAL) connection tools use for side-effect
    writes. ``emit`` is the step-event sink (CONTRACTS.md §3): a tool MAY push
    additional ``tool``/``tool_result_summary`` events through it, though the
    agent loop already brackets every dispatch with a pair.
    """

    turn_id: str
    state: "State"
    emit: Callable[[dict[str, Any]], None]


class ToolBase(ABC):
    """Base class for all harness-native tools.

    Subclasses define the four class attributes below and implement
    ``run(args, ctx) -> str``. The model sees ``name`` / ``description`` /
    ``input_schema``; ``permission`` gates visibility and dispatch. The model
    never sees ``ctx`` — it cannot spoof turn state.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    permission: ClassVar[Permission]

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        # `__abstractmethods__` isn't populated until after __init_subclass__
        # runs — skip validation for intermediate bases that don't override run.
        if getattr(cls, "run", None) is ToolBase.run:
            return
        for attr in ("name", "description", "input_schema", "permission"):
            if not hasattr(cls, attr):
                raise TypeError(
                    f"{cls.__name__} must define class attribute {attr!r}"
                )
        if cls.permission not in _VALID_PERMISSIONS:
            raise ValueError(
                f"{cls.__name__}.permission must be one of {_VALID_PERMISSIONS}, "
                f"got {cls.permission!r}"
            )
        if not isinstance(cls.name, str) or not cls.name.strip():
            raise ValueError(f"{cls.__name__}.name must be a non-empty string")
        if not isinstance(cls.input_schema, dict):
            raise TypeError(f"{cls.__name__}.input_schema must be a dict")

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Invoke the tool with model-provided args; return a text result.

        Never raise for an expected failure — return an explanatory string the
        model can read and recover from. The registry catches unexpected
        exceptions at dispatch, but tools that talk to a fallible boundary
        (RAG, network) should map failures to text themselves.
        """


class ToolRegistry:
    """Holds tool instances + a per-tool permission table.

    Config permissions override a tool's class-declared ``permission``. Tools
    absent from config fall back to their declared default. The same gate is
    enforced at both visibility (``schemas_for``) and call time (``dispatch``).
    """

    def __init__(
        self,
        tools: Iterable[ToolBase],
        permissions: dict[str, Permission],
    ) -> None:
        self._tools: dict[str, ToolBase] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            self._tools[tool.name] = tool
        self._permissions = dict(permissions)

    @classmethod
    def load(
        cls,
        *,
        tools: Iterable[ToolBase] | None = None,
        config_path: Path | str | None = None,
    ) -> "ToolRegistry":
        """Build a registry, reading permission overrides from YAML if present."""
        path = (
            Path(config_path)
            if config_path is not None
            else DEFAULT_PERMISSIONS_PATH
        )
        permissions: dict[str, Permission] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            if not isinstance(data, dict):
                raise ValueError(
                    f"{path}: expected a mapping of tool -> permission"
                )
            for name, perm in data.items():
                if perm not in _VALID_PERMISSIONS:
                    raise ValueError(
                        f"{path}: tool {name!r} has invalid permission {perm!r} "
                        f"(must be one of {_VALID_PERMISSIONS})"
                    )
                permissions[str(name)] = perm
            log.info(
                "tool permissions loaded: %d entries from %s",
                len(permissions),
                path,
            )
        else:
            log.warning(
                "tool permissions config missing at %s; using tool defaults", path
            )
        return cls(tools=tools or (), permissions=permissions)

    def _permission_for(self, tool_name: str) -> Permission:
        if tool_name in self._permissions:
            return self._permissions[tool_name]
        return self._tools[tool_name].permission

    def _is_visible(self, tool_name: str) -> bool:
        # A synchronous voice turn cannot satisfy a `gated` approval, so gated
        # tools are hidden from the model and refused at dispatch until the
        # approval seam lands. Every tool in this pass is `local`.
        return self._permission_for(tool_name) == "local"

    def schemas_for(self, ctx: ToolContext | None = None) -> list[dict[str, Any]]:
        """Return ``[{name, description, input_schema}, ...]`` for the model.

        ``ctx`` is accepted for forward compatibility (a future gate may be
        per-turn); today visibility depends only on the permission tier.
        """
        del ctx  # not consulted yet; gate is permission-only in this pass
        out: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if not self._is_visible(name):
                continue
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            )
        return out

    def dispatch(
        self, name: str, args: dict[str, Any], ctx: ToolContext
    ) -> str:
        """Invoke a tool by name.

        Raises ``KeyError`` for an unknown tool and ``PermissionError`` for a
        gated (unapproved) tool — the same gate as visibility.
        """
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name!r}")
        if not self._is_visible(name):
            raise PermissionError(
                f"tool {name!r} is gated and requires human approval"
            )
        return self._tools[name].run(args, ctx)

    def names(self) -> list[str]:
        return list(self._tools)
