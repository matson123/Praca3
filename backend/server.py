from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import uuid
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Literal

import bcrypt
import jwt
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, Query
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from poland_geo import is_in_poland

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@mapmeet.pl")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin123!")

CATEGORIES = ["Sport", "Kultura", "Muzyka", "Jedzenie", "Nauka", "Gry", "Outdoor", "Inne"]

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="MapMeet API")
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response(
        content='{"detail":"Zbyt wiele żądań. Spróbuj ponownie później."}',
        status_code=429,
        media_type="application/json",
    )


api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("mapmeet")


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Brak autoryzacji")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token wygasł")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Nieprawidłowy token")
    user = await db.users.find_one({"id": payload["sub"]})
    if not user or user.get("is_blocked"):
        raise HTTPException(status_code=401, detail="Użytkownik nieaktywny")
    user.pop("password_hash", None)
    user.pop("_id", None)
    return user


async def get_optional_user(request: Request) -> Optional[dict]:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


def set_auth_cookie(response: Response, token: str):
    # Env-driven so the same code works both in HTTPS/cross-site (Emergent)
    # and in http://localhost (local dev).
    secure = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
    samesite = os.environ.get("COOKIE_SAMESITE", "none").lower()
    # Browsers require secure=True whenever SameSite=None.
    if samesite == "none":
        secure = True
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        max_age=ACCESS_TOKEN_MINUTES * 60,
        path="/",
    )


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    nick: str = Field(min_length=2, max_length=40)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdate(BaseModel):
    nick: Optional[str] = Field(default=None, min_length=2, max_length=40)
    avatar_url: Optional[str] = None
    bio: Optional[str] = Field(default=None, max_length=500)


class EventCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    category: Literal["Sport", "Kultura", "Muzyka", "Jedzenie", "Nauka", "Gry", "Outdoor", "Inne"]
    starts_at: str
    max_participants: int = Field(ge=2, le=1000)
    is_public: bool = True
    requires_approval: bool = False
    comments_enabled: bool = True
    lat: float
    lon: float
    location_name: Optional[str] = Field(default=None, max_length=200)


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ReportCreate(BaseModel):
    target_type: Literal["event", "comment"]
    target_id: str
    reason: str = Field(min_length=3, max_length=500)


async def create_notification(user_id: str, ntype: str, title: str, body: str, event_id: Optional[str] = None):
    """Best-effort notification insert."""
    if not user_id:
        return
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": ntype,
        "title": title,
        "body": body,
        "event_id": event_id,
        "is_read": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.notifications.insert_one(doc)
    except Exception:
        pass


