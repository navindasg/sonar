"""Agent tool loop: the final synthesis escalates to the reasoning model.

Uses a scripted fake Ollama (no network) so the escalation policy and the
tool-dispatch path are locked without a live model.
"""

from __future__ import annotations

from sonar_harness.agent import run_turn
from sonar_harness.events import EventSink
from sonar_harness.model_router import ModelsConfig
from sonar_harness.tools.base import ToolBase, ToolRegistry

FAST = "gemma4:e4b-mlx"
REASON = "gemma4:26b-mlx"


def _models(escalate: bool = True) -> ModelsConfig:
    return ModelsConfig(
        default="fast",
        escalation="reason",
        aliases={"fast": FAST, "reason": REASON},
        escalate_synthesis_after_tools=escalate,
    )


class _EchoSearch(ToolBase):
    name = "rag.search"
    description = "search notes"
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    permission = "local"

    def run(self, args, ctx):  # noqa: ANN001 - test tool
        return "RESULT: the WSN pipeline is three-tier, deployed on Kubernetes."


class _FakeOllama:
    """Returns scripted assistant messages in order; records model per call."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.models_called: list[str] = []

    def chat(self, model, messages, tools=None, *, temperature=0.0):  # noqa: ANN001
        self.models_called.append(model)
        return self._scripted.pop(0)


def _run(fake: _FakeOllama, models: ModelsConfig):
    return run_turn(
        inbound_messages=[{"role": "user", "content": "what does my WSN note say?"}],
        charter="You are Sonar.",
        registry=ToolRegistry(tools=[_EchoSearch()], permissions={}),
        ollama=fake,
        models=models,
        state=None,
        events=EventSink(),
    )


_TOOLCALL = {
    "content": "",
    "tool_calls": [
        {"function": {"name": "rag.search", "arguments": {"query": "WSN"}}}
    ],
}
_WEAK_FINAL = {"content": "I don't have specific details.", "tool_calls": []}
_STRONG_FINAL = {"content": "It's a three-tier pipeline on Kubernetes.", "tool_calls": []}


def test_synthesis_escalates_to_reason_after_tools():
    fake = _FakeOllama([_TOOLCALL, _WEAK_FINAL, _STRONG_FINAL])
    result = _run(fake, _models(escalate=True))
    # fast selects the tool, fast drafts a weak final, then reason re-synthesizes.
    assert fake.models_called == [FAST, FAST, REASON]
    assert result.model == REASON
    assert result.text == "It's a three-tier pipeline on Kubernetes."
    assert result.tool_calls == 1


def test_no_escalation_when_flag_off():
    fake = _FakeOllama([_TOOLCALL, _WEAK_FINAL])
    result = _run(fake, _models(escalate=False))
    assert fake.models_called == [FAST, FAST]
    assert result.model == FAST
    assert result.text == "I don't have specific details."


def test_chitchat_stays_on_fast_model():
    fake = _FakeOllama([{"content": "Hey! How can I help?", "tool_calls": []}])
    result = _run(fake, _models(escalate=True))
    assert fake.models_called == [FAST]
    assert result.model == FAST
    assert result.tool_calls == 0
