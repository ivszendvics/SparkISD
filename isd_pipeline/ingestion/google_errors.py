"""Turn googleapiclient HttpErrors into actionable messages / retry decisions."""
from __future__ import annotations

import json

from isd_pipeline.errors import LiveSetupError

RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _reason(exc) -> tuple[int, str, str]:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    reason, message = "", str(exc)
    try:
        body = json.loads(exc.content.decode())
        err = body.get("error", {})
        message = err.get("message", message)
        details = err.get("errors") or [{}]
        reason = details[0].get("reason", "") or (err.get("status") or "")
    except Exception:  # noqa: BLE001
        pass
    return status, reason, message


def translate(exc: Exception, api: str) -> Exception:
    """Return the exception to raise: LiveSetupError for setup problems, the original otherwise."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover
        return exc
    if not isinstance(exc, HttpError):
        return exc
    status, reason, message = _reason(exc)
    if status == 401:
        return LiveSetupError(f"{api}: Google rejected the saved login (401). Run: python -m isd_pipeline.auth")
    if status == 403 and reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "PERMISSION_DENIED"):
        return LiveSetupError(f"{api}: the saved login lacks the needed scope ({message}). Delete secrets/token.json "
                              "and run python -m isd_pipeline.auth again, approving every permission.")
    if status == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return LiveSetupError(f"{api} is not enabled for this Google Cloud project. Console -> APIs & Services -> "
                              f"Library -> enable it. ({message})")
    if status == 403 and "admin" in message.lower():
        return LiveSetupError(f"{api}: blocked by the Google Workspace administrator ({message}). "
                              "See docs/LIVE_SETUP.md step 11.")
    return exc


def is_retriable(exc: Exception) -> bool:
    if isinstance(exc, LiveSetupError):
        return False
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    if status:
        return status in RETRIABLE_STATUS
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))
