"""The OpenAI service and MirrorGPT orchestrator are process-wide singletons.

Constructing OpenAIService rebuilds two httpx clients; the orchestrator also
loads ArchetypeEngine definitions. Caching them (like get_dynamodb_service)
avoids that per request. These tests pin the caching so a future change can't
silently reintroduce per-request construction.
"""

from unittest.mock import patch

from src.app.services.openai_service import get_openai_service


def test_get_openai_service_is_cached():
    get_openai_service.cache_clear()
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
        a = get_openai_service()
        b = get_openai_service()
    assert a is b


def test_get_mirror_orchestrator_is_cached():
    from src.app.api.mirrorgpt_routes import get_mirror_orchestrator

    get_mirror_orchestrator.cache_clear()
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
        a = get_mirror_orchestrator()
        b = get_mirror_orchestrator()
    assert a is b
    # Reuses the cached OpenAI singleton rather than building a new client.
    assert a.openai_service is get_openai_service()
