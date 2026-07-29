"""Forum: users post a snipe they found (an image, required, plus an
optional title). Logged-out visitors can see every post; posting requires
login and a nickname (set once via `PATCH /api/me/nickname` in dashboard.py
-- posts used to display the account's real email publicly, human feedback
after shipping said that's the wrong default; enforced here at post time
rather than at registration, since that also naturally covers every account
that registered before nicknames existed, not just new signups). Deliberately
minimal -- no editing, deleting, comments, or moderation queue, none of which
were asked for.

Images are stored as plain files under DATA/forum_images/ on the same
persistent Railway volume that already holds snapshots/listings (see
CLAUDE.md's "Architecture & data layout") rather than a new object-storage
dependency -- consistent with this project's "minimal deps" convention.
Filenames are server-generated (uuid4 + an extension derived from the
validated content-type), never taken from the client, so there's no path-
traversal or overwrite risk from the original upload filename.
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import current_active_user
from db import ForumPost, User, get_async_session

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
IMAGE_DIR = DATA / "forum_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# Content-type allowlist, not a client-supplied filename extension -- the
# client's claimed extension is never trusted for anything, including display.
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB -- generous for a screenshot, bounded so one post can't balloon the volume

router = APIRouter(prefix="/api/forum", tags=["forum"])

# Separate router (no /api prefix, matches the image_url paths returned by
# _post_to_json below) so an <img> tag can hit it directly. A plain
# app.mount(StaticFiles(...)) would freeze IMAGE_DIR at mount time instead of
# re-reading the module-level global per request -- this reads IMAGE_DIR
# fresh on every call instead, which also makes it trivial to redirect in
# tests via monkeypatch.setattr(forum, "IMAGE_DIR", ...).
image_router = APIRouter(prefix="/forum/images", tags=["forum"])


@image_router.get("/{filename}")
async def serve_image(filename: str) -> FileResponse:
    # Path(...).name strips any directory components -- filenames are always
    # server-generated (uuid4 + a validated extension, see create_post
    # below), so this is defense in depth, not a path this app can reach
    # organically.
    safe_name = Path(filename).name
    path = IMAGE_DIR / safe_name
    if not path.is_file():
        raise HTTPException(404, "image not found")
    return FileResponse(path)


def _post_to_json(post: ForumPost) -> dict:
    return {
        "id": post.id,
        "title": post.title,
        "image_url": f"/forum/images/{post.image_filename}",
        # author_email is intentionally not exposed here -- see the module
        # docstring. Falls back for posts made before nicknames existed
        # (author_nickname is nullable, see db.ForumPost).
        "author_nickname": post.author_nickname or "Anonymous Sniper",
        "created_at": post.created_at.isoformat() if post.created_at else None,
    }


@router.get("/posts")
async def list_posts(limit: int = Query(50, ge=1, le=200),
                     session: AsyncSession = Depends(get_async_session)) -> dict:
    """Public -- no auth dependency at all, unlike every /api/* route in
    dashboard.py. Non-logged-in visitors can see posts, only posting is gated."""
    result = await session.execute(
        select(ForumPost).order_by(ForumPost.created_at.desc()).limit(limit)
    )
    posts = result.scalars().all()
    return {"posts": [_post_to_json(p) for p in posts]}


@router.post("/posts")
async def create_post(title: str | None = Form(None), image: UploadFile = File(...),
                      user: User = Depends(current_active_user),
                      session: AsyncSession = Depends(get_async_session)) -> dict:
    if not user.nickname:
        # Defense in depth -- snipeboard.html's dialog already prompts for a
        # nickname before ever reaching this request, but that's a client-
        # side convenience, not the enforcement boundary.
        raise HTTPException(400, "set a nickname before posting")
    ext = ALLOWED_IMAGE_TYPES.get(image.content_type)
    if ext is None:
        raise HTTPException(400, f"unsupported image type {image.content_type!r} "
                                 f"-- use PNG, JPEG, WEBP, or GIF")
    data = await image.read()
    if not data:
        raise HTTPException(400, "image is required")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, f"image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")

    filename = f"{uuid.uuid4().hex}{ext}"
    (IMAGE_DIR / filename).write_bytes(data)

    title = title.strip() if title and title.strip() else None
    post = ForumPost(author_id=user.id, author_email=user.email, author_nickname=user.nickname,
                     title=title, image_filename=filename)
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return _post_to_json(post)
