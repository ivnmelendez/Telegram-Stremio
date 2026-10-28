"""Short activation codes for the Nuvio app.

Each API token can have a short code. A person types it once in the app, which asks /app/redeem/{code} for
that token's addon link and installs it, so nobody has to paste a long manifest URL. Codes are stored on the
token document (field ``app_code``); no other part of the token changes.
"""
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request

from Backend import db
from Backend.fastapi.security.credentials import require_auth
from Backend.helper.settings_manager import SettingsManager

router = APIRouter(tags=["App codes"])

#----- No 0/O or 1/I, so a code read off a screen can't be mistyped
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6

#----- Per-IP throttle on redeeming: guessing a code isn't practical
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 60
_attempts: dict = defaultdict(deque)


def _tokens():
    return db.dbs["tracking"]["api_tokens"]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle(ip: str) -> None:
    now = time.monotonic()
    window = _attempts[ip]
    while window and now - window[0] > _WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts, try again in a minute")
    window.append(now)


async def _new_code() -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        if not await _tokens().find_one({"app_code": code}):
            return code
    raise HTTPException(status_code=500, detail="Could not generate a unique code")


#----- Admin: every token with its code (a code is made for any token that has none). Open it in the browser while
#----- logged in to the panel.
@router.get("/api/app-codes")
async def list_app_codes(_: bool = Depends(require_auth)):
    codes = []
    async for doc in _tokens().find({}):
        code = doc.get("app_code")
        if not code:
            code = await _new_code()
            await _tokens().update_one({"_id": doc["_id"]}, {"$set": {"app_code": code}})
        codes.append({"name": doc.get("name"), "code": code})
    return {"codes": codes}


#----- Admin: replace one token's code (the old one stops working)
@router.post("/api/app-codes/{token}/reset")
async def reset_app_code(token: str, _: bool = Depends(require_auth)):
    code = await _new_code()
    result = await _tokens().update_one({"token": token}, {"$set": {"app_code": code}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"code": code}


#----- Public: what the app asks with the code a person typed
@router.get("/app/redeem/{code}")
async def redeem_app_code(code: str, request: Request):
    _throttle(_client_ip(request))
    doc = await _tokens().find_one({"app_code": code.strip().upper()})
    if not doc or not doc.get("token"):
        raise HTTPException(status_code=404, detail="Invalid code")
    base_url = SettingsManager.current().base_url
    return {
        "name": doc.get("name"),
        "manifest_url": f"{base_url}/stremio/{doc['token']}/manifest.json",
    }
