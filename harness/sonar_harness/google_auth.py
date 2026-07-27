"""Google OAuth for Sonar — per-user installed-app flow, ZERO admin required.

Sonar reads Gmail + Calendar locally through the user's OWN Google account using
the standard 3-legged "installed app" OAuth flow: a browser consent once, then a
locally-stored, auto-refreshing token. We deliberately do NOT use a service
account / domain-wide delegation (the only Google auth path that needs a
Workspace super-admin). For a personal ``@gmail.com`` there is no tenant and no
admin at all.

Gmail is READ-ONLY plus ``gmail.compose`` (create DRAFTS). Sonar has no tool that
sends mail — email stays draft-only, never auto-sent (DECISIONS) — and that is
enforced in code, not by the scope: Google offers no "drafts but not send" scope,
so ``gmail.compose`` is simply the narrowest scope that can save a draft at all.
Calendar uses ``calendar.events``.

--------------------------------------------------------------------------------
ONE-TIME SETUP (Navin — no admin needed):
  1. Google Cloud Console -> create a project (personal; free).
  2. "APIs & Services" -> Library -> enable "Gmail API" and "Google Calendar API".
  3. "OAuth consent screen" -> User type EXTERNAL -> fill app name + your email.
       Add yourself under "Test users". Add scopes gmail.readonly + gmail.compose
       + calendar.events. (gmail.compose is a "restricted" scope: Google shows a
       verification notice when you add it — for your own account you can proceed
       and click through the one-time "unverified app" screen at consent.)
       IMPORTANT: click "PUBLISH APP" (set publishing status to "In production").
       In "Testing" status Google expires the refresh token after 7 days; "In
       production" for your own account just shows a one-time "unverified app"
       screen you click through. No verification review is needed for personal use.
  4. "Credentials" -> Create credentials -> OAuth client ID -> type "Desktop app".
       Download the JSON.
  5. Save it as ~/.config/sonar/google_client_secret.json
       (or set SONAR_GOOGLE_CLIENT_SECRET=/path/to/it).
  6. Run:  scripts/sonar.sh google-auth
       A browser opens; approve. The token is saved to
       ~/.config/sonar/google_token.json and refreshes itself thereafter.
--------------------------------------------------------------------------------

Heavy Google libraries are imported lazily so the harness (and its tests) import
without them; a tool that needs Google surfaces a clear "not connected" string
rather than crashing the turn.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

log = logging.getLogger("sonar.google")

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# FALLBACK scopes, used only when the stored token doesn't record its own (see
# `load_scopes`, which is what credentials are actually built with). This is the
# historical read-only-Gmail set on purpose: it is the grant any token predating
# `gmail.compose` carries, and requesting a scope that was never granted is what
# makes google-auth complain at refresh (older versions raise outright).
DEFAULT_SCOPES: tuple[str, ...] = (GMAIL_READONLY_SCOPE, CALENDAR_EVENTS_SCOPE)

# Scopes requested at CONSENT — a superset of the floor. `gmail.compose` lets
# Sonar SAVE DRAFTS (gmail.draft). Google has no draft-without-send scope, so
# "never sends" is guaranteed by Sonar's code (there is no send call anywhere in
# the harness), not by the grant. `calendar.events` = read AND write events; it
# does NOT grant calendar settings/sharing or any other Google data.
CONSENT_SCOPES: tuple[str, ...] = (*DEFAULT_SCOPES, GMAIL_COMPOSE_SCOPE)

# Scopes that permit creating a Gmail draft. Sonar only ever REQUESTS the first
# (narrowest) one, but accepts a broader grant a token may already carry rather
# than forcing a pointless re-consent.
GMAIL_DRAFT_CAPABLE_SCOPES: tuple[str, ...] = (
    GMAIL_COMPOSE_SCOPE,
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",
)


class GoogleAuthError(RuntimeError):
    """Raised when Google auth is unavailable/expired. Message is model-safe."""


def _config_dir() -> Path:
    return Path(os.environ.get("SONAR_CONFIG_DIR", str(Path.home() / ".config" / "sonar")))


def _client_secret_path() -> Path:
    env = os.environ.get("SONAR_GOOGLE_CLIENT_SECRET")
    return Path(env) if env else _config_dir() / "google_client_secret.json"


def _token_path() -> Path:
    env = os.environ.get("SONAR_GOOGLE_TOKEN")
    return Path(env) if env else _config_dir() / "google_token.json"


_NOT_CONNECTED = (
    "Google is not connected yet. Run `scripts/sonar.sh google-auth` once to "
    "sign in (see harness/sonar_harness/google_auth.py for the one-time setup)."
)


def granted_scopes() -> frozenset[str]:
    """Scopes the STORED token actually carries (empty when unreadable/absent).

    Read from the token file, not from the constants above: the constants say
    what Sonar *asks* for, and a token minted before a scope was added still
    holds the older, smaller grant. Only the file knows what the user really
    approved — which is what lets a tool say "re-run google-auth" up front
    instead of letting Google answer a doomed request with a bare 403.
    """
    path = _token_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("could not read scopes from token %s (%s)", path, exc)
        return frozenset()
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, str):  # some writers store them space-separated
        scopes = scopes.split()
    if not isinstance(scopes, list):
        return frozenset()
    return frozenset(str(s) for s in scopes if isinstance(s, str))


def require_any_scope(scopes: Iterable[str], *, capability: str) -> None:
    """Raise ``GoogleAuthError`` unless the stored grant includes one of ``scopes``.

    ``capability`` is a plain-language verb phrase ("save Gmail drafts") that
    goes into the message, so the model can tell the user what to re-approve and
    why — a scope URL alone is not actionable to a person listening out loud.
    """
    accepted = tuple(scopes)
    if not accepted:  # programming error: an empty gate would silently pass
        raise ValueError("require_any_scope needs at least one acceptable scope")
    if not _token_path().exists():
        raise GoogleAuthError(_NOT_CONNECTED)
    if granted_scopes() & frozenset(accepted):
        return
    raise GoogleAuthError(
        f"Sonar's Google sign-in doesn't include permission to {capability} yet. "
        "Re-run `scripts/sonar.sh google-auth` and approve the extra permission "
        f"({accepted[0]}); the existing read access keeps working either way."
    )


def load_scopes() -> list[str]:
    """The scope list to construct ``Credentials`` with — read from the TOKEN.

    Not a constant, and that is the whole point. google-auth passes these as the
    OAuth ``scope`` parameter on every refresh, and per RFC 6749 §6 a refresh
    request narrows the access token it mints (``refresh_grant``'s own docstring:
    "if present, all scopes must be authorized for the refresh token"). So a
    hardcoded list here is not a floor, it is a ceiling: pinning it below what
    the user actually approved would silently un-grant ``gmail.compose`` at the
    first refresh — drafts would work for about an hour after consent and then
    403 forever, which reads as "Sonar is broken", not "re-consent".

    Mirroring the token also preserves the property the old constant was
    protecting: we never request a scope the grant lacks, so a token minted
    before a scope existed keeps refreshing untouched. ``DEFAULT_SCOPES`` is the
    fallback for a token file that doesn't record its scopes.
    """
    stored = granted_scopes()
    return sorted(stored) if stored else list(DEFAULT_SCOPES)


def _save_token(creds: object) -> None:
    """Persist refreshed/created credentials to the token file (0600)."""
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())  # type: ignore[attr-defined]
    try:
        path.chmod(0o600)
    except OSError:  # non-POSIX / permission quirk — the token is still usable
        log.debug("could not chmod token file %s", path)


def load_credentials():
    """Return valid, refreshed Google credentials, or raise GoogleAuthError.

    Loads the stored token, silently refreshes it when expired (persisting the
    new access token), and raises a model-safe error when the user has not
    completed the one-time consent yet.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise GoogleAuthError(
            "Google API libraries are not installed in the harness env."
        ) from exc

    token = _token_path()
    if not token.exists():
        raise GoogleAuthError(_NOT_CONNECTED)

    creds = Credentials.from_authorized_user_file(str(token), load_scopes())
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 — refresh failure -> re-consent
            raise GoogleAuthError(
                f"Google auth expired and could not refresh ({exc}). "
                "Re-run `scripts/sonar.sh google-auth`."
            ) from exc
        _save_token(creds)
        return creds
    raise GoogleAuthError(
        "Google auth is invalid. Re-run `scripts/sonar.sh google-auth`."
    )


def build_service(api: str, version: str):
    """Build an authenticated Google API client (e.g. ``build_service('gmail','v1')``)."""
    creds = load_credentials()
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise GoogleAuthError(
            "google-api-python-client is not installed in the harness env."
        ) from exc
    # cache_discovery=False: the file cache warns/needs oauth2client on modern setups.
    return build(api, version, credentials=creds, cache_discovery=False)


def run_consent() -> None:
    """Run the one-time browser consent and save the token. Used by the CLI."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise GoogleAuthError(
            "google-auth-oauthlib is not installed in the harness env."
        ) from exc

    secret = _client_secret_path()
    if not secret.exists():
        raise GoogleAuthError(
            f"OAuth client secret not found at {secret}. Create a Desktop-app "
            "OAuth client in Google Cloud Console and save its JSON there "
            "(see this module's docstring for the exact zero-admin steps)."
        )
    # CONSENT_SCOPES, not DEFAULT_SCOPES: consent is the one moment we can ask
    # for more than the token already has (see the constants above).
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), list(CONSENT_SCOPES))
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    print(f"[google] connected; token saved to {_token_path()}", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        run_consent()
    except GoogleAuthError as exc:
        print(f"[google] {exc}", flush=True)
        raise SystemExit(1)
