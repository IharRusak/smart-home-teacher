"""End-to-end tests for inert custom-phrase learning."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.smart_home_teacher import custom_actions
from custom_components.smart_home_teacher.const import (
    CUSTOM_ACTIONS_DIRECTORY,
    LEGACY_CUSTOM_SENTENCE_FILENAME,
)


@pytest.mark.e2e
async def test_phrase_learning_records_mapping_without_running_script(hass, monkeypatch):
    """A validated phrase mapping is persisted, but never registered or executed."""
    script_entity = "script.boil_kettle"
    hass.states.async_set(
        script_entity,
        "off",
        {"friendly_name": "Boil kettle"},
    )
    monkeypatch.setattr(
        custom_actions,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: True,
    )
    service_calls = []
    service_registry_type = type(hass.services)

    async def intercept_service_calls(registry, domain, service, *args, **kwargs):
        service_calls.append((domain, service))
        return None

    monkeypatch.setattr(service_registry_type, "async_call", intercept_service_calls)
    register_intent = Mock()
    monkeypatch.setattr(custom_actions.intent, "async_register", register_intent)

    result = await custom_actions.async_add_custom_phrase(
        hass,
        script_entity,
        "I would like some hot water",
        "en",
    )

    assert result.startswith("Custom phrase recorded only:")
    actions_path = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json"))
    assert json.loads(actions_path.read_text(encoding="utf-8"))["actions"] == [
        {
            "phrase": "I would like some hot water",
            "entity_id": script_entity,
            "language": "en",
        }
    ]
    assert hass.states.get(script_entity).state == "off"
    assert service_calls == []
    register_intent.assert_not_called()
    assert custom_actions.HANDLERS_KEY not in hass.data
    assert not Path(
        hass.config.path(
            "custom_sentences",
            "en",
            LEGACY_CUSTOM_SENTENCE_FILENAME,
        )
    ).exists()


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("entity_id", "phrase", "create_state", "exposed", "expected_message"),
    [
        (
            "light.kettle",
            "I would like some hot water",
            False,
            True,
            "expected a valid phrase",
        ),
        (
            "script.boil_kettle",
            "Hi",
            True,
            True,
            "expected a valid phrase",
        ),
        (
            "script.boil_kettle",
            "I would like some hot water",
            False,
            True,
            "script is missing or not exposed",
        ),
        (
            "script.boil_kettle",
            "I would like some hot water",
            True,
            False,
            "script is missing or not exposed",
        ),
    ],
)
async def test_phrase_validation_rejects_invalid_mappings(
    hass,
    monkeypatch,
    entity_id,
    phrase,
    create_state,
    exposed,
    expected_message,
):
    """Invalid domains, phrases, missing scripts, and unexposed scripts are rejected."""
    if create_state:
        hass.states.async_set(entity_id, "off")
    monkeypatch.setattr(
        custom_actions,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: exposed,
    )

    result = await custom_actions.async_add_custom_phrase(
        hass,
        entity_id,
        phrase,
        "en",
    )

    assert expected_message in result
    actions_path = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json"))
    assert not actions_path.exists()


@pytest.mark.e2e
async def test_duplicate_phrase_cannot_be_remapped(hass, monkeypatch):
    """Case-insensitive duplicates stay mapped to the original script."""
    first_script = "script.boil_kettle"
    second_script = "script.make_tea"
    hass.states.async_set(first_script, "off")
    hass.states.async_set(second_script, "off")
    monkeypatch.setattr(
        custom_actions,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: True,
    )

    phrase = "I would like some hot water"
    assert "recorded only" in await custom_actions.async_add_custom_phrase(
        hass, first_script, phrase, "en"
    )
    assert "already recorded" in await custom_actions.async_add_custom_phrase(
        hass, first_script, phrase.upper(), "en"
    )
    assert "already mapped to another script" in await custom_actions.async_add_custom_phrase(
        hass, second_script, phrase, "en"
    )

    actions_path = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json"))
    assert json.loads(actions_path.read_text(encoding="utf-8"))["actions"] == [
        {
            "phrase": phrase,
            "entity_id": first_script,
            "language": "en",
        }
    ]


@pytest.mark.e2e
async def test_setup_removes_legacy_executable_sentences_only(hass, monkeypatch):
    """Upgrade cleanup removes integration-generated sentences, not user files."""
    language_dir = Path(hass.config.path("custom_sentences", "en"))
    language_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = language_dir / LEGACY_CUSTOM_SENTENCE_FILENAME
    legacy_file.write_text("legacy intent", encoding="utf-8")
    user_file = language_dir / "user_sentences.yaml"
    user_file.write_text("user intent", encoding="utf-8")
    reload_sentences = AsyncMock()
    monkeypatch.setattr(custom_actions, "_reload_sentences", reload_sentences)

    await custom_actions.async_setup_custom_actions(hass, None)

    assert not legacy_file.exists()
    assert user_file.read_text(encoding="utf-8") == "user intent"
    reload_sentences.assert_awaited_once_with(hass, {"en"})
