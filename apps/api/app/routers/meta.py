"""Reference-data endpoints.

These are static lookups the frontend needs to build filter controls. They are separated
from the job endpoints because they are cacheable for hours, whereas job data is not.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel

from jobplatform_schemas import (
    US_STATES,
    US_TERRITORIES,
    EmploymentType,
    FreshnessBucket,
    JobStatus,
    RemoteType,
)

router = APIRouter()


class StateRef(BaseModel):
    code: str
    name: str
    is_territory: bool = False


class EnumsResponse(BaseModel):
    job_status: list[str]
    remote_type: list[str]
    employment_type: list[str]
    freshness_bucket: list[str]


@router.get("/locations/states", response_model=list[StateRef], summary="US states and DC")
async def list_states(response: Response, include_territories: bool = False) -> list[StateRef]:
    """The 50 states plus DC, optionally including inhabited territories."""
    # Reference data changes essentially never; let clients and CDNs cache it hard.
    response.headers["Cache-Control"] = "public, max-age=86400"

    states = [StateRef(code=c, name=n) for c, n in sorted(US_STATES.items())]
    if include_territories:
        states += [
            StateRef(code=c, name=n, is_territory=True) for c, n in sorted(US_TERRITORIES.items())
        ]
    return states


@router.get("/meta/enums", response_model=EnumsResponse, summary="Enumeration values")
async def list_enums(response: Response) -> EnumsResponse:
    """Every enum the API accepts or returns, so the frontend never hard-codes them."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    return EnumsResponse(
        job_status=[e.value for e in JobStatus],
        remote_type=[e.value for e in RemoteType],
        employment_type=[e.value for e in EmploymentType],
        freshness_bucket=[e.value for e in FreshnessBucket],
    )