async def require_admin(user=Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Wymagane uprawnienia administratora")
    return user


def _user_public(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u["email"],
        "nick": u["nick"],
        "avatar_url": u.get("avatar_url"),
        "bio": u.get("bio"),
        "role": u.get("role", "user"),
        "created_at": u["created_at"],
    }


def _is_archived(starts_iso: Optional[str]) -> bool:
    if not starts_iso:
        return False
    try:
        dt = datetime.fromisoformat(starts_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


async def _event_out(ev: dict) -> dict:
    organizer = await db.users.find_one({"id": ev["organizer_id"]})
    ev = dict(ev)
    ev.pop("_id", None)
    ev["organizer_nick"] = organizer["nick"] if organizer else "Nieznany"
    ev["organizer_avatar"] = organizer.get("avatar_url") if organizer else None
    ev["participants_count"] = len(ev.get("participants", []))
    ev["is_archived"] = _is_archived(ev.get("starts_at"))
    return ev


@api.post("/auth/register")
@limiter.limit("10/minute")
async def register(request: Request, response: Response, payload: RegisterIn):
    email = payload.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Konto z tym adresem już istnieje")
    user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "nick": payload.nick.strip(),
        "password_hash": hash_password(payload.password),
        "avatar_url": None,
        "bio": None,
        "role": "user",
        "is_blocked": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(user)
    token = create_access_token(user["id"], user["email"], user["role"])
    set_auth_cookie(response, token)
    return {"user": _user_public(user), "token": token}


@api.post("/auth/login")
@limiter.limit("20/minute")
async def login(request: Request, response: Response, payload: LoginIn):
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Nieprawidłowy email lub hasło")
    if user.get("is_blocked"):
        raise HTTPException(status_code=403, detail="Konto zostało zablokowane")
    token = create_access_token(user["id"], user["email"], user.get("role", "user"))
    set_auth_cookie(response, token)
    return {"user": _user_public(user), "token": token}


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@api.get("/auth/me")
async def me(user=Depends(get_current_user)):
    return {"user": user}


@api.patch("/auth/profile")
async def update_profile(payload: ProfileUpdate, user=Depends(get_current_user)):
    update = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
    updated = await db.users.find_one({"id": user["id"]})
    return {"user": _user_public(updated)}


@api.post("/events")
@limiter.limit("30/minute")
async def create_event(request: Request, payload: EventCreate, user=Depends(get_current_user)):
    if not is_in_poland(payload.lat, payload.lon):
        raise HTTPException(status_code=400, detail="Wydarzenie musi być umieszczone w granicach Polski")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    daily = await db.events.count_documents(
        {"organizer_id": user["id"], "created_at": {"$gte": since.isoformat()}}
    )
    if daily >= 10:
        raise HTTPException(status_code=429, detail="Przekroczono dzienny limit wydarzeń (10/dobę)")

    try:
        starts_dt = datetime.fromisoformat(payload.starts_at.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowa data")

    ev = {
        "id": str(uuid.uuid4()),
        "title": payload.title.strip(),
        "description": payload.description.strip(),
        "category": payload.category,
        "starts_at": starts_dt.isoformat(),
        "max_participants": payload.max_participants,
        "is_public": payload.is_public,
        "requires_approval": payload.requires_approval,
        "comments_enabled": payload.comments_enabled,
        "lat": payload.lat,
        "lon": payload.lon,
        "location": {"type": "Point", "coordinates": [payload.lon, payload.lat]},
        "location_name": payload.location_name,
        "organizer_id": user["id"],
        "participants": [user["id"]],
        "pending": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.events.insert_one(ev)
    return await _event_out(ev)


@api.get("/events")
async def list_events(
    category: Optional[str] = None,
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    only_public: Optional[bool] = None,
    include_archived: bool = False,
    near_lat: Optional[float] = None,
    near_lon: Optional[float] = None,
    max_km: Optional[float] = None,
    search: Optional[str] = None,
    user=Depends(get_optional_user),
):
    q: dict = {}
    if category and category in CATEGORIES:
        q["category"] = category
    if only_public is True:
        q["is_public"] = True
    now_iso = datetime.now(timezone.utc).isoformat()
    date_q: dict = {}
    if not include_archived:
        date_q["$gte"] = now_iso
    if from_date:
        date_q["$gte"] = from_date
    if to_date:
        date_q["$lte"] = to_date
    if date_q:
        q["starts_at"] = date_q
    if search:
        q["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"location_name": {"$regex": search, "$options": "i"}},
        ]
    if near_lat is not None and near_lon is not None and max_km:
        q["location"] = {
            "$near": {
                "$geometry": {"type": "Point", "coordinates": [near_lon, near_lat]},
                "$maxDistance": max_km * 1000,
            }
        }
    if not user:
        q["is_public"] = True

    cursor = db.events.find(q).limit(500)
    result = []
    async for ev in cursor:
        if not ev.get("is_public") and user:
            if user["id"] != ev["organizer_id"] and user["id"] not in ev.get("participants", []):
                continue
        result.append(await _event_out(ev))
    return {"events": result}


@api.get("/events/{event_id}")
async def get_event(event_id: str, user=Depends(get_optional_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if not ev.get("is_public"):
        if not user or (user["id"] != ev["organizer_id"] and user["id"] not in ev.get("participants", [])):
            raise HTTPException(status_code=403, detail="Brak dostępu")
    return await _event_out(ev)


@api.delete("/events/{event_id}")
async def delete_event(event_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Brak uprawnień")
    await db.events.delete_one({"id": event_id})
    await db.comments.delete_many({"event_id": event_id})
    # Auto-resolve any open reports referencing this event.
    await db.reports.update_many(
        {"target_type": "event", "target_id": event_id, "status": "open"},
        {"$set": {"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat(), "resolved_by": user["id"]}},
    )
    return {"ok": True}


@api.post("/events/{event_id}/join")
async def join_event(event_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if _is_archived(ev.get("starts_at")):
        raise HTTPException(status_code=400, detail="Wydarzenie już się zakończyło")
    if user["id"] in ev.get("participants", []):
        raise HTTPException(status_code=400, detail="Już dołączyłeś do tego wydarzenia")
    if user["id"] in ev.get("pending", []):
        raise HTTPException(status_code=400, detail="Twoje zgłoszenie oczekuje na akceptację")
    if len(ev.get("participants", [])) >= ev["max_participants"]:
        raise HTTPException(status_code=400, detail="Brak wolnych miejsc")

    if ev.get("requires_approval") and ev["organizer_id"] != user["id"]:
        await db.events.update_one({"id": event_id}, {"$addToSet": {"pending": user["id"]}})
        await create_notification(
            ev["organizer_id"],
            "join_request",
            "Nowe zgłoszenie do wydarzenia",
            f"{user['nick']} chce dołączyć do „{ev['title']}”",
            event_id,
        )
        return {"status": "pending"}
    await db.events.update_one({"id": event_id}, {"$addToSet": {"participants": user["id"]}})
    if ev["organizer_id"] != user["id"]:
        await create_notification(
            ev["organizer_id"],
            "joined",
            "Nowy uczestnik",
            f"{user['nick']} dołączył do „{ev['title']}”",
            event_id,
        )
    return {"status": "joined"}


@api.post("/events/{event_id}/leave")
async def leave_event(event_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] == user["id"]:
        raise HTTPException(status_code=400, detail="Organizator nie może opuścić własnego wydarzenia")
    await db.events.update_one(
        {"id": event_id},
        {"$pull": {"participants": user["id"], "pending": user["id"]}},
    )
    return {"ok": True}


@api.post("/events/{event_id}/approve/{user_id}")
async def approve_join(event_id: str, user_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Tylko organizator może akceptować zgłoszenia")
    if user_id not in ev.get("pending", []):
        raise HTTPException(status_code=400, detail="Brak takiego zgłoszenia")
    if len(ev.get("participants", [])) >= ev["max_participants"]:
        raise HTTPException(status_code=400, detail="Brak wolnych miejsc")
    await db.events.update_one(
        {"id": event_id},
        {"$pull": {"pending": user_id}, "$addToSet": {"participants": user_id}},
    )
    await create_notification(
        user_id,
        "approved",
        "Zgłoszenie zaakceptowane",
        f"Zostałeś dopisany do „{ev['title']}”",
        event_id,
    )
    return {"ok": True}


@api.post("/events/{event_id}/reject/{user_id}")
async def reject_join(event_id: str, user_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień")
    await db.events.update_one({"id": event_id}, {"$pull": {"pending": user_id}})
    await create_notification(
        user_id,
        "rejected",
        "Zgłoszenie odrzucone",
        f"Twoje zgłoszenie do „{ev['title']}” zostało odrzucone",
        event_id,
    )
    return {"ok": True}


@api.get("/events/{event_id}/participants")
async def event_participants(event_id: str):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    ids = list(set(ev.get("participants", []) + ev.get("pending", [])))
    users = await db.users.find({"id": {"$in": ids}}).to_list(1000)
    people = {u["id"]: {"id": u["id"], "nick": u["nick"], "avatar_url": u.get("avatar_url")} for u in users}
    return {
        "participants": [people[i] for i in ev.get("participants", []) if i in people],
        "pending": [people[i] for i in ev.get("pending", []) if i in people],
    }


@api.get("/events/{event_id}/comments")
async def list_comments(event_id: str):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    cs = await db.comments.find({"event_id": event_id}).sort("created_at", 1).to_list(1000)
    user_ids = list({c["author_id"] for c in cs})
    users = await db.users.find({"id": {"$in": user_ids}}).to_list(1000)
    umap = {u["id"]: u for u in users}
    out = []
    for c in cs:
        u = umap.get(c["author_id"])
        out.append({
            "id": c["id"],
            "text": c["text"],
            "created_at": c["created_at"],
            "author": {
                "id": c["author_id"],
                "nick": u["nick"] if u else "Nieznany",
                "avatar_url": u.get("avatar_url") if u else None,
            },
        })
    return {"comments": out}


@api.post("/events/{event_id}/comments")
@limiter.limit("30/minute")
async def add_comment(request: Request, event_id: str, payload: CommentCreate, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if not ev.get("comments_enabled", True):
        raise HTTPException(status_code=400, detail="Komentarze są wyłączone dla tego wydarzenia")
    comment = {
        "id": str(uuid.uuid4()),
        "event_id": event_id,
        "author_id": user["id"],
        "text": payload.text.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.comments.insert_one(comment)
    # Notify organizer (if not the author).
    if ev["organizer_id"] != user["id"]:
        await create_notification(
            ev["organizer_id"],
            "comment",
            "Nowy komentarz",
            f"{user['nick']}: „{payload.text.strip()[:80]}”",
            event_id,
        )
    return {
        "id": comment["id"],
        "text": comment["text"],
        "created_at": comment["created_at"],
        "author": {
            "id": user["id"],
            "nick": user["nick"],
            "avatar_url": user.get("avatar_url"),
        },
    }


@api.get("/me/events")
async def my_events(user=Depends(get_current_user)):
    organized = await db.events.find({"organizer_id": user["id"]}).sort("starts_at", -1).to_list(500)
    joined = await db.events.find({
        "participants": user["id"],
        "organizer_id": {"$ne": user["id"]},
    }).sort("starts_at", -1).to_list(500)
    return {
        "organized": [await _event_out(e) for e in organized],
        "joined": [await _event_out(e) for e in joined],
    }


@api.get("/categories")
async def categories():
    return {"categories": CATEGORIES}


# --------------------------------------------------------------------------- #
# Reports (user-facing)
# --------------------------------------------------------------------------- #
@api.post("/reports")
@limiter.limit("20/minute")
async def create_report(request: Request, payload: ReportCreate, user=Depends(get_current_user)):
    # Validate target exists.
    if payload.target_type == "event":
        target = await db.events.find_one({"id": payload.target_id})
    else:
        target = await db.comments.find_one({"id": payload.target_id})
    if not target:
        raise HTTPException(status_code=404, detail="Nie znaleziono zgłaszanego obiektu")

    # Prevent duplicate open reports from same user for same target.
    dup = await db.reports.find_one({
        "target_type": payload.target_type,
        "target_id": payload.target_id,
        "reporter_id": user["id"],
        "status": "open",
    })
    if dup:
        raise HTTPException(status_code=400, detail="Już zgłosiłeś ten obiekt")

    report = {
        "id": str(uuid.uuid4()),
        "target_type": payload.target_type,
        "target_id": payload.target_id,
        "reason": payload.reason.strip(),
        "reporter_id": user["id"],
        "reporter_nick": user["nick"],
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "resolved_by": None,
    }
    await db.reports.insert_one(report)
    report.pop("_id", None)
    return report


# --------------------------------------------------------------------------- #
# Admin endpoints
# --------------------------------------------------------------------------- #
@api.get("/admin/stats")
async def admin_stats(admin=Depends(require_admin)):
    now_iso = datetime.now(timezone.utc).isoformat()
    users_total = await db.users.count_documents({})
    users_blocked = await db.users.count_documents({"is_blocked": True})
    events_total = await db.events.count_documents({})
    events_upcoming = await db.events.count_documents({"starts_at": {"$gte": now_iso}})
    events_archived = events_total - events_upcoming
    comments_total = await db.comments.count_documents({})
    reports_open = await db.reports.count_documents({"status": "open"})
    reports_resolved = await db.reports.count_documents({"status": "resolved"})
    # Events by category
    by_cat = {}
    for c in CATEGORIES:
        by_cat[c] = await db.events.count_documents({"category": c})
    return {
        "users": {"total": users_total, "blocked": users_blocked},
        "events": {"total": events_total, "upcoming": events_upcoming, "archived": events_archived, "by_category": by_cat},
        "comments": {"total": comments_total},
        "reports": {"open": reports_open, "resolved": reports_resolved},
    }


@api.get("/admin/users")
async def admin_list_users(
    admin=Depends(require_admin),
    search: Optional[str] = None,
    blocked: Optional[bool] = None,
):
    q: dict = {}
    if search:
        q["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"nick": {"$regex": search, "$options": "i"}},
        ]
    if blocked is not None:
        q["is_blocked"] = blocked
    users = await db.users.find(q).sort("created_at", -1).limit(500).to_list(500)
    out = []
    for u in users:
        out.append({
            "id": u["id"],
            "email": u["email"],
            "nick": u["nick"],
            "avatar_url": u.get("avatar_url"),
            "bio": u.get("bio"),
            "role": u.get("role", "user"),
            "is_blocked": bool(u.get("is_blocked")),
            "created_at": u["created_at"],
        })
    return {"users": out}


@api.post("/admin/users/{user_id}/block")
async def admin_toggle_block(user_id: str, admin=Depends(require_admin)):
    target = await db.users.find_one({"id": user_id})
    if not target:
        raise HTTPException(status_code=404, detail="Użytkownik nie istnieje")
    if target.get("role") == "admin":
        raise HTTPException(status_code=400, detail="Nie można zablokować administratora")
    new_state = not bool(target.get("is_blocked"))
    await db.users.update_one({"id": user_id}, {"$set": {"is_blocked": new_state}})
    return {"id": user_id, "is_blocked": new_state}


@api.get("/admin/reports")
async def admin_list_reports(
    admin=Depends(require_admin),
    status: Optional[str] = None,
):
    q: dict = {}
    if status in ("open", "resolved"):
        q["status"] = status
    reports = await db.reports.find(q).sort("created_at", -1).limit(500).to_list(500)
    out = []
    for r in reports:
        r.pop("_id", None)
        # Enrich with target snapshot
        if r["target_type"] == "event":
            ev = await db.events.find_one({"id": r["target_id"]})
            if ev:
                r["target_snapshot"] = {
                    "title": ev.get("title"),
                    "category": ev.get("category"),
                    "organizer_id": ev.get("organizer_id"),
                    "location_name": ev.get("location_name"),
                    "exists": True,
                }
            else:
                r["target_snapshot"] = {"exists": False}
        else:
            c = await db.comments.find_one({"id": r["target_id"]})
            if c:
                r["target_snapshot"] = {
                    "text": c.get("text"),
                    "author_id": c.get("author_id"),
                    "event_id": c.get("event_id"),
                    "exists": True,
                }
            else:
                r["target_snapshot"] = {"exists": False}
        out.append(r)
    return {"reports": out}


@api.post("/admin/reports/{report_id}/resolve")
async def admin_resolve_report(report_id: str, admin=Depends(require_admin)):
    r = await db.reports.find_one({"id": report_id})
    if not r:
        raise HTTPException(status_code=404, detail="Zgłoszenie nie istnieje")
    await db.reports.update_one(
        {"id": report_id},
        {"$set": {
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": admin["id"],
        }},
    )
    return {"ok": True}


@api.delete("/admin/comments/{comment_id}")
async def admin_delete_comment(comment_id: str, admin=Depends(require_admin)):
    r = await db.comments.delete_one({"id": comment_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Komentarz nie istnieje")
    # Auto-resolve open reports on this comment.
    await db.reports.update_many(
        {"target_type": "comment", "target_id": comment_id, "status": "open"},
        {"$set": {"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat(), "resolved_by": admin["id"]}},
    )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #
@api.post("/events/{event_id}/invite")
async def generate_invite(event_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Tylko organizator może generować zaproszenia")
    token = ev.get("invite_token")
    if not token:
        token = secrets.token_urlsafe(12)
        await db.events.update_one({"id": event_id}, {"$set": {"invite_token": token}})
    return {"invite_token": token}


@api.delete("/events/{event_id}/invite")
async def revoke_invite(event_id: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"id": event_id})
    if not ev:
        raise HTTPException(status_code=404, detail="Wydarzenie nie istnieje")
    if ev["organizer_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Tylko organizator może wyłączać zaproszenia")
    await db.events.update_one({"id": event_id}, {"$unset": {"invite_token": ""}})
    return {"ok": True}


@api.get("/events/by_invite/{token}")
async def preview_invite(token: str):
    ev = await db.events.find_one({"invite_token": token})
    if not ev:
        raise HTTPException(status_code=404, detail="Zaproszenie nieaktywne")
    return await _event_out(ev)


@api.post("/events/by_invite/{token}/join")
async def join_by_invite(token: str, user=Depends(get_current_user)):
    ev = await db.events.find_one({"invite_token": token})
    if not ev:
        raise HTTPException(status_code=404, detail="Zaproszenie nieaktywne")
    if _is_archived(ev.get("starts_at")):
        raise HTTPException(status_code=400, detail="Wydarzenie już się zakończyło")
    if user["id"] in ev.get("participants", []):
        return {"status": "joined", "event_id": ev["id"]}
    if len(ev.get("participants", [])) >= ev["max_participants"]:
        raise HTTPException(status_code=400, detail="Brak wolnych miejsc")
    # Invite bypasses approval, but still respects capacity.
    await db.events.update_one(
        {"id": ev["id"]},
        {"$addToSet": {"participants": user["id"]}, "$pull": {"pending": user["id"]}},
    )
    if ev["organizer_id"] != user["id"]:
        await create_notification(
            ev["organizer_id"],
            "invite_joined",
            "Ktoś dołączył przez zaproszenie",
            f"{user['nick']} dołączył do „{ev['title']}” przez link",
            ev["id"],
        )
    return {"status": "joined", "event_id": ev["id"]}


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@api.get("/notifications")
async def list_notifications(limit: int = 50, user=Depends(get_current_user)):
    limit = max(1, min(200, limit))
    items = await db.notifications.find(
        {"user_id": user["id"]}
    ).sort("created_at", -1).limit(limit).to_list(limit)
    unread = await db.notifications.count_documents({"user_id": user["id"], "is_read": False})
    for n in items:
        n.pop("_id", None)
    return {"notifications": items, "unread": unread}


@api.post("/notifications/{notif_id}/read")
async def mark_read(notif_id: str, user=Depends(get_current_user)):
    r = await db.notifications.update_one(
        {"id": notif_id, "user_id": user["id"]},
        {"$set": {"is_read": True}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Powiadomienie nie istnieje")
    return {"ok": True}


@api.post("/notifications/read-all")
async def mark_all_read(user=Depends(get_current_user)):
    r = await db.notifications.update_many(
        {"user_id": user["id"], "is_read": False},
        {"$set": {"is_read": True}},
    )
    return {"updated": r.modified_count}


# --------------------------------------------------------------------------- #
# Calendar (upcoming events grouped by day)
# --------------------------------------------------------------------------- #
@api.get("/calendar")
async def calendar_view(days: int = 30, user=Depends(get_optional_user)):
    """Return upcoming events grouped by day. `days` = forward window (default 30)."""
    days = max(1, min(90, days))
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    q: dict = {"starts_at": {"$gte": now.isoformat(), "$lte": end.isoformat()}}
    if not user:
        q["is_public"] = True
    cursor = db.events.find(q).sort("starts_at", 1).limit(500)
    grouped: dict = {}
    async for ev in cursor:
        if not ev.get("is_public") and user:
            if user["id"] != ev["organizer_id"] and user["id"] not in ev.get("participants", []):
                continue
        e = await _event_out(ev)
        day = e["starts_at"][:10]  # YYYY-MM-DD
        grouped.setdefault(day, []).append(e)
    days_list = [{"date": d, "events": grouped[d]} for d in sorted(grouped.keys())]
    return {"days": days_list}


@api.get("/")
async def root():
    return {"app": "MapMeet", "status": "ok"}


@api.get("/poland/check")
async def poland_check(lat: float, lon: float):
    return {"in_poland": is_in_poland(lat, lon)}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_origin_regex=os.environ.get("CORS_ORIGIN_REGEX") or None,
    allow_methods=["*"],
    allow_headers=["*"],
)


SAMPLE_EVENTS = [
    ("Bieganie w Łazienkach", "Wspólny wieczorny bieg 5km po Łazienkach Królewskich. Tempo spokojne, dla każdego.", "Sport", 52.2149, 21.0359, "Warszawa – Łazienki Królewskie", 15),
    ("Wieczór planszówek", "Catan, Terraforming Mars i klasyki. Zapraszamy początkujących i weteranów.", "Gry", 50.0614, 19.9366, "Kraków – Rynek Główny", 12),
    ("Jazz Session na Starówce", "Jam session przy dobrej kawie i winie. Muzycy mile widziani z instrumentami.", "Muzyka", 51.1079, 17.0385, "Wrocław – Rynek", 40),
    ("Warsztaty pierogów", "Uczymy się lepić najlepsze pierogi z tradycyjnymi farszami. Wszystkie składniki wliczone.", "Jedzenie", 52.4064, 16.9252, "Poznań – Stary Rynek", 20),
    ("Zwiedzanie Trójmiasta", "Spacer po Głównym Mieście z lokalnym przewodnikiem. Historia w pigułce.", "Kultura", 54.3520, 18.6466, "Gdańsk – Długi Targ", 25),
    ("Meetup Pythonowy", "Prezentacje o async, typowaniu i FastAPI. Networking po sesji.", "Nauka", 51.7592, 19.4560, "Łódź – EC1", 60),
    ("Trekking w Beskidach", "Całodniowa wycieczka na Babią Górę. Trasa średnio wymagająca.", "Outdoor", 49.5732, 19.5286, "Zawoja", 15),
    ("Spotkanie fanów fotografii", "Fotospacer po nocnym Katowicach. Zabierz statyw!", "Inne", 50.2649, 19.0238, "Katowice – Rynek", 12),
    ("Turniej piłki nożnej 5x5", "Amatorski turniej na Orliku. Zapisy drużyn 5-osobowych.", "Sport", 51.2465, 22.5684, "Lublin – Park Ludowy", 30),
    ("Koncert kameralny", "Muzyka klasyczna w kameralnym gronie – Chopin, Debussy.", "Muzyka", 53.4285, 14.5528, "Szczecin – Filharmonia", 50),
    ("Kurs kaligrafii", "Podstawy kaligrafii nowoczesnej. Materiały zapewniamy.", "Kultura", 53.1325, 23.1688, "Białystok – Centrum", 10),
    ("Rowerowa pętla Ojcowska", "50 km na rowerze przez Ojcowski Park. Konieczne kaski.", "Outdoor", 50.2000, 19.8300, "Ojców", 20),
]


async def seed():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.events.create_index("id", unique=True)
    await db.events.create_index("organizer_id")
    await db.events.create_index("starts_at")
    await db.events.create_index("category")
    await db.events.create_index([("location", "2dsphere")])
    await db.comments.create_index("event_id")
    await db.reports.create_index("status")
    await db.reports.create_index("target_id")
    await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
    await db.notifications.create_index([("user_id", 1), ("is_read", 1)])

    admin = await db.users.find_one({"email": ADMIN_EMAIL})
    if not admin:
        admin_id = str(uuid.uuid4())
        await db.users.insert_one({
            "id": admin_id,
            "email": ADMIN_EMAIL,
            "nick": "Admin",
            "password_hash": hash_password(ADMIN_PASSWORD),
            "avatar_url": None,
            "bio": "Administrator MapMeet",
            "role": "admin",
            "is_blocked": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    else:
        admin_id = admin["id"]
        if not verify_password(ADMIN_PASSWORD, admin["password_hash"]):
            await db.users.update_one(
                {"email": ADMIN_EMAIL},
                {"$set": {"password_hash": hash_password(ADMIN_PASSWORD)}},
            )

    demo_email = "demo@mapmeet.pl"
    demo = await db.users.find_one({"email": demo_email})
    if not demo:
        demo_id = str(uuid.uuid4())
        await db.users.insert_one({
            "id": demo_id,
            "email": demo_email,
            "nick": "Kasia_Podróżniczka",
            "password_hash": hash_password("Demo123!"),
            "avatar_url": "https://images.unsplash.com/photo-1592234789031-94bf65f630ed?w=200",
            "bio": "Uwielbiam spotkania na świeżym powietrzu i dobrą kawę.",
            "role": "user",
            "is_blocked": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    else:
        demo_id = demo["id"]

    if await db.events.count_documents({}) == 0:
        base = datetime.now(timezone.utc)
        for i, (title, desc, cat, lat, lon, loc, cap) in enumerate(SAMPLE_EVENTS):
            starts = base + timedelta(days=(i % 20) + 2, hours=(i % 8) + 10)
            organizer = demo_id if i % 2 == 0 else admin_id
            await db.events.insert_one({
                "id": str(uuid.uuid4()),
                "title": title,
                "description": desc,
                "category": cat,
                "starts_at": starts.isoformat(),
                "max_participants": cap,
                "is_public": True,
                "requires_approval": i % 5 == 0,
                "comments_enabled": True,
                "lat": lat,
                "lon": lon,
                "location": {"type": "Point", "coordinates": [lon, lat]},
                "location_name": loc,
                "organizer_id": organizer,
                "participants": [organizer],
                "pending": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    creds = ROOT_DIR.parent / "memory" / "test_credentials.md"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        f"""# MapMeet – dane testowe

## Konto administratora
- Email: `{ADMIN_EMAIL}`
- Hasło: `{ADMIN_PASSWORD}`
- Rola: admin

## Konto testowe (użytkownik)
- Email: `demo@mapmeet.pl`
- Hasło: `Demo123!`
- Rola: user

## Endpointy
- POST /api/auth/register
- POST /api/auth/login
- POST /api/auth/logout
- GET  /api/auth/me
- PATCH /api/auth/profile
- GET  /api/events
- POST /api/events
- GET  /api/events/{{id}}
- POST /api/events/{{id}}/join
- POST /api/events/{{id}}/leave
- GET  /api/events/{{id}}/comments
- POST /api/events/{{id}}/comments
- GET  /api/me/events
""",
        encoding="utf-8",
    )


@app.on_event("startup")
async def on_start():
    await seed()
    logger.info("MapMeet uruchomiony")


@app.on_event("shutdown")
async def on_stop():
    client.close()
