"""Local access-controlled Discord gallery. Run: python -m uvicorn webapp:app --reload."""
import json
import mimetypes
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

load_dotenv()
ROOT = Path(__file__).parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", ROOT / "storage"))
ACTIVE_GUILDS_FILE = STORAGE_DIR / "active_guilds.json"
GUILD_ID_RE, IMAGE_NAME_RE = re.compile(r"^\d+$"), re.compile(r"^[a-z0-9_-]{1,32}$")
SESSION_COOKIE, SESSION_TTL_SECONDS = "jasmin_session", 8 * 60 * 60
ACTIVE_GUILD_MAX_AGE_SECONDS = int(os.getenv("ACTIVE_GUILD_MAX_AGE_SECONDS", "120"))
DISCORD_API = "https://discord.com/api/v10"


@dataclass
class Session:
    state: str
    created_at: float
    access_token: str | None = None
    user: dict | None = None
    # Cache for Discord guild list to prevent rate limits on image loads
    guild_ids: set[str] | None = None
    guilds_checked_at: float = 0.0


class DiscordAuthError(Exception):
    pass


class DiscordAPIError(Exception):
    pass


sessions: dict[str, Session] = {}
app = FastAPI(title="jasmin bot gallery")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")


def session_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise HTTPException(500, "SESSION_SECRET is missing from .env.")
    return URLSafeTimedSerializer(secret, salt="jasmin-session")


def oauth_config() -> tuple[str, str, str]:
    client_id = os.getenv("DISCORD_CLIENT_ID")
    secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect = os.getenv("DISCORD_REDIRECT_URI")

    if not client_id or not secret or not redirect:
        raise HTTPException(500, "Discord OAuth is not configured.")
    if not redirect.startswith(("http://", "https://")):
        raise HTTPException(500, "DISCORD_REDIRECT_URI must be a full URL.")
    if not os.getenv("SESSION_SECRET"):
        raise HTTPException(500, "SESSION_SECRET is missing from .env.")
    return client_id, secret, redirect


def cleanup_sessions() -> None:
    now = time.time()
    for key, value in list(sessions.items()):
        if now - value.created_at > SESSION_TTL_SECONDS:
            sessions.pop(key, None)


def session_for(request: Request) -> tuple[str, Session] | None:
    cleanup_sessions()
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        key = session_serializer().loads(raw, max_age=SESSION_TTL_SECONDS)
    except BadSignature:
        return None
    except Exception:
        return None
    if not isinstance(key, str) or key not in sessions:
        return None
    return key, sessions[key]


def set_session_cookie(response, key: str) -> None:
    token = session_serializer().dumps(key)
    redirect = os.getenv("DISCORD_REDIRECT_URI", "")
    is_local = "127.0.0.1" in redirect or "localhost" in redirect
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        secure=not is_local,          # ← change from hardcoded False
        max_age=SESSION_TTL_SECONDS,
    )


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def registered_ids() -> set[str]:
    if not STORAGE_DIR.is_dir():
        return set()
    return {p.name for p in STORAGE_DIR.iterdir()
            if p.is_dir() and GUILD_ID_RE.fullmatch(p.name) and (p / "guild.json").is_file()}


def active_ids() -> set[str]:
    data = read_json(ACTIVE_GUILDS_FILE)
    if not data:
        return set()
    updated = data.get("updated_at")
    if not isinstance(updated, (int, float)) or time.time() - updated > ACTIVE_GUILD_MAX_AGE_SECONDS:
        return set()
    ids = data.get("guild_ids")
    if not isinstance(ids, list):
        return set()
    return {x for x in ids if isinstance(x, str) and GUILD_ID_RE.fullmatch(x)}


def directory(guild_id: str) -> Path:
    if not GUILD_ID_RE.fullmatch(guild_id) or not (STORAGE_DIR / guild_id / "guild.json").is_file():
        raise HTTPException(404)
    return STORAGE_DIR / guild_id


def info(guild_id: str) -> dict:
    value = read_json(directory(guild_id) / "guild.json")
    if value is None:
        raise HTTPException(404)
    return value


def images(guild_id: str) -> list[dict]:
    result, folder = [], directory(guild_id)
    for meta_file in folder.glob("*.json"):
        if meta_file.name in {"guild.json", "saves.json"}:
            continue
        meta = read_json(meta_file) or {}
        name, ext = meta.get("name"), meta.get("ext")
        if (isinstance(name, str) and IMAGE_NAME_RE.fullmatch(name)
                and isinstance(ext, str) and ext and (folder / f"{name}.{ext}").is_file()):
            meta["tags"] = meta.get("tags") or []
            result.append(meta)
    return sorted(result, key=lambda x: x["name"])

def cards(ids: set[str], user_id: str | None) -> list[dict]:
    result = []
    for gid in ids:
        data = info(gid)
        folder = directory(gid)
        storage = data.get("storage") or {}
        saves = read_json(folder / "saves.json") or {}
        result.append({
            "id": gid,
            "name": data.get("name") or "Server",
            "image_count": data.get("image_count", 0),
            "used_bytes": storage.get("used_bytes", 0),
            "limit_bytes": storage.get("limit_bytes", 0),
            "member_saves": sum(x.get("count", 0) for x in saves.values() if isinstance(x, dict)),
            "is_whitelisted": whitelist_contains(data.get("whitelist"), user_id)
        })
    return sorted(result, key=lambda x: x["name"].lower())

