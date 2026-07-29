"""Tests for forum.py: the "post a snipe you found" feature. Mirrors
test_dashboard.py's dependency-override bypass style for the business-logic
tests (image validation, storage, listing) plus one real-auth check that
posting is actually gated (see test_auth.py for genuine login-flow coverage
elsewhere)."""
import asyncio
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth
import dashboard
import forum
from db import Base, User, get_async_session

client = TestClient(dashboard.app)

# Explicit id -- FAKE_USER is a plain Python object handed back directly by
# the dependency override, never actually inserted via the DB, so the
# column's server-side id default never fires. forum.create_post() needs a
# real, non-None user.id to satisfy forum_post.author_id's FK/NOT NULL.
FAKE_USER = User(id=uuid.uuid4(), email="poster@example.com", hashed_password="x",
                 is_active=True, is_superuser=False, is_verified=True)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32  # not a real decodable PNG -- content-type is what's validated, not pixels


@pytest.fixture(autouse=True)
def bypass_auth():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)


@pytest.fixture(autouse=True)
def bypass_get_async_session(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_forum.db'}"
    engine = create_async_engine(db_url)

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create_tables())

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    yield
    dashboard.app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def isolate_forum_image_dir(tmp_path, monkeypatch):
    """forum.py writes uploaded images to disk unconditionally -- redirect
    into tmp_path so no test ever touches the real, gitignored data/forum_images."""
    image_dir = tmp_path / "forum_images_test"
    image_dir.mkdir()
    monkeypatch.setattr(forum, "IMAGE_DIR", image_dir)
    return image_dir


def _drop_auth_override():
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)


def test_list_posts_empty_by_default():
    r = client.get("/api/forum/posts")
    assert r.status_code == 200
    assert r.json()["posts"] == []


def test_create_post_requires_login():
    _drop_auth_override()
    r = client.post("/api/forum/posts", files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    assert r.status_code == 401


def test_create_post_stores_image_and_returns_it_in_the_feed(isolate_forum_image_dir):
    r = client.post("/api/forum/posts", data={"title": "4g bag on Draenor"},
                    files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "4g bag on Draenor"
    assert body["author_email"] == FAKE_USER.email
    assert body["image_url"].startswith("/forum/images/")
    assert body["created_at"] is not None

    # the file actually landed on disk under the isolated image dir
    stored = list(isolate_forum_image_dir.glob("*.png"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == PNG_BYTES

    # and shows up in the public feed, newest first, without login
    _drop_auth_override()
    feed = client.get("/api/forum/posts").json()["posts"]
    assert len(feed) == 1
    assert feed[0]["title"] == "4g bag on Draenor"


def test_create_post_title_is_optional():
    r = client.post("/api/forum/posts", files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    assert r.status_code == 200
    assert r.json()["title"] is None


def test_create_post_blank_title_is_normalized_to_none():
    r = client.post("/api/forum/posts", data={"title": "   "},
                    files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    assert r.status_code == 200
    assert r.json()["title"] is None


def test_create_post_rejects_unsupported_content_type():
    r = client.post("/api/forum/posts",
                    files={"image": ("snipe.exe", io.BytesIO(b"MZ..."), "application/octet-stream")})
    assert r.status_code == 400


def test_create_post_rejects_oversized_image(monkeypatch):
    monkeypatch.setattr(forum, "MAX_IMAGE_BYTES", 10)
    r = client.post("/api/forum/posts",
                    files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    assert r.status_code == 400


def test_create_post_rejects_empty_image():
    r = client.post("/api/forum/posts",
                    files={"image": ("snipe.png", io.BytesIO(b""), "image/png")})
    assert r.status_code == 400


def test_list_posts_newest_first(isolate_forum_image_dir):
    for title in ("first", "second", "third"):
        r = client.post("/api/forum/posts", data={"title": title},
                        files={"image": (f"{title}.png", io.BytesIO(PNG_BYTES), "image/png")})
        assert r.status_code == 200
    feed = client.get("/api/forum/posts").json()["posts"]
    assert [p["title"] for p in feed] == ["third", "second", "first"]


def test_list_posts_respects_limit(isolate_forum_image_dir):
    for i in range(3):
        client.post("/api/forum/posts", files={"image": (f"{i}.png", io.BytesIO(PNG_BYTES), "image/png")})
    feed = client.get("/api/forum/posts", params={"limit": 2}).json()["posts"]
    assert len(feed) == 2


def test_forum_page_served_without_auth():
    _drop_auth_override()
    r = client.get("/forum", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_uploaded_image_is_served_back():
    r = client.post("/api/forum/posts", files={"image": ("snipe.png", io.BytesIO(PNG_BYTES), "image/png")})
    image_url = r.json()["image_url"]
    served = client.get(image_url)
    assert served.status_code == 200
    assert served.content == PNG_BYTES
