"""gmail.draft — save a message to the user's Gmail DRAFTS. It cannot send.

"Draft a reply to Thayer saying I'll have it Friday" composes the mail and
leaves it in Drafts; a human opens Gmail and presses Send, or doesn't. That is
the standing decision (DECISIONS: email is draft-only, never auto-sent) and it
matters more here than anywhere else in the harness — a voice assistant that can
mail on a mishearing is a different, much worse product.

**The guarantee is structural, not a promise in a comment.** The only Gmail
handle this module ever holds is ``DraftsOnlyGmail``, which binds
``users().drafts().create`` at construction and keeps nothing else — the tool
never touches the raw service, so no code path here (not even a buggy one)
reaches ``drafts().send()`` or ``users().messages().send()``. A test scans this
file's source for a ``.send(`` call, so a future edit that adds one turns the
suite red instead of shipping quietly.

Scope note: Google publishes no "create drafts but cannot send" scope —
``gmail.compose`` is the narrowest that can save a draft, and it technically
permits sending too. Sonar simply never asks Gmail to send. A grant that predates
this tool is read-only, so the tool checks the stored scopes first and returns a
"re-run google-auth" line the model can relay, rather than a 403 stack trace.

Only ``run`` touches the network; the recipient parsing and MIME/raw body
building are pure and unit-tested.
"""
from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import Any

from sonar_harness.google_auth import (
    GMAIL_DRAFT_CAPABLE_SCOPES,
    GoogleAuthError,
    build_service,
    require_any_scope,
)
from sonar_harness.tools.base import ToolBase, ToolContext

