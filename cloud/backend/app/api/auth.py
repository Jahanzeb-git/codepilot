"""
/app/auth/api/auth.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles routing for authorization and authentication.
Licensed: MIT
"""
# --IMPORTS--
from fastapi import APIRouter, Depends, status, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from schemas.schemas import UserCreate
from db.database import get_db_session
from crud.repository import create_user, get_user_by_email
from core.security import get_pass_hash, create_access_token
from pydantic import BaseModel
import os
import re
import uuid

class GuestLogin(BaseModel):
    access_code: str

# --CONFIG--
router = APIRouter(prefix="/auth")

# A long-lived, first-party-looking identity cookie for guests. Separate
# from the short-lived (30 min) access_token JWT on purpose: this is what
# lets a *returning* guest keep their existing account/quota instead of
# getting a brand new one every time their access_token expires and the
# frontend calls this endpoint again.
GUEST_COOKIE_NAME = "guest_id"
GUEST_COOKIE_MAX_AGE = 3600 * 24 * 365  # 1 year
_GUEST_ID_RE = re.compile(r"^[a-f0-9]{16}$")

# --ENDPOINTS--
@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate, 
    db: AsyncSession = Depends(get_db_session)
    ):
    raise HTTPException(status_code=403, detail="Registration is disabled for this portfolio instance.")

@router.post("/login")
async def login(
    request: Request,
    db: AsyncSession = Depends(get_db_session)
    ):
    raise HTTPException(status_code=403, detail="Standard login is disabled. Please use the guest access code.")

@router.post("/guest", status_code=status.HTTP_200_OK)
async def guest_login(
    payload: GuestLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db_session)
):
    secret = os.environ.get("PORTFOLIO_ACCESS_CODE", "hireme")
    if payload.access_code.lower() != secret.lower():
        raise HTTPException(status_code=401, detail="Invalid access code")

    # If this browser already has our long-lived guest cookie AND that
    # guest account still exists, reuse it -- same user, same quota,
    # instead of minting a fresh account (and a fresh 3-hour/90-hour
    # allowance) every time the short-lived access_token expires.
    guest_id = request.cookies.get(GUEST_COOKIE_NAME)
    email = None
    if guest_id and _GUEST_ID_RE.match(guest_id):
        candidate_email = f"guest-{guest_id}@portfolio.codepilot"
        if await get_user_by_email(db, candidate_email) is not None:
            email = candidate_email

    if email is None:
        # First time we've seen this browser (or the cookie was cleared,
        # or that guest row is somehow gone) -- mint a new stable id and
        # remember it for next time.
        guest_id = uuid.uuid4().hex[:16]
        email = f"guest-{guest_id}@portfolio.codepilot"
        dummy_pass = get_pass_hash(uuid.uuid4().hex)
        await create_user(db, email, dummy_pass)

    # samesite=none + secure is required for this cookie to round-trip at
    # all, since the frontend (netlify.app) and this API (fly.dev) are
    # different origins -- the frontend's fetch call to this endpoint MUST
    # use `credentials: "include"` or the browser will neither send nor
    # store this cookie, and every login will look like a first visit.
    response.set_cookie(
        key=GUEST_COOKIE_NAME,
        value=guest_id,
        httponly=True,
        samesite="none",
        secure=True,
        max_age=GUEST_COOKIE_MAX_AGE,
    )

    token = create_access_token(email)
    return {"access_token": token, "token_type": "bearer", "email": email}

# --END OF FILE--
