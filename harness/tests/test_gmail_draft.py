"""Unit tests for gmail.draft — the DRAFT-ONLY email tool.

Two jobs here. The ordinary one: the pure builders (recipient parsing, MIME/raw
body) and the graceful-degradation paths (bad address, missing scope, not
connected, API failure) all return model-safe text instead of raising.

The load-bearing one: proving this tool **cannot send mail**. The fakes below
raise ``AssertionError`` the instant anything reaches ``drafts().send()`` or
``users().messages()``, and ``test_module_never_references_a_send_call`` scans
the module source, so a future edit that adds a send path fails the suite rather
than quietly shipping. Nothing here touches the real Gmail API.
"""
from __future__ import annotations

import ast
import base64
import json
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

import pytest

from sonar_harness.google_auth import (
    CALENDAR_EVENTS_SCOPE,
    CONSENT_SCOPES,
    DEFAULT_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_DRAFT_CAPABLE_SCOPES,
    GMAIL_READONLY_SCOPE,
    GoogleAuthError,
    granted_scopes,
    load_credentials,
    load_scopes,
    require_any_scope,
)
from sonar_harness.tools import gmail_draft as gmail_draft_module
from sonar_harness.tools.gmail_draft import (
    GmailDraftTool,
    build_draft_body,
    parse_recipients,
)
from sonar_harness.tools.base import ToolContext


def _ctx(events: list[dict] | None = None) -> ToolContext:
    sink = events if events is not None else []
    return ToolContext(turn_id="t", state=None, emit=sink.append)


def _decode(body: dict) -> EmailMessage:
    """Round-trip a drafts.create body back into a parsed email message."""
    raw = body["message"]["raw"]
    return message_from_bytes(
        base64.urlsafe_b64decode(raw.encode("ascii")), policy=default_policy
    )


# ---- fake Gmail service -------------------------------------------------------
#
# Every send-capable entry point is a landmine: if the tool ever grows a path to
# one, these raise instead of pretending to succeed.

