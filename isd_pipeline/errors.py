"""User-facing errors. The CLI prints these as plain messages (no stack trace)."""
from isd_pipeline.retry import PermanentError


class PipelineError(RuntimeError):
    """Base class for errors with an actionable message."""


class LiveSetupError(PipelineError, PermanentError):
    """Live mode is not set up correctly (credentials, token, scopes, sender address...).
    Never retried, never swallowed: the run stops and the message tells you what to do."""
