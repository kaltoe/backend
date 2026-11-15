"""/api/v1 아래의 REST 라우터를 모은다. 기능 라우터는 여기에 include 한다."""

from fastapi import APIRouter

from app.api.docs import ENTRY, INTERNAL, LIVE, RESULT, SETUP
from app.meetings import (
    agendas,
    create,
    dismiss,
    end,
    entry,
    pause,
    postprocess,
    recording,
    result,
    sign_in,
    state,
    summary,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(create.router, tags=[SETUP])
api_router.include_router(entry.router, tags=[ENTRY])
api_router.include_router(sign_in.router, tags=[ENTRY])
api_router.include_router(state.router, tags=[LIVE])
api_router.include_router(pause.router, tags=[LIVE])
api_router.include_router(agendas.router, tags=[LIVE])
api_router.include_router(end.router, tags=[LIVE])
api_router.include_router(dismiss.router, tags=[LIVE])
api_router.include_router(summary.router, tags=[RESULT])
api_router.include_router(recording.router, tags=[RESULT])
api_router.include_router(result.router, tags=[RESULT])
api_router.include_router(postprocess.router, tags=[INTERNAL])