class _FakeExecutable:
    def __init__(self, result: dict | None, error: Exception | None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> dict:
        if self._error is not None:
            raise self._error
        return self._result or {}


class _FakeDrafts:
    def __init__(self, *, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or {"id": "r-1", "message": {"id": "m-1", "threadId": "th-1"}}
        self._error = error

    def create(self, **kwargs) -> _FakeExecutable:
        self.calls.append(kwargs)
        return _FakeExecutable(self._result, self._error)

    def send(self, **kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("gmail.draft must never call drafts().send()")


class _FakeUsers:
    def __init__(self, drafts: _FakeDrafts) -> None:
        self._drafts = drafts

    def drafts(self) -> _FakeDrafts:
        return self._drafts

    def messages(self):  # pragma: no cover - reaching this is the failure
        raise AssertionError("gmail.draft must never touch users().messages()")


class _FakeGmail:
    def __init__(self, drafts: _FakeDrafts) -> None:
        self._users = _FakeUsers(drafts)

    def users(self) -> _FakeUsers:
        return self._users


def _connect(monkeypatch, drafts: _FakeDrafts, *, scopes: tuple[str, ...] = (GMAIL_COMPOSE_SCOPE,)) -> None:
    """Wire the tool to a fake service with a fake, draft-capable grant."""
    monkeypatch.setattr(
        gmail_draft_module, "build_service", lambda *_a, **_k: _FakeGmail(drafts)
    )
    monkeypatch.setattr(
        gmail_draft_module, "require_any_scope", lambda *_a, **_k: None
    )
    del scopes  # grant is asserted separately via require_any_scope's own tests


# ---- recipient parsing --------------------------------------------------------

def test_parse_recipients_accepts_string_list_and_display_names() -> None:
    assert parse_recipients("alice@example.com", field="to") == ("alice@example.com",)
    assert parse_recipients(
        "alice@example.com, Bob Jones <bob@example.com>", field="to"
    ) == ("alice@example.com", "Bob Jones <bob@example.com>")
    assert parse_recipients(
        ["alice@example.com", " bob@example.com "], field="to"
    ) == ("alice@example.com", "bob@example.com")


def test_parse_recipients_rejects_malformed_address() -> None:
    with pytest.raises(ValueError) as exc:
        parse_recipients("thayer at example dot com", field="to")
    assert "thayer" in str(exc.value)  # the bad value is named, so the model can fix it
    with pytest.raises(ValueError):
        parse_recipients("alice@example.com, broken@", field="to")
    with pytest.raises(ValueError):
        parse_recipients(42, field="to")


def test_parse_recipients_names_the_original_text_when_parsing_yields_nothing() -> None:
    # An unparseable value can leave getaddresses with an empty display AND an
    # empty address; quoting '' back at the model tells it nothing it can fix.
    with pytest.raises(ValueError) as exc:
        parse_recipients("<<>>", field="to")
    assert "<<>>" in str(exc.value)


def test_parse_recipients_tolerates_semicolon_separators() -> None:
    # Models emit "a@x.com; b@y.com" often enough that hard-failing it is a
    # self-inflicted wound; RFC 5322 only uses ';' to close a group, which we
    # don't support anyway.
    assert parse_recipients("a@example.com; b@example.com", field="to") == (
        "a@example.com",
        "b@example.com",
    )


def test_parse_recipients_keeps_a_semicolon_inside_a_quoted_display_name() -> None:
    # ...but never at the cost of rewriting quoted text the user actually gave.
    assert parse_recipients('"Jones; Bob" <bob@example.com>', field="to") == (
        '"Jones; Bob" <bob@example.com>',
    )


def test_parse_recipients_empty_is_empty_not_an_error() -> None:
    assert parse_recipients(None, field="cc") == ()
    assert parse_recipients("  ", field="cc") == ()
    assert parse_recipients([], field="cc") == ()


# ---- draft body builder -------------------------------------------------------

_ARGS = {
    "to": "thayer@example.com",
    "subject": "Re: the deck",
    "body": "I'll have it Friday.",
}


def test_build_draft_body_encodes_headers_and_body() -> None:
    body = build_draft_body(_ARGS)
    msg = _decode(body)
    assert msg["To"] == "thayer@example.com"
    assert msg["Subject"] == "Re: the deck"
    assert "I'll have it Friday." in msg.get_content()
    assert "threadId" not in body["message"]  # no thread asked for -> none set


def test_build_draft_body_includes_cc_and_thread_id() -> None:
    body = build_draft_body({**_ARGS, "cc": "boss@example.com", "thread_id": " th-9 "})
    assert body["message"]["threadId"] == "th-9"
    assert _decode(body)["Cc"] == "boss@example.com"


def test_build_draft_body_keeps_every_recipient_in_the_to_header() -> None:
    # parse_recipients returning all of them is only half the job — the header
    # has to carry all of them too. A draft that silently loses the second
    # recipient is the kind of thing a user skims past before pressing Send.
    msg = _decode(
        build_draft_body(
            {**_ARGS, "to": "alice@example.com, Bob Jones <bob@example.com>"}
        )
    )
    assert str(msg["To"]) == "alice@example.com, Bob Jones <bob@example.com>"


def test_build_draft_body_encodes_raw_with_the_url_safe_alphabet() -> None:
    # Gmail's drafts.create `raw` field is base64URL, not standard base64: a
    # '+' or '/' in the payload is rejected/corrupted rather than decoded. The
    # two alphabets only differ once bytes hit indexes 62/63, so this body is
    # chosen to reach them — and the first assertion is a guard that fails
    # loudly if it ever stops doing so, rather than letting the test go vacuous.
    high_bytes = "��\U0001f600��\U0001f600�"
    raw = build_draft_body({**_ARGS, "body": high_bytes})["message"]["raw"]
    standard = base64.b64encode(base64.urlsafe_b64decode(raw.encode("ascii"))).decode()
    assert "+" in standard or "/" in standard, "test input no longer distinguishes"
    assert "+" not in raw and "/" not in raw


def test_build_draft_body_never_sets_a_from_or_bcc_header() -> None:
    # From is Gmail's to fill (the authenticated user); Bcc is never ours to add.
    msg = _decode(build_draft_body(_ARGS))
    assert msg["From"] is None and msg["Bcc"] is None


def test_build_draft_body_strips_header_injection_from_subject() -> None:
    body = build_draft_body({**_ARGS, "subject": "Hi\nBcc: evil@example.com"})
    msg = _decode(body)
    assert msg["Bcc"] is None
    assert "\n" not in str(msg["Subject"])


def test_build_draft_body_requires_to_subject_and_body() -> None:
    for missing in ("to", "subject", "body"):
        args = {k: v for k, v in _ARGS.items() if k != missing}
        with pytest.raises(ValueError) as exc:
            build_draft_body(args)
        assert missing in str(exc.value)


def test_build_draft_body_rejects_non_string_thread_id() -> None:
    with pytest.raises(ValueError):
        build_draft_body({**_ARGS, "thread_id": 17})


# ---- run: happy path ----------------------------------------------------------

def test_run_creates_a_draft_and_never_sends(monkeypatch) -> None:
    drafts = _FakeDrafts()
    _connect(monkeypatch, drafts)
    events: list[dict] = []

    result = GmailDraftTool().run(dict(_ARGS), _ctx(events))

    assert len(drafts.calls) == 1
    call = drafts.calls[0]
    assert call["userId"] == "me"
    assert _decode(call["body"])["To"] == "thayer@example.com"
    assert "draft" in result.lower() and "not sent" in result.lower()
    assert events and events[-1]["status"] == "ok"


def test_run_does_not_mutate_the_args_it_is_given(monkeypatch) -> None:
    _connect(monkeypatch, _FakeDrafts())
    args = dict(_ARGS)
    GmailDraftTool().run(args, _ctx())
    assert args == _ARGS


# ---- run: failure paths -------------------------------------------------------

def test_run_bad_address_returns_error_string_without_calling_the_api(monkeypatch) -> None:
    drafts = _FakeDrafts()
    _connect(monkeypatch, drafts)
    result = GmailDraftTool().run({**_ARGS, "to": "not-an-address"}, _ctx())
    assert result.startswith("error:") and "not-an-address" in result
    assert drafts.calls == []  # validation fails before anything reaches Gmail


def test_run_missing_scope_returns_actionable_message(monkeypatch) -> None:
    def _refuse(*_a, **_k):
        raise GoogleAuthError("Sonar's Google sign-in can't create drafts yet. Re-run `scripts/sonar.sh google-auth`.")

    called: list[str] = []
    monkeypatch.setattr(gmail_draft_module, "require_any_scope", _refuse)
    monkeypatch.setattr(
        gmail_draft_module, "build_service", lambda *_a, **_k: called.append("built")
    )
    result = GmailDraftTool().run(dict(_ARGS), _ctx())
    assert "google-auth" in result and "Traceback" not in result
    assert called == []  # we never even build a service without the grant


def test_run_not_connected_returns_string(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", str(tmp_path / "nope.json"))
    result = GmailDraftTool().run(dict(_ARGS), _ctx())
    assert isinstance(result, str) and "google" in result.lower()


def test_run_maps_api_failure_to_text(monkeypatch) -> None:
    _connect(monkeypatch, _FakeDrafts(error=RuntimeError("boom")))
    events: list[dict] = []
    result = GmailDraftTool().run(dict(_ARGS), _ctx(events))
    assert result.startswith("error:") and "nothing was sent" in result.lower()
    assert events[-1]["status"] == "error"


def test_run_permission_denied_points_at_reconsent(monkeypatch) -> None:
    _connect(monkeypatch, _FakeDrafts(error=RuntimeError("403 Insufficient Permission")))
    result = GmailDraftTool().run(dict(_ARGS), _ctx())
    assert "google-auth" in result


def test_run_bad_thread_id_explains_the_subject_rule(monkeypatch) -> None:
    _connect(monkeypatch, _FakeDrafts(error=RuntimeError("Invalid thread_id value")))
    result = GmailDraftTool().run({**_ARGS, "thread_id": "th-x"}, _ctx())
    assert "subject" in result.lower()


def test_run_thread_advice_only_when_a_thread_was_actually_requested(monkeypatch) -> None:
    # The word "thread" shows up in plenty of unrelated backend errors. Handing
    # the user a subject-line rule for a thread they never asked for is worse
    # than saying nothing — they'd go edit a subject that was never the problem.
    _connect(monkeypatch, _FakeDrafts(error=RuntimeError("backend thread pool exhausted")))
    result = GmailDraftTool().run(dict(_ARGS), _ctx())  # no thread_id
    assert "subject" not in result.lower()
    assert "nothing was sent" in result.lower()


# ---- the never-sends guarantee ------------------------------------------------

def test_drafts_only_client_exposes_no_send(monkeypatch) -> None:
    client = gmail_draft_module.DraftsOnlyGmail(_FakeGmail(_FakeDrafts()))
    assert not hasattr(client, "send")
    with pytest.raises(AttributeError):
        client.service  # the raw, send-capable handle is not retained  # noqa: B018


def test_module_never_references_a_send_call() -> None:
    # AST, not a text scan: prose is allowed to say "send" (the tool description
    # must!), but no *executable* attribute in this module may name a
    # send/delete endpoint. A future edit that reaches for one fails here.
    tree = ast.parse(Path(gmail_draft_module.__file__).read_text(encoding="utf-8"))
    forbidden = {"send", "messages", "trash", "delete", "batchDelete", "modify"}
    reached = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert not reached, f"gmail.draft must not reach {sorted(reached)}"


def test_description_promises_a_draft_and_no_send() -> None:
    described = GmailDraftTool.description.lower()
    assert "draft" in described
    assert "cannot send" in described or "never send" in described


def test_tool_is_local_because_saving_a_draft_is_inert() -> None:
    # `gated` tools are hidden from the model AND refused at dispatch (see
    # ToolRegistry), so gating this one would just delete the capability from a
    # voice turn. It is safe to auto-run for the same reason the tool exists:
    # nothing leaves the account until a human opens Gmail and presses Send.
    assert GmailDraftTool.name == "gmail.draft"
    assert GmailDraftTool.permission == "local"


# ---- scopes -------------------------------------------------------------------

def test_consent_adds_compose_without_widening_the_loader_floor() -> None:
    # The loader keeps asking for the OLD, smaller set: an existing read-only
    # token must keep refreshing after this change (a request for a scope the
    # token never got is what breaks refreshes).
    assert GMAIL_COMPOSE_SCOPE not in DEFAULT_SCOPES
    assert set(DEFAULT_SCOPES) <= set(CONSENT_SCOPES)
    assert GMAIL_COMPOSE_SCOPE in CONSENT_SCOPES
    assert GMAIL_COMPOSE_SCOPE in GMAIL_DRAFT_CAPABLE_SCOPES


def _token(tmp_path: Path, monkeypatch, scopes) -> Path:
    path = tmp_path / "google_token.json"
    payload = {"token": "x", "refresh_token": "y", "client_id": "c", "client_secret": "s"}
    if scopes is not None:
        payload["scopes"] = scopes
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", str(path))
    return path


def test_granted_scopes_reads_the_stored_token(tmp_path: Path, monkeypatch) -> None:
    _token(tmp_path, monkeypatch, [GMAIL_COMPOSE_SCOPE, "https://x/cal"])
    assert granted_scopes() == frozenset({GMAIL_COMPOSE_SCOPE, "https://x/cal"})


def test_granted_scopes_tolerates_missing_or_corrupt_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", str(tmp_path / "absent.json"))
    assert granted_scopes() == frozenset()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", str(bad))
    assert granted_scopes() == frozenset()


def test_require_any_scope_passes_when_granted(tmp_path: Path, monkeypatch) -> None:
    _token(tmp_path, monkeypatch, [GMAIL_COMPOSE_SCOPE])
    require_any_scope(GMAIL_DRAFT_CAPABLE_SCOPES, capability="save drafts")  # no raise


def test_require_any_scope_accepts_a_broader_existing_grant(tmp_path: Path, monkeypatch) -> None:
    _token(tmp_path, monkeypatch, ["https://www.googleapis.com/auth/gmail.modify"])
    require_any_scope(GMAIL_DRAFT_CAPABLE_SCOPES, capability="save drafts")  # no raise


def test_require_any_scope_raises_actionably_when_read_only(tmp_path: Path, monkeypatch) -> None:
    _token(tmp_path, monkeypatch, ["https://www.googleapis.com/auth/gmail.readonly"])
    with pytest.raises(GoogleAuthError) as exc:
        require_any_scope(GMAIL_DRAFT_CAPABLE_SCOPES, capability="save drafts")
    message = str(exc.value)
    assert "google-auth" in message and "save drafts" in message


def test_require_any_scope_says_not_connected_when_there_is_no_token(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SONAR_GOOGLE_TOKEN", str(tmp_path / "absent.json"))
    with pytest.raises(GoogleAuthError) as exc:
        require_any_scope(GMAIL_DRAFT_CAPABLE_SCOPES, capability="save drafts")
    assert "not connected" in str(exc.value).lower()


# ---- the scopes CREDENTIALS are loaded with ----------------------------------
#
# Load-bearing and easy to get wrong: google-auth sends the credentials' scope
# list as the `scope` parameter on every refresh, and per RFC 6749 §6 that
# NARROWS the resulting access token (see refresh_grant's own docstring: "if
# present, all scopes must be authorized for the refresh token"). A hardcoded
# list here would silently un-grant compose about an hour after the user
# approved it — drafts would work once, then 403 forever.

def test_load_scopes_keeps_compose_when_the_token_carries_it(
    tmp_path: Path, monkeypatch
) -> None:
    _token(
        tmp_path,
        monkeypatch,
        [GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, CALENDAR_EVENTS_SCOPE],
    )
    assert GMAIL_COMPOSE_SCOPE in load_scopes()


def test_load_scopes_never_asks_for_a_scope_the_token_lacks(
    tmp_path: Path, monkeypatch
) -> None:
    # The other half: a token minted before a scope existed must not have that
    # scope requested on its behalf at refresh time.
    _token(tmp_path, monkeypatch, [GMAIL_READONLY_SCOPE])
    assert load_scopes() == [GMAIL_READONLY_SCOPE]


def test_load_scopes_falls_back_when_the_token_records_none(
    tmp_path: Path, monkeypatch
) -> None:
    _token(tmp_path, monkeypatch, None)
    assert set(load_scopes()) == set(DEFAULT_SCOPES)


def test_load_credentials_uses_the_tokens_own_scopes(tmp_path: Path, monkeypatch) -> None:
    """End-to-end on the seam that actually matters: what reaches Credentials."""
    _token(tmp_path, monkeypatch, [GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE])
    seen: dict[str, list[str]] = {}

    class _FakeCredentials:
        valid = True

        @classmethod
        def from_authorized_user_file(cls, filename: str, scopes):  # noqa: ANN001
            seen["scopes"] = list(scopes)
            return cls()

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials", _FakeCredentials, raising=True
    )
    load_credentials()
    assert GMAIL_COMPOSE_SCOPE in seen["scopes"]
