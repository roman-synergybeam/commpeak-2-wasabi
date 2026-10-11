"""The API a remote transcription agent talks to.

Bearer-token authenticated, JSON only, and deliberately narrow: claim work,
report a result, give a job back, read progress. See `c2w.transcribe.agents`
for what the server does and does not trust the agent with.

The agent program itself is served from here too, so installing it on a
machine is one command that needs nothing but this console's address. It is
code, not a secret: the token is what grants access, and it is entered at
install time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.api.deps import get_session
from c2w.settings import settings_service
from c2w.transcribe import agents
from c2w.transcribe.agents import Agent, AgentResult, LeaseGone

router = APIRouter(prefix="/api/agent/v1", tags=["agent"])

_DIST = Path(__file__).resolve().parent.parent / "agent_dist"
_DOWNLOADS = {"c2w_agent.py": "text/x-python", "install.ps1": "text/plain"}


class AgentInfo(BaseModel):
    """What the agent says about itself, kept for the console to show."""

    version: str = Field(default="", max_length=40)
    device: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=120)


class ClaimIn(BaseModel):
    max: int = Field(default=8, ge=1, le=agents.MAX_CLAIM)
    agent: AgentInfo = Field(default_factory=AgentInfo)


class SegmentIn(BaseModel):
    start: float = Field(ge=0, le=86_400)
    end: float = Field(ge=0, le=86_400)
    text: str = Field(max_length=4_000)
    confidence: float | None = None


class ResultIn(BaseModel):
    segments: list[SegmentIn] = Field(default_factory=list, max_length=20_000)
    language: str | None = Field(default=None, max_length=12)
    language_detected: str | None = Field(default=None, max_length=12)
    language_probability: float | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    speech_seconds: float | None = Field(default=None, ge=0)
    model: str = Field(default="", max_length=60)
    device: str = Field(default="", max_length=200)
    seconds_taken: float | None = Field(default=None, ge=0)


class FailIn(BaseModel):
    reason: str = Field(max_length=400)
    retry: bool = True


async def current_agent(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Agent:
    scheme, _, token = (authorization or "").partition(" ")
    agent = (
        await agents.authenticate(session, token.strip())
        if scheme.lower() == "bearer"
        else None
    )
    if agent is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or revoked agent token")
    return agent


CurrentAgent = Annotated[Agent, Depends(current_agent)]


@router.post("/claim")
async def claim(
    body: ClaimIn,
    agent: CurrentAgent,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    await agents.note_detail(session, agent, body.agent.model_dump())
    return {
        "jobs": await agents.claim(agent, limit=body.max),
        "progress": await agents.progress(),
    }


@router.post("/jobs/{job_id}/result")
async def result(job_id: str, body: ResultIn, agent: CurrentAgent) -> dict:
    try:
        outcome = await agents.submit(
            agent,
            job_id,
            AgentResult(
                segments=[s.model_dump() for s in body.segments],
                **body.model_dump(exclude={"segments"}),
            ),
        )
    except LeaseGone as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"outcome": outcome}


@router.post("/jobs/{job_id}/fail")
async def give_back(job_id: str, body: FailIn, agent: CurrentAgent) -> dict:
    try:
        await agents.fail(agent, job_id, reason=body.reason, retry=body.retry)
    except LeaseGone as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"outcome": "returned" if body.retry else "recorded"}


@router.get("/progress")
async def progress(agent: CurrentAgent) -> dict:
    return {"progress": await agents.progress()}


@router.get("/download/{name}", response_class=PlainTextResponse)
async def download(
    name: str, request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> PlainTextResponse:
    if name not in _DOWNLOADS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
    body = (_DIST / name).read_text(encoding="utf-8")
    return PlainTextResponse(
        body.replace("__C2W_SERVER__", await _public_address(request, session)),
        media_type=_DOWNLOADS[name],
    )


async def _public_address(request: Request, session: AsyncSession) -> str:
    """Where an agent should reach this console: the tunnel, when there is one.

    The request's own address is the fallback, and behind the tunnel it is the
    wrong one -- cloudflared talks to the API over plain HTTP on loopback.
    """
    if await settings_service.get_bool(session, "tunnel.enabled"):
        hostname = (await settings_service.get_str(session, "tunnel.hostname")).strip()
        if hostname:
            return f"https://{hostname}"
    return str(request.base_url).rstrip("/")
