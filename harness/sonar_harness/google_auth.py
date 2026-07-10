"""Google OAuth for Sonar — per-user installed-app flow, ZERO admin required.

Sonar reads Gmail + Calendar locally through the user's OWN Google account using
the standard 3-legged "installed app" OAuth flow: a browser consent once, then a
locally-stored, auto-refreshing token. We deliberately do NOT use a service
account / domain-wide delegation (the only Google auth path that needs a
Workspace super-admin). For a personal ``@gmail.com`` there is no tenant and no
admin at all.

Scopes are READ-ONLY (``gmail.readonly`` + ``calendar.readonly``). Draft/send is
a separate, human-gated capability (DECISIONS: email is draft-only, never
auto-sent) and is not wired here.

--------------------------------------------------------------------------------
ONE-TIME SETUP (Navin — no admin needed):
  1. Google Cloud Console -> create a project (personal; free).
  2. "APIs & Services" -> Library -> enable "Gmail API" and "Google Calendar API".
  3. "OAuth consent screen" -> User type EXTERNAL -> fill app name + your email.
       Add yourself under "Test users". Add scopes gmail.readonly + calendar.readonly.
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

import logging
import os
from pathlib import Path

log = logging.getLogger("sonar.google")

# The token is granted ALL of these at consent; every tool loads with the full
# set so a refresh never trips a scope-change error. Gmail stays READ-ONLY
# (draft/send is a later human-gated capability). Calendar uses `calendar.events`
# = read AND write events (Navin asked to create events); it does NOT grant
# access to calendar settings/sharing or other Google data.
DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
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

    creds = Credentials.from_authorized_user_file(str(token), list(DEFAULT_SCOPES))
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
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), list(DEFAULT_SCOPES))
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
