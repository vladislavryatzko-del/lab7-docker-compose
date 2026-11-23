"""Мессенджер учебных групп: REST API (FastAPI) + PostgreSQL + Redis.

PostgreSQL — источник истины (группы, участники, сообщения).
Redis — (1) кэш «последних N сообщений» группы, (2) онлайн-статус пользователя с TTL.
Если Redis недоступен, API продолжает работать (кэш просто пропускается).
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
LAST_CACHE_SIZE = int(os.getenv("LAST_CACHE_SIZE", "50"))      # сколько сообщений группы держим в Redis
ONLINE_TTL_SECONDS = int(os.getenv("ONLINE_TTL_SECONDS", "60"))  # сколько живёт отметка «online»

log = logging.getLogger("messenger")
app = FastAPI(title="Study Groups Messenger", version="1.0.0")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


# ---------------- вспомогательное ----------------
@contextmanager
def db() -> Iterator[psycopg.Connection]:
    """Соединение на запрос (достаточно для учебного MVP). commit/rollback выполняет контекст."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def cache_key(group_id: int) -> str:
    return f"group:{group_id}:last"


def online_key(username: str) -> str:
    return f"online:{username}"


def serialize(row: dict) -> dict:
    return {**row, "created_at": row["created_at"].isoformat()}


def require_group(conn: psycopg.Connection, group_id: int) -> None:
    if conn.execute("SELECT 1 FROM groups WHERE id = %s", (group_id,)).fetchone() is None:
        raise HTTPException(404, "group not found")


# ---------------- схемы ----------------
class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=50, pattern=r"^[\w.-]+$")


class MessageIn(BaseModel):
    author: str = Field(min_length=1, max_length=50)
    text: str = Field(min_length=1, max_length=2000)


# ---------------- служебные ----------------
@app.get("/health")
def health() -> dict:
    """Готовность: отвечают и PostgreSQL, и Redis (иначе 503)."""
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        r.ping()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"dependency unavailable: {type(exc).__name__}")
    return {"status": "ok"}


# ---------------- группы и участники ----------------
@app.post("/groups", status_code=201)
def create_group(body: GroupIn) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name must not be blank")
    with db() as conn:
        try:
            row = conn.execute(
                "INSERT INTO groups (name) VALUES (%s) RETURNING id, name, created_at", (name,)
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "group with this name already exists")
    return serialize(row)


@app.get("/groups")
def list_groups() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM groups ORDER BY id").fetchall()
    return [serialize(x) for x in rows]


@app.post("/groups/{group_id}/members", status_code=201)
def join_group(group_id: int, body: MemberIn) -> dict:
    """Вступить в группу (пользователь создаётся при первом упоминании; повторное вступление безопасно)."""
    with db() as conn:
        require_group(conn, group_id)
        conn.execute("INSERT INTO users (username) VALUES (%s) ON CONFLICT DO NOTHING", (body.username,))
        conn.execute(
            "INSERT INTO memberships (group_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (group_id, body.username),
        )
    return {"group_id": group_id, "username": body.username}


@app.get("/users/{username}/groups")
def user_groups(username: str) -> list[dict]:
    """Список групп пользователя."""
    with db() as conn:
        rows = conn.execute(
            """SELECT g.id, g.name, g.created_at
                 FROM groups g JOIN memberships m ON m.group_id = g.id
                WHERE m.username = %s ORDER BY g.id""",
            (username,),
        ).fetchall()
    return [serialize(x) for x in rows]


# ---------------- сообщения ----------------
@app.post("/groups/{group_id}/messages", status_code=201)
def post_message(group_id: int, body: MessageIn) -> dict:
    """Отправить сообщение в группу. Писать могут только участники (иначе 403)."""
    with db() as conn:
        require_group(conn, group_id)
        member = conn.execute(
            "SELECT 1 FROM memberships WHERE group_id = %s AND username = %s", (group_id, body.author)
        ).fetchone()
        if member is None:
            raise HTTPException(403, "author is not a member of the group")
        row = conn.execute(
            "INSERT INTO messages (group_id, author, text) VALUES (%s, %s, %s) "
            "RETURNING id, group_id, author, text, created_at",
            (group_id, body.author, body.text),
        ).fetchone()
    msg = serialize(row)
    try:  # кэш: свежие сообщения в начало списка, хвост обрезаем
        pipe = r.pipeline()
        pipe.lpush(cache_key(group_id), json.dumps(msg, ensure_ascii=False))
        pipe.ltrim(cache_key(group_id), 0, LAST_CACHE_SIZE - 1)
        pipe.execute()
    except redis.RedisError as exc:
        log.warning("redis unavailable, cache skipped: %s", exc)
    return msg


@app.get("/groups/{group_id}/messages")
def last_messages(group_id: int, limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    """Последние ``limit`` сообщений группы в хронологическом порядке (старые → новые).

    Сначала смотрим в Redis; при промахе читаем PostgreSQL и «прогреваем» кэш.
    """
    with db() as conn:
        require_group(conn, group_id)

    if limit <= LAST_CACHE_SIZE:
        try:
            cached = r.lrange(cache_key(group_id), 0, limit - 1)
            if len(cached) == limit:
                return [json.loads(x) for x in reversed(cached)]
        except redis.RedisError as exc:
            log.warning("redis unavailable, reading from db: %s", exc)

    fetch = max(limit, LAST_CACHE_SIZE)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, group_id, author, text, created_at FROM messages "
            "WHERE group_id = %s ORDER BY id DESC LIMIT %s",
            (group_id, fetch),
        ).fetchall()
    newest_first = [serialize(x) for x in rows]
    try:  # прогрев кэша
        pipe = r.pipeline()
        pipe.delete(cache_key(group_id))
        if newest_first:
            pipe.rpush(cache_key(group_id), *[json.dumps(m, ensure_ascii=False) for m in newest_first[:LAST_CACHE_SIZE]])
        pipe.execute()
    except redis.RedisError as exc:
        log.warning("redis unavailable, cache not warmed: %s", exc)
    return list(reversed(newest_first[:limit]))


# ---------------- онлайн-статус (Redis + TTL) ----------------
@app.post("/users/{username}/heartbeat")
def heartbeat(username: str) -> dict:
    """Отметить пользователя как online на ONLINE_TTL_SECONDS секунд."""
    try:
        r.set(online_key(username), "1", ex=ONLINE_TTL_SECONDS)
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable")
    return {"username": username, "ttl_seconds": ONLINE_TTL_SECONDS}


@app.get("/groups/{group_id}/online")
def group_online(group_id: int) -> dict:
    """Кто из участников группы сейчас online."""
    with db() as conn:
        require_group(conn, group_id)
        members = [x["username"] for x in conn.execute(
            "SELECT username FROM memberships WHERE group_id = %s ORDER BY username", (group_id,)).fetchall()]
    try:
        flags = r.mget([online_key(u) for u in members]) if members else []
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable")
    return {"group_id": group_id, "online": [u for u, f in zip(members, flags) if f]}
