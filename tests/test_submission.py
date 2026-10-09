import threading
import time

import pytest

from app.lender import LenderClient, LenderTimeout, LenderUnreachable
from app.main import api
from app.service import BACKOFF_BASE_SECONDS, MAX_ATTEMPTS


class SlowLender(LenderClient):
    """Slow to respond - the condition the lender reported duplicates under.

    The delay widens the window in which a second request can race the first.
    """

    def submit(self, application, idempotency_key=None):
        time.sleep(0.05)
        return super().submit(application, idempotency_key)


class UnreachableAtFirst(LenderClient):
    """Connection refused for the first N calls; the lender never sees them."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures_left = failures

    def submit(self, application, idempotency_key=None):
        if self.failures_left:
            self.failures_left -= 1
            raise LenderUnreachable("connection refused")
        return super().submit(application, idempotency_key)


class TimesOutAfterRecording(LenderClient):
    """The lender records the submission, but the response is lost for the first N calls."""

    def __init__(self, timeouts: int) -> None:
        super().__init__()
        self.timeouts_left = timeouts

    def submit(self, application, idempotency_key=None):
        reference = super().submit(application, idempotency_key)
        if self.timeouts_left:
            self.timeouts_left -= 1
            raise LenderTimeout("read timed out")
        return reference


class RecordsKeys(TimesOutAfterRecording):
    """Like TimesOutAfterRecording, but remembers every idempotency key it was sent."""

    def __init__(self, timeouts: int) -> None:
        super().__init__(timeouts)
        self.keys_seen: list[str | None] = []

    def submit(self, application, idempotency_key=None):
        self.keys_seen.append(idempotency_key)
        return super().submit(application, idempotency_key)


def test_submit_marks_application_submitted(client, lender, new_application):
    app = new_application()

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"
    assert resp.json()["lender_reference"] == lender.submissions[0]["reference"]


def test_cannot_submit_twice(client, lender, new_application):
    app = new_application()
    client.post(f"/applications/{app['id']}/submit")

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 409
    assert len(lender.submissions) == 1


@pytest.mark.parametrize("lender", [UnreachableAtFirst(failures=2)])
def test_retries_when_lender_briefly_unreachable(client, lender, new_application):
    app = new_application()

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 200
    assert len(lender.submissions) == 1


@pytest.mark.parametrize("lender", [UnreachableAtFirst(failures=10)])
def test_gives_up_after_repeated_failures(client, lender, new_application):
    app = new_application()

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 502
    assert client.get(f"/applications/{app['id']}").json()["status"] == "draft"


@pytest.mark.parametrize("lender", [TimesOutAfterRecording(timeouts=2)])
def test_timeouts_do_not_create_duplicate_submissions(client, lender, new_application):
    app = new_application()

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 200
    assert len(lender.submissions) == 1
    assert resp.json()["lender_reference"] == lender.submissions[0]["reference"]


@pytest.mark.parametrize("lender", [RecordsKeys(timeouts=2)])
def test_every_attempt_carries_the_same_idempotency_key(client, lender, new_application):
    app = new_application()

    client.post(f"/applications/{app['id']}/submit")

    assert len(lender.keys_seen) == 3
    assert all(key for key in lender.keys_seen), "every attempt must send a key"
    assert len(set(lender.keys_seen)) == 1, "retries must reuse the first attempt's key"


@pytest.mark.parametrize("lender", [TimesOutAfterRecording(timeouts=MAX_ATTEMPTS)])
def test_submitting_again_after_giving_up_does_not_duplicate(client, lender, new_application):
    """Every attempt timed out, so we report failure - but the lender did record it.

    The key is derived from the application, not from the request, so the caller's
    next attempt is recognised as the same submission rather than a new one.
    """
    app = new_application()

    first = client.post(f"/applications/{app['id']}/submit")

    assert first.status_code == 502
    assert client.get(f"/applications/{app['id']}").json()["status"] == "draft"
    assert len(lender.submissions) == 1

    second = client.post(f"/applications/{app['id']}/submit")

    assert second.status_code == 200
    assert len(lender.submissions) == 1
    assert second.json()["lender_reference"] == lender.submissions[0]["reference"]


@pytest.mark.parametrize("lender", [TimesOutAfterRecording(timeouts=0)])
def test_separate_applications_get_separate_references(client, lender, new_application):
    first = new_application(business_name="Blue Door Bakery")
    second = new_application(business_name="Red Door Bakery")

    client.post(f"/applications/{first['id']}/submit")
    client.post(f"/applications/{second['id']}/submit")

    assert len(lender.submissions) == 2
    assert lender.submissions[0]["reference"] != lender.submissions[1]["reference"]


def test_no_backoff_when_the_first_attempt_succeeds(client, new_application, backoff_delays):
    app = new_application()

    client.post(f"/applications/{app['id']}/submit")

    assert backoff_delays == []


@pytest.mark.parametrize("lender", [UnreachableAtFirst(failures=2)])
def test_backs_off_between_retries(client, lender, new_application, backoff_delays):
    app = new_application()

    client.post(f"/applications/{app['id']}/submit")

    # Two failures, so two waits - and none after the attempt that succeeded.
    assert len(backoff_delays) == 2
    # Equal jitter: each delay sits in the top half of a doubling ceiling.
    for attempt, delay in enumerate(backoff_delays, start=1):
        ceiling = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        assert ceiling / 2 <= delay <= ceiling
    assert backoff_delays[1] > backoff_delays[0], "delays must grow"


@pytest.mark.parametrize("lender", [UnreachableAtFirst(failures=10)])
def test_does_not_wait_after_the_final_attempt(client, lender, new_application, backoff_delays):
    app = new_application()

    resp = client.post(f"/applications/{app['id']}/submit")

    assert resp.status_code == 502
    # Three attempts, two gaps. Sleeping after the last one would just delay the 502.
    assert len(backoff_delays) == MAX_ATTEMPTS - 1


@pytest.mark.parametrize("lender", [SlowLender()])
def test_concurrent_submits_resolve_to_one_submission(client, lender, new_application):
    """Two requests arriving together must not both get past the draft check.

    FastAPI runs sync endpoints in a threadpool, so without the application lock
    both requests read `draft`, both submit, and both write their result.
    """
    app = new_application()
    codes: list[int] = []
    barrier = threading.Barrier(3)

    def submit() -> None:
        barrier.wait()  # line all three up so they contend for real
        codes.append(client.post(f"/applications/{app['id']}/submit").status_code)

    threads = [threading.Thread(target=submit) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(codes) == [200, 409, 409], "exactly one request may submit"
    assert len(lender.submissions) == 1
    assert client.get(f"/applications/{app['id']}").json()["status"] == "submitted"


def test_concurrent_submits_of_different_applications_do_not_block(client, lender, new_application):
    """The lock is per application - unrelated submissions must stay parallel."""
    first = new_application(business_name="Blue Door Bakery")
    second = new_application(business_name="Red Door Bakery")
    entered = threading.Barrier(2, timeout=2)

    class BlocksUntilBothArrive(LenderClient):
        def submit(self, application, idempotency_key=None):
            entered.wait()  # deadlocks if the two submissions are serialised
            return super().submit(application, idempotency_key)

    api.state.lender = BlocksUntilBothArrive()

    threads = [
        threading.Thread(target=lambda app_id=app_id: client.post(f"/applications/{app_id}/submit"))
        for app_id in (first["id"], second["id"])
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(api.state.lender.submissions) == 2
