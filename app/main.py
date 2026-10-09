from fastapi import Depends, FastAPI, HTTPException, Request

from app.lender import LenderClient
from app.models import Application, ApplicationIn, Status
from app.service import ApplicationNotFound, InvalidState, SubmissionFailed, submit_application
from app.store import ApplicationStore

api = FastAPI(title="Lender Submissions")
api.state.store = ApplicationStore()
api.state.lender = LenderClient()


def get_store() -> ApplicationStore:
    return api.state.store


def get_lender() -> LenderClient:
    return api.state.lender


@api.post("/applications", status_code=201)
def create_application(data: ApplicationIn, store: ApplicationStore = Depends(get_store)) -> Application:
    return store.create(data)


@api.get("/applications")
def list_applications(status: Status | None = None, store: ApplicationStore = Depends(get_store)) -> list[Application]:
    return store.list(status=status)


@api.get("/applications/{app_id}")
def get_application(app_id: str, store: ApplicationStore = Depends(get_store)) -> Application:
    app = store.get(app_id)
    if app is None:
        raise HTTPException(404, "application not found")
    return app


@api.post("/applications/{app_id}/submit")
def submit(
    app_id: str,
    store: ApplicationStore = Depends(get_store),
    lender: LenderClient = Depends(get_lender),
) -> Application:
    try:
        return submit_application(app_id, lender, store)
    except ApplicationNotFound as exc:
        raise HTTPException(404, str(exc))
    except InvalidState as exc:
        raise HTTPException(409, str(exc))
    except SubmissionFailed as exc:
        raise HTTPException(502, str(exc))


@api.post("/webhooks/lender-status")
async def lender_status_webhook(request: Request, store: ApplicationStore = Depends(get_store)):
    # TODO: Task 2 (stretch) — see README.
    raise HTTPException(501, "not implemented")
