"""Интеграционные тесты API против реальных PostgreSQL и Redis.

Запуск (сервисы уже подняты, например через `docker compose up -d db redis`):
    DATABASE_URL=postgresql://messenger:...@localhost:5432/messenger REDIS_URL=redis://localhost:6379/0 \
    pytest tests -v
Без переменных окружения тесты пропускаются.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

if not (os.getenv("DATABASE_URL") and os.getenv("REDIS_URL")):
    pytest.skip("DATABASE_URL / REDIS_URL не заданы", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
os.environ.setdefault("LAST_CACHE_SIZE", "5")
os.environ.setdefault("ONLINE_TTL_SECONDS", "2")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)


def uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def group():
    """Новая группа с участниками alice и bob."""
    g = client.post("/groups", json={"name": uniq("course")}).json()
    for u in ("alice", "bob"):
        assert client.post(f"/groups/{g['id']}/members", json={"username": u}).status_code == 201
    return g


def test_health_ok():
    assert client.get("/health").json() == {"status": "ok"}


def test_create_group_and_duplicate_name_conflict():
    name = uniq("dup")
    assert client.post("/groups", json={"name": name}).status_code == 201
    assert client.post("/groups", json={"name": name}).status_code == 409


def test_blank_group_name_rejected():
    assert client.post("/groups", json={"name": "   "}).status_code == 422


def test_join_is_idempotent_and_listed_in_user_groups(group):
    assert client.post(f"/groups/{group['id']}/members", json={"username": "alice"}).status_code == 201
    groups = client.get("/users/alice/groups").json()
    assert group["id"] in [g["id"] for g in groups]
    assert [g["id"] for g in groups].count(group["id"]) == 1


def test_join_unknown_group_404():
    assert client.post("/groups/99999999/members", json={"username": "alice"}).status_code == 404


def test_user_without_groups_gets_empty_list():
    assert client.get(f"/users/{uniq('nobody')}/groups").json() == []


def test_post_and_read_last_messages_in_chronological_order(group):
    for i in range(3):
        r = client.post(f"/groups/{group['id']}/messages", json={"author": "alice", "text": f"msg {i}"})
        assert r.status_code == 201
    msgs = client.get(f"/groups/{group['id']}/messages?limit=2").json()
    assert [m["text"] for m in msgs] == ["msg 1", "msg 2"]  # последние 2, старые -> новые


def test_non_member_cannot_post(group):
    r = client.post(f"/groups/{group['id']}/messages", json={"author": "mallory", "text": "hi"})
    assert r.status_code == 403


def test_post_to_unknown_group_404():
    assert client.post("/groups/99999999/messages", json={"author": "alice", "text": "x"}).status_code == 404


def test_empty_and_too_long_text_rejected(group):
    url = f"/groups/{group['id']}/messages"
    assert client.post(url, json={"author": "alice", "text": ""}).status_code == 422
    assert client.post(url, json={"author": "alice", "text": "x" * 2001}).status_code == 422


def test_limit_validation(group):
    assert client.get(f"/groups/{group['id']}/messages?limit=0").status_code == 422
    assert client.get(f"/groups/{group['id']}/messages?limit=201").status_code == 422


def test_empty_group_returns_empty_list(group):
    assert client.get(f"/groups/{group['id']}/messages").json() == []


def test_cache_is_trimmed_but_history_is_complete_in_db(group):
    gid = group["id"]
    for i in range(8):  # LAST_CACHE_SIZE=5
        client.post(f"/groups/{gid}/messages", json={"author": "bob", "text": f"m{i}"})
    assert main.r.llen(main.cache_key(gid)) == 5
    # лимит больше размера кэша -> читается из БД, история полная
    assert [m["text"] for m in client.get(f"/groups/{gid}/messages?limit=8").json()] == [f"m{i}" for i in range(8)]


def test_cache_miss_is_warmed_from_db(group):
    gid = group["id"]
    for i in range(3):
        client.post(f"/groups/{gid}/messages", json={"author": "alice", "text": f"m{i}"})
    main.r.delete(main.cache_key(gid))  # имитируем потерю кэша
    msgs = client.get(f"/groups/{gid}/messages?limit=3").json()
    assert [m["text"] for m in msgs] == ["m0", "m1", "m2"]
    assert main.r.llen(main.cache_key(gid)) == 3  # кэш прогрет


def test_messages_survive_cache_flush(group):
    gid = group["id"]
    client.post(f"/groups/{gid}/messages", json={"author": "alice", "text": "keep me"})
    main.r.flushdb()
    assert client.get(f"/groups/{gid}/messages").json()[0]["text"] == "keep me"


def test_online_status_with_ttl(group):
    gid = group["id"]
    assert client.get(f"/groups/{gid}/online").json()["online"] == []
    client.post("/users/alice/heartbeat")
    assert client.get(f"/groups/{gid}/online").json()["online"] == ["alice"]
    time.sleep(2.2)  # ONLINE_TTL_SECONDS=2
    assert client.get(f"/groups/{gid}/online").json()["online"] == []


def test_unicode_text_roundtrip(group):
    gid = group["id"]
    client.post(f"/groups/{gid}/messages", json={"author": "alice", "text": "Привет, группа! 👋"})
    assert client.get(f"/groups/{gid}/messages").json()[-1]["text"] == "Привет, группа! 👋"
