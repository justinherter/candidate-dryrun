import logging
import random
import time
from typing import Callable

from app.lender import LenderClient, LenderTimeout, LenderUnreachable
from app.models import Application, Status
from app.store import ApplicationStore

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# First retry waits ~0.5s, the second ~1s. Half of each delay is random so that a
# batch of submissions interrupted by the same lender outage does not retry in
# lockstep and arrive as one spike.
BACKOFF_BASE_SECONDS = 0.5


class SubmissionFailed(Exception):
    pass


class InvalidState(Exception):
    pass


class ApplicationNotFound(Exception):
    pass


def idempotency_key_for(app: Application) -> str:
    """The key the lender uses to collapse repeat submissions of one application.

    Derived from the application id, so it is identical for every retry inside a
    single call *and* for any later call that submits the same application again
    (for example after we gave up and the caller tried once more). It must not be
    derived from the application's contents, or an edit between attempts would
    read as a new submission.
    """
    return f"application:{app.id}"


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before the attempt after this one. Equal jitter."""
    ceiling = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def submit_application(
    app_id: str,
    lender: LenderClient,
    store: ApplicationStore,
    sleep: Callable[[float], None] = time.sleep,
) -> Application:
    """Submit an application to the lender, at most once.

    Holds the application's lock for the whole read-check-submit-write sequence.
    The check that an application is still a draft is only meaningful if nothing
    else can submit it between that check and the write that follows.
    """
    with store.locked(app_id):
        app = store.get(app_id)
        if app is None:
            raise ApplicationNotFound("application not found")
        if app.status != Status.DRAFT:
            raise InvalidState(f"application {app.id} is already {app.status.value}")

        idempotency_key = idempotency_key_for(app)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                # A timeout means the lender may already have recorded this
                # submission, so every attempt carries the same key and the lender
                # replays the original reference instead of recording a duplicate.
                reference = lender.submit(app, idempotency_key=idempotency_key)
                break
            except (LenderUnreachable, LenderTimeout) as exc:
                logger.warning("lender submit failed (attempt %d/%d): %r", attempt, MAX_ATTEMPTS, exc)
                if attempt < MAX_ATTEMPTS:
                    # Back off before trying again. Retrying instantly adds load to
                    # a lender that is, by the evidence of this failure, struggling.
                    sleep(backoff_delay(attempt))
        else:
            raise SubmissionFailed(f"lender did not accept application {app.id}")

        app.lender_reference = reference
        app.status = Status.SUBMITTED
        return app
