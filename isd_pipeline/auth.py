"""One-time OAuth for live mode, and credential loading for the live adapters.

    python -m isd_pipeline.auth              # opens a browser; sign in with the BU account
    python -m isd_pipeline.auth --no-browser # prints the URL instead (e.g. over SSH)

Scopes (nothing broader is ever requested):
    gmail.readonly  - read the Power BI emails and attachments
    drive.file      - create/read ONLY files this app created (it cannot see the rest of Drive)
"""
from __future__ import annotations

import argparse
import os
import sys

from isd_pipeline.errors import LiveSetupError
from isd_pipeline.settings import Settings, load_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

_NO_CREDENTIALS = """\
Google OAuth client file not found: {path}

Create it once (docs/LIVE_SETUP.md steps 3-6):
  1. console.cloud.google.com -> create a project (e.g. isd-permit-pipeline)
  2. APIs & Services -> Library -> enable "Gmail API" and "Google Drive API"
  3. OAuth consent screen (Google Auth Platform): Internal if BU-owned, otherwise External + Testing
     with the BU address as a test user; scopes gmail.readonly and drive.file
  4. Credentials -> Create credentials -> OAuth client ID -> Desktop app -> Download JSON
  5. Save it as {path}
"""


def load_credentials(settings: Settings | None = None):
    """Return valid google.oauth2 Credentials or raise LiveSetupError with what to do next."""
    settings = settings or load_settings()
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover
        raise LiveSetupError("Google client libraries are missing: pip install -r requirements.txt") from exc

    token = settings.token_file
    if not token.exists():
        if not settings.credentials_file.exists():
            raise LiveSetupError(_NO_CREDENTIALS.format(path=settings.credentials_file))
        raise LiveSetupError(f"No saved login ({token}). Run once:  python -m isd_pipeline.auth")
    try:
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    except ValueError as exc:
        raise LiveSetupError(f"{token} is not a valid token file ({exc}). Delete it and run: python -m isd_pipeline.auth")

    granted = set(creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if granted and missing:
        raise LiveSetupError(
            "The saved login is missing required permission(s): " + ", ".join(missing) +
            f".\nDelete {token} and run  python -m isd_pipeline.auth  again, and tick every checkbox on Google's "
            "consent screen.")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise LiveSetupError(
                    "The saved Google login has expired or was revoked "
                    f"({exc.args[0] if exc.args else exc}).\nIf the OAuth app is 'External' in 'Testing', Google "
                    "expires logins after ~7 days - this is expected.\nFix: python -m isd_pipeline.auth") from exc
            _save(creds, token)
        else:
            raise LiveSetupError("The saved Google login is not usable. Run: python -m isd_pipeline.auth")
    return creds


def _save(creds, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass


def run_oauth_flow(settings: Settings | None = None, open_browser: bool = True):
    settings = settings or load_settings()
    if not settings.credentials_file.exists():
        raise LiveSetupError(_NO_CREDENTIALS.format(path=settings.credentials_file))
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Open this URL and sign in with the BU account:\n{url}\n")
    _save(creds, settings.token_file)
    return creds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Authorize Gmail (read-only) + Drive (app files only) for live mode.")
    ap.add_argument("--no-browser", action="store_true", help="print the sign-in URL instead of opening a browser")
    args = ap.parse_args(argv)
    try:
        settings = load_settings()
        run_oauth_flow(settings, open_browser=not args.no_browser)
        print(f"✔ Authorized. Token saved to {settings.token_file} (keep it private; it is gitignored).")
        print("Next: python -m isd_pipeline.cli check-gmail --date YYYY-MM-DD --live")
        return 0
    except LiveSetupError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "access_denied" in msg or "admin_policy_enforced" in msg or "restricted" in msg:
            print("✘ Google blocked the sign-in (your organization may restrict this app).\n"
                  "  Ask BU IT whether internal Gmail API projects are allowed for students; until then keep using "
                  "mock mode. See docs/LIVE_SETUP.md step 11.", file=sys.stderr)
        else:
            print(f"✘ Authorization failed: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
