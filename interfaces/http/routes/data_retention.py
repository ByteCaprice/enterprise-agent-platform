"""Admin-only project retention inventory endpoint."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import AwareDatetime

from domain.data_retention import (
    RetentionCutoffAfterReadTime,
    RetentionInventory,
    RetentionProjectNotFound,
)
from interfaces.http.platform_auth import AdminActor
from orchestration.data_retention_service import DataRetentionService

router = APIRouter(prefix="/v2", tags=["data-retention"])


@router.get(
    "/projects/{project_key}/operations/retention-inventory",
    response_model=RetentionInventory,
)
def retention_inventory(
    request: Request,
    response: Response,
    project_key: str,
    actor: AdminActor,
    before: Annotated[AwareDatetime, Query()],
) -> RetentionInventory:
    response.headers["Cache-Control"] = "no-store"
    try:
        return DataRetentionService(request.app.state.store.data_retention).inventory(project_key, before)
    except RetentionProjectNotFound:
        raise HTTPException(404, "Project not found", headers={"Cache-Control": "no-store"}) from None
    except RetentionCutoffAfterReadTime:
        raise HTTPException(422, "Cutoff must not be after the database read time", headers={"Cache-Control": "no-store"}) from None
