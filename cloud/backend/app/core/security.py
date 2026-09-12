"""
/app/core/security.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles dependency utilities for authorization.
Licensed: MIT
"""
# --IMPORTS--
import os, datetime
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from authlib.jose import jwt
from db.database import get_db_session

# --CONFIG--
ALGORITHM = os.environ.get("ALGORITHM", "HS256")
ACCESS_TOKEN_EXP_MINUTES = int(os.environ.get("EXP_MINUTES", "30"))
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is not set.")

ph = PasswordHasher()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# --UTILITIES--
def get_pass_hash(password: str) -> str:
    return ph.hash(password)

def verify_pass(password: str, hashed_password: str) -> bool:
    try:
        return ph.verify(hashed_password, password)
    except VerifyMismatchError:
        return False

def create_access_token(email: str) -> str:
    header = {
        "alg": ALGORITHM
    }
    payload = {
        "sub": email,
        "exp": int((datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_EXP_MINUTES)).timestamp())
    }
    _token_bytes = jwt.encode(header, payload, SECRET_KEY)
    return _token_bytes.decode('utf-8')

def decode_access_token(token: str):
    claims = jwt.decode(token, SECRET_KEY)
    claims.validate() # validate expiry.
    return claims

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session)
    ):
    try:
        email = decode_access_token(token)["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    from crud.repository import get_user_by_email
    user = await get_user_by_email(db, email)
    return user

async def get_current_user_ws(
    token: str,
    db: AsyncSession
):
    try:
        email = decode_access_token(token)["sub"]
    except Exception:
        raise ValueError("Invalid or expired token")
    
    from crud.repository import get_user_by_email
    user = await get_user_by_email(db, email)
    return user

def create_connect_ticket(email: str) -> str:
    """Creates a short-lived (5 min) JWT specifically for redirecting to workspaces."""
    header = {"alg": ALGORITHM}
    payload = {
        "sub": email,
        "type": "connect_ticket",
        "exp": int((datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).timestamp())
    }
    _token_bytes = jwt.encode(header, payload, SECRET_KEY)
    return _token_bytes.decode('utf-8')

async def get_user_from_connect_ticket(
    ticket: str,
    db: AsyncSession = Depends(get_db_session)
    ):
    """
    Validates a short-lived connect ticket from query params.
    Prevents long-lived session tokens from sitting in the URL history.
    """
    try:
        claims = jwt.decode(ticket, SECRET_KEY)
        claims.validate() # validates expiry
        if claims.get("type") != "connect_ticket":
            raise ValueError("Invalid token type")
        email = claims["sub"]
    except Exception as e:
        import logging
        logging.error(f"Ticket validation failed: {e}")
        raise HTTPException(status_code=401, detail=f"Invalid or expired ticket in URL")
    
    from crud.repository import get_user_by_email
    user = await get_user_by_email(db, email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# --END OF FILE--
