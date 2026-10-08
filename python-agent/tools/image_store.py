"""In-memory holder for generated images. Nothing here touches the disk.

- put_image(): keeps the bytes in RAM for a while (so they can also be attached to an email)
- queue_for_delivery(): marks an image to be sent to the user with the current reply
- begin_request(): called once per request by main.py; returns the list of images to deliver
"""
import threading
import time
import uuid
from contextvars import ContextVar
from typing import List, Optional, Tuple

TTL_SEC = 60 * 60   # forget images after 1 hour
MAX_ITEMS = 20      # and never keep more than 20 at once

_lock = threading.Lock()
_images: dict = {}  # ref -> (bytes, mime, created_at)
_outbox: ContextVar = ContextVar("image_outbox", default=None)


def _evict(now: float) -> None:
    for ref in [r for r, (_, _, ts) in _images.items() if now - ts > TTL_SEC]:
        del _images[ref]
    while len(_images) > MAX_ITEMS:
        oldest = min(_images, key=lambda r: _images[r][2])
        del _images[oldest]


def put_image(data: bytes, mime: str = "image/png") -> str:
    ref = uuid.uuid4().hex[:10]
    now = time.time()
    with _lock:
        _images[ref] = (data, mime, now)
        _evict(now)
    return ref


def get_image(ref: str) -> Optional[Tuple[bytes, str]]:
    ref = (ref or "").strip()
    if ref.lower().startswith("gen:"):
        ref = ref[4:].strip()
    with _lock:
        item = _images.get(ref)
        if not item or time.time() - item[2] > TTL_SEC:
            return None
        return item[0], item[1]


def begin_request() -> List[str]:
    """Start a fresh delivery list for this request. Tools running inside it append to the same list."""
    box: List[str] = []
    _outbox.set(box)
    return box


def queue_for_delivery(ref: str) -> None:
    box = _outbox.get()
    if box is not None:
        box.append(ref)