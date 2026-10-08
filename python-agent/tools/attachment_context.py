"""Holds the files/photos the user sent in the CURRENT request.

A ContextVar keeps each request isolated, so two users chatting at the same
time never see each other's files.
"""
from contextvars import ContextVar
from typing import Optional

_current_attachments: ContextVar[Optional[list]] = ContextVar("current_attachments", default=None)


def set_current_attachments(items: Optional[list]) -> None:
    _current_attachments.set(list(items or []))


def get_current_attachments() -> list:
    return _current_attachments.get() or []