def whitelist_contains(whitelist, user_id: str | None) -> bool:
    try:
        wanted = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return False
    if wanted is None:
        return False
    for entry in whitelist or []:
        try:
            if int(entry) == wanted:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def discord_get(path: str, token: str) -> dict | list:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(DISCORD_API + path, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            raise DiscordAuthError()
        if response.status_code == 429:
            raise DiscordAPIError("Rate limited")
        if response.status_code != 200:
            raise DiscordAPIError(f"Discord API returned {response.status_code}")
        return response.json()
    except httpx.HTTPError as exc:
        raise DiscordAPIError("Discord verification failed") from exc


async def authorized(request: Request) -> tuple[Session, set[str]] | None:
    found = session_for(request)
    if not found or not found[1].access_token:
        return None
        
    session = found[1]
    now = time.time()
    
    # Cache guild list for 60 seconds to prevent rate limits when loading galleries
    if session.guild_ids is None or now - session.guilds_checked_at > 60:
        try:
            guilds = await discord_get("/users/@me/guilds", session.access_token)
        except DiscordAuthError:
            # Token is dead, kill session
            sessions.pop(found[0], None)
            return None
        except DiscordAPIError as exc:
            # Temporary Discord error, don't kill the session, just fail this request
            raise HTTPException(503, "Discord is currently unavailable. Please try again.") from exc
            
        if not isinstance(guilds, list):
            sessions.pop(found[0], None)
            return None
            
        session.guild_ids = {g.get("id") for g in guilds if isinstance(g, dict) and isinstance(g.get("id"), str)}
        session.guilds_checked_at = now
        
    return session, session.guild_ids & active_ids() & registered_ids()


@app.get("/")
async def home(request: Request):
    access = await authorized(request)
    if not access:
        return templates.TemplateResponse(request=request, name="login.html", context={})
    session, ids = access
    
    user_id = session.user.get("id") if session.user else None
    guilds = cards(ids, user_id)
    
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": session.user or {},
        "guilds": guilds,
        "total_images": sum(x["image_count"] for x in guilds),
        "total_storage": sum(x["used_bytes"] for x in guilds),
    })


@app.get("/auth/discord/login")
async def login():
    client_id, _, redirect = oauth_config()
    key, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    sessions[key] = Session(state, time.time())
    response = RedirectResponse(
        "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }),
        302,
    )
    set_session_cookie(response, key)
    return response


@app.get("/auth/discord/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None):
    found = session_for(request)
    if not found or not code or not state or not secrets.compare_digest(found[1].state, state):
        raise HTTPException(400, "Invalid Discord sign-in state. Please try again.")
    client_id, secret, redirect = oauth_config()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(DISCORD_API + "/oauth2/token", data={
                "client_id": client_id,
                "client_secret": secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
            })
        payload = response.json()
        token = payload.get("access_token") if response.status_code == 200 else None
        if not isinstance(token, str):
            raise ValueError
        user = await discord_get("/users/@me", token)
        if not isinstance(user, dict):
            raise ValueError
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, DiscordAPIError, DiscordAuthError):
        sessions.pop(found[0], None)
        raise HTTPException(401, "Discord sign-in failed. Please try again.")
    found[1].access_token = token
    found[1].user = user
    found[1].state = ""
    return RedirectResponse("/", 302)


@app.post("/auth/logout")
async def logout(request: Request):
    found = session_for(request)
    if found:
        sessions.pop(found[0], None)
    response = RedirectResponse("/", 303)
    response.delete_cookie(SESSION_COOKIE)
    return response


async def require(request: Request, guild_id: str) -> tuple[Session, set[str]]:
    access = await authorized(request)
    if not access:
        raise HTTPException(403, "Sign in with Discord to view this gallery.")
    session, ids = access
    if guild_id not in ids:
        raise HTTPException(403, "You don't have access to that gallery.")
    return session, ids


@app.get("/guilds/{guild_id}")
async def gallery(request: Request, guild_id: str):
    session, _ = await require(request, guild_id)
    data = info(guild_id)
    folder = directory(guild_id)
    storage = data.get("storage") or {}
    saves = read_json(folder / "saves.json") or {}
    user_id = session.user.get("id") if session.user else None
    is_whitelisted = whitelist_contains(data.get("whitelist"), user_id)
    return templates.TemplateResponse(request=request, name="gallery.html", context={
        "guild": {
            "id": guild_id,
            "name": data.get("name") or "Server",
            "used_bytes": storage.get("used_bytes", 0),
            "limit_bytes": storage.get("limit_bytes", 0),
            "contributor_count": len(saves),
        },
        "images": images(guild_id),
        "is_whitelisted": is_whitelisted,
    })


@app.get("/guilds/{guild_id}/images/{name}")
async def image(request: Request, guild_id: str, name: str):
    await require(request, guild_id)
    if not IMAGE_NAME_RE.fullmatch(name):
        raise HTTPException(404)
    meta = read_json(directory(guild_id) / f"{name}.json")
    ext = meta.get("ext") if meta else None
    if not isinstance(ext, str) or not ext or "/" in ext or "\\" in ext:
        raise HTTPException(404)
    path = directory(guild_id) / f"{name}.{ext}"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@app.post("/guilds/{guild_id}/images/{name}/delete")
async def delete_image(request: Request, guild_id: str, name: str):
    session, _ = await require(request, guild_id)
    if not IMAGE_NAME_RE.fullmatch(name):
        raise HTTPException(404)
    data = info(guild_id)
    if not whitelist_contains(data.get("whitelist"), session.user.get("id") if session.user else None):
        raise HTTPException(403, "Only whitelisted members can delete images.")

    try:
        from cogs.imagestorage import Storage as ImageStorage
    except ImportError:
        raise HTTPException(503, "Image storage is not available on the web process yet.")

    store = ImageStorage(STORAGE_DIR).guild(int(guild_id))
    deleted = await store.delete_image(name)
    if deleted is None:
        raise HTTPException(404)
    return RedirectResponse(f"/guilds/{guild_id}", 303)