# Pragmatic addr-spec check: enough to catch a mis-heard "thayer at example dot
# com" or a truncated domain before it becomes an opaque Gmail 400. Full RFC 5322
# is not worth implementing — Gmail is the real validator.
_ADDR_RE = re.compile(
    r"^[^@\s,;<>\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

_CAPABILITY = "save Gmail drafts"


def _clean_header(value: str) -> str:
    """Collapse CR/LF (and stray control chars) out of a header value.

    Subjects and display names arrive as model-authored text; a newline in one
    would either fold the header or, worse, inject a new one (``Bcc:``). Headers
    are single-line by construction here.
    """
    return " ".join(str(value).split()).strip()


def _separators(value: str) -> str:
    """Let ``;`` act as a recipient separator, which models emit constantly.

    Only when the value carries no quoted text: inside ``"Jones; Bob" <b@x.com>``
    the semicolon is part of a display name the user gave us, and rewriting it
    would silently corrupt their data to fix a problem they don't have. RFC 5322
    otherwise uses ``;`` only to close a group, which this tool doesn't support.
    """
    return value.replace(";", ",") if '"' not in value else value


def _format_address(display: str, addr: str) -> str:
    """Re-render one parsed recipient, keeping a display name when given."""
    name = _clean_header(display)
    return formataddr((name, addr)) if name else addr


def parse_recipients(value: Any, *, field: str) -> tuple[str, ...]:
    """Normalize a recipient argument into validated address strings (pure).

    Accepts a single address, a comma-separated string, or a list, with or
    without display names (``Bob Jones <bob@example.com>``). Returns ``()`` for
    an absent/blank value — callers decide whether that field is required.
    Raises ``ValueError`` naming the offending text, so the model can correct it
    instead of relaying an API exception.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = [item for item in value if item is not None]
    else:
        raise ValueError(f"'{field}' must be an email address or a list of them.")
    if any(not isinstance(item, str) for item in raw):
        raise ValueError(f"'{field}' must contain only email addresses as text.")

    cleaned = [_separators(_clean_header(item)) for item in raw if item.strip()]
    out: list[str] = []
    for display, addr in getaddresses(cleaned):
        addr = addr.strip()
        if not _ADDR_RE.match(addr):
            # Fall back to the whole original value: text this malformed can
            # parse to an empty display AND an empty address, and quoting ''
            # back gives the model nothing to correct.
            shown = addr or display or ", ".join(cleaned)
            raise ValueError(
                f"'{field}' has an address Sonar can't use: {shown!r}. "
                "Give a full address like name@example.com."
            )
        out.append(_format_address(display, addr))
    return tuple(out)


def build_draft_body(args: dict[str, Any]) -> dict[str, Any]:
    """Build the Gmail ``drafts.create`` request body from model args (pure).

    Returns ``{"message": {"raw": <base64url RFC-2822>, "threadId"?: ...}}``.
    No ``From`` header: Gmail stamps the authenticated user, so Sonar never has
    to know (or guess) the user's own address. Raises ``ValueError`` — mapped to
    model text by ``run`` — on anything malformed.
    """
    to = parse_recipients(args.get("to"), field="to")
    if not to:
        raise ValueError("gmail.draft requires 'to' — at least one recipient address.")
    cc = parse_recipients(args.get("cc"), field="cc")

    subject = args.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("gmail.draft requires a non-empty 'subject'.")
    body = args.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("gmail.draft requires a non-empty 'body' (the email text).")

    thread_id = args.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        raise ValueError("'thread_id' must be the thread's id as text, if given.")
    thread_id = thread_id.strip() if isinstance(thread_id, str) else ""

    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = _clean_header(subject)
    message.set_content(body.strip())

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return {"message": payload}


class DraftsOnlyGmail:
    """A Gmail handle whose entire vocabulary is "create a draft".

    Constructed from a full service, it binds ``users().drafts().create`` and
    drops the rest on the floor: ``__slots__`` keeps anything else from being
    attached later, and the tool holds one of these instead of the service, so
    the send endpoints are simply not addressable from this module.
    """

    __slots__ = ("_create",)

    def __init__(self, service: Any) -> None:
        self._create = service.users().drafts().create

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """Save one draft; returns the created draft resource."""
        return self._create(userId="me", body=body).execute()


def _api_error_text(exc: Exception, *, threaded: bool) -> str:
    """Turn a Gmail failure into a line the model can act on and say out loud.

    ``threaded`` gates the thread-specific advice on whether a thread was
    actually requested: "thread" appears in plenty of unrelated backend errors,
    and sending the user to fix a subject line they never set is worse than
    saying nothing.

    Every branch ends by reasserting that nothing was sent — after a failure
    mid-turn that is the user's actual question.
    """
    detail = str(exc)
    lowered = detail.lower()
    if "insufficient" in lowered or "permission" in lowered or "403" in lowered:
        return (
            "error: Gmail refused the draft for lack of permission. Re-run "
            "`scripts/sonar.sh google-auth` and approve the drafts permission. "
            "Nothing was sent."
        )
    if threaded and "thread" in lowered:
        return (
            "error: Gmail rejected that thread — a reply can only join a thread "
            "when its subject matches the original (prefix the original subject "
            "with 'Re: '). Try again without the thread, or with the exact "
            "subject. Nothing was sent."
        )
    return (
        f"error: saving the Gmail draft failed ({type(exc).__name__}): {detail}. "
        "Nothing was sent."
    )


class GmailDraftTool(ToolBase):
    name = "gmail.draft"
    description = (
        "Write an email and SAVE IT AS A DRAFT in the user's Gmail. This tool "
        "cannot send mail — Sonar has no tool that can — so the message waits in "
        "the Drafts folder until the user opens Gmail and sends it themselves. "
        "Tell them that plainly when you report back; it is the point. Use it for "
        "'draft a reply to Thayer saying I'll have it Friday', 'write Alice about "
        "the invoice', 'put together an email to my landlord'. Give 'to' (one "
        "address, or several separated by commas), a short 'subject', and 'body' "
        "written as the user would write it, in their voice, first person — not a "
        "description of the email. Optional 'cc'. Optional 'thread_id' keeps the "
        "draft inside an existing conversation; only pass one you were actually "
        "given, and then reuse that thread's exact subject (prefixed 'Re: '), "
        "which Gmail requires. If you don't know the recipient's address, ask "
        "instead of guessing. After saving, read the subject and the gist of the "
        "body back to the user so they can correct it — never read ids aloud."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient address, or several separated by commas.",
            },
            "subject": {"type": "string", "description": "Subject line."},
            "body": {
                "type": "string",
                "description": "The email text itself, in the user's voice (plain text).",
            },
            "cc": {
                "type": "string",
                "description": "Optional cc address(es), comma-separated.",
            },
            "thread_id": {
                "type": "string",
                "description": (
                    "Optional Gmail thread id to reply inside; requires the "
                    "original subject (as 'Re: <subject>')."
                ),
            },
        },
        "required": ["to", "subject", "body"],
    }
    permission = "local"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            body = build_draft_body(args)  # pure: never mutates `args`
        except ValueError as exc:
            ctx.emit(_summary(self.name, "invalid arguments", status="error"))
            return f"error: {exc}"

        # Check the stored grant BEFORE building a service: a read-only token
        # would otherwise spend a round trip to earn an opaque 403.
        try:
            require_any_scope(GMAIL_DRAFT_CAPABLE_SCOPES, capability=_CAPABILITY)
            service = build_service("gmail", "v1")
        except GoogleAuthError as exc:
            ctx.emit(_summary(self.name, str(exc), status="error"))
            return str(exc)

        try:
            draft = DraftsOnlyGmail(service).create(body)
        except Exception as exc:  # noqa: BLE001 — map API failure to model text
            ctx.emit(_summary(self.name, type(exc).__name__, status="error"))
            return _api_error_text(exc, threaded="threadId" in body["message"])

        ctx.emit(_summary(self.name, "draft saved"))
        return _confirmation(args, draft)


def _confirmation(args: dict[str, Any], draft: dict[str, Any]) -> str:
    """What the model reads back: what was written, and that it is still unsent."""
    draft_id = str(draft.get("id", "")).strip()
    # Re-parsing cannot raise here: the draft only exists because
    # build_draft_body already accepted these exact recipients.
    recipients = ", ".join(parse_recipients(args.get("to"), field="to"))
    subject = _clean_header(str(args.get("subject", "")))
    tail = f" [draft:{draft_id}]" if draft_id else ""
    return (
        f"Saved a draft to Gmail — it was NOT sent. To: {recipients}. "
        f"Subject: {subject}. It's sitting in the Drafts folder; the user can "
        f"review and send it from Gmail whenever they like.{tail}"
    )


def _summary(tool: str, detail: str, *, status: str = "ok") -> dict[str, Any]:
    return {"step": "tool_result_summary", "tool": tool, "detail": detail, "status": status}
