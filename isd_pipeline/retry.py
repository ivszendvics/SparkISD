"""Small retry-with-exponential-backoff helper used by the live adapters and the pipeline."""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


class PermanentError(Exception):
    """Raise (or wrap) to stop retrying immediately, e.g. bad credentials or a 404."""


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    what: str = "operation",
    is_retriable: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn(); on a retriable exception wait base_delay * 2**n and try again.

    The last exception is re-raised after `attempts` tries. PermanentError is never retried.
    """
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except PermanentError:
            raise
        except Exception as exc:  # noqa: BLE001 - we re-raise below
            last = exc
            if is_retriable is not None and not is_retriable(exc):
                raise
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.1fs", what, attempt, attempts, exc, delay)
            sleep(delay)
    assert last is not None
    raise last
