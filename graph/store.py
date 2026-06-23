"""Cross-ticket Store helpers for repo profiles."""

from __future__ import annotations

from typing import Any


REPO_PROFILE_NS = "repo_profiles"


def get_repo_profile(store, repo: str) -> dict[str, Any]:
    """Return the stored repo profile dict, or {} if not found."""
    if store is None:
        return {}
    try:
        item = store.get((REPO_PROFILE_NS, repo))
        if item is None:
            return {}
        # LangGraph InMemoryStore returns an Item with .value
        return getattr(item, "value", item) or {}
    except Exception:
        return {}


def put_repo_profile(store, repo: str, profile: dict[str, Any]) -> None:
    """Upsert a repo profile into the Store."""
    if store is None:
        return
    try:
        store.put((REPO_PROFILE_NS, repo), profile)
    except Exception:
        pass
