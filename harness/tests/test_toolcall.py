"""Tool-call extraction: native primary, XML-heal fallback, plain-answer none."""

from __future__ import annotations

from sonar_harness.toolcall import extract_tool_calls

TOOLS = frozenset({"rag.search", "rag.note_context"})


def test_native_tool_calls_take_precedence():
    msg = {
        "content": "",
        "tool_calls": [
            {"function": {"name": "rag.search", "arguments": {"query": "wsn"}}}
        ],
    }
    calls, via = extract_tool_calls(msg, TOOLS)
    assert via == "native"
    assert [(c.name, c.args) for c in calls] == [("rag.search", {"query": "wsn"})]


def test_native_arguments_as_json_string_are_coerced():
    msg = {
        "tool_calls": [
            {"function": {"name": "rag.search", "arguments": '{"query": "x"}'}}
        ]
    }
    calls, via = extract_tool_calls(msg, TOOLS)
    assert via == "native"
    assert calls[0].args == {"query": "x"}


def test_xml_heal_recovers_tool_code_fence():
    msg = {"content": '```tool_code\nrag.search(query="three tier")\n```'}
    calls, via = extract_tool_calls(msg, TOOLS)
    assert via == "xml_heal"
    assert calls[0].name == "rag.search"
    assert calls[0].args == {"query": "three tier"}


def test_xml_heal_recovers_tool_call_json_tag():
    msg = {
        "content": '<tool_call>{"name": "rag.note_context", '
        '"arguments": {"path": "a.md"}}</tool_call>'
    }
    calls, via = extract_tool_calls(msg, TOOLS)
    assert via == "xml_heal"
    assert calls[0].name == "rag.note_context"
    assert calls[0].args == {"path": "a.md"}


def test_plain_answer_is_no_call():
    msg = {"content": "The architecture is three-tier."}
    calls, via = extract_tool_calls(msg, TOOLS)
    assert calls == []
    assert via == "none"
