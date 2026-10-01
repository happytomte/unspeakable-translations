from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def avoid_remote_repository_access(monkeypatch):
    """Keep application tests deterministic when no project source is configured."""
    monkeypatch.setattr(
        "card_translator.web.app.refresh_repository",
        lambda *args, **kwargs: {
            "configured": False,
            "available": False,
            "projects": [],
        },
    )
