"""Persist custom phrases that map to exposed scripts without executing them."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from home_assistant_intents import get_languages

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.util import language as language_util

from .const import CUSTOM_ACTIONS_DIRECTORY, LEGACY_CUSTOM_SENTENCE_FILENAME

_LOGGER = logging.getLogger(__name__)
HANDLERS_KEY = "smart_home_teacher_custom_intent_handlers"


def _actions_path(hass: HomeAssistant) -> Path:
    """Return persistent JSON storage for learned custom actions."""
    directory = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "custom_actions.json"


def _language_variant(hass: HomeAssistant, language: Any) -> str | None:
    """Resolve the Assist language tag to the exact language variant used by HA."""
    if not isinstance(language, str) or not language.strip():
        return None
    matches = language_util.matches(language, get_languages())
    return matches[0] if matches else None


def _normalise_phrase(value: Any) -> str | None:
    """Validate and normalize a complete phrase before storing it."""
    if not isinstance(value, str):
        return None
    phrase = " ".join(value.strip().split()).strip(" .,!?:;\"'")
    if not 4 <= len(phrase) <= 100 or len(phrase.split()) > 12:
        return None
    if any(char in phrase for char in "\r\n{}<>[]"):
        return None
    return phrase


def _read_actions_sync(hass: HomeAssistant) -> list[dict[str, str]]:
    """Read and validate persisted custom phrase mappings, including v1.1 migration."""
    path = _actions_path(hass)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.error("Could not read learned custom actions: %s", exc)
        return []
    records = payload.get("actions", []) if isinstance(payload, dict) else []
    valid: list[dict[str, str]] = []
    if not isinstance(records, list):
        return valid
    for item in records:
        if not isinstance(item, dict):
            continue
        phrase = _normalise_phrase(item.get("phrase"))
        entity_id = item.get("entity_id")
        language = _language_variant(hass, item.get("language", "ru"))
        if not phrase or not isinstance(entity_id, str) or not entity_id.startswith("script.") or not language:
            continue
        valid.append({
            "phrase": phrase,
            "entity_id": entity_id,
            "language": language,
        })
    return valid


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_actions_sync(hass: HomeAssistant, records: list[dict[str, str]]) -> None:
    """Persist phrase-to-script mappings without making them executable."""
    payload = {
        "version": 2,
        "actions": [
            {
                "phrase": item["phrase"],
                "entity_id": item["entity_id"],
                "language": item["language"],
            }
            for item in records
        ],
    }
    _atomic_write(
        _actions_path(hass),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


async def _reload_sentences(hass: HomeAssistant, languages: set[str]) -> None:
    """Invalidate the built-in agent's cached intents for cleaned-up languages."""
    if not languages:
        return
    default_agent = conversation.async_get_agent(hass, conversation.HOME_ASSISTANT_AGENT)
    if default_agent is None:
        _LOGGER.warning("Built-in conversation agent is not available; sentence cache was not refreshed")
        return
    for language in sorted(languages):
        await default_agent.async_reload(language)
        _LOGGER.info("Refreshed built-in Assist sentence cache for language=%s", language)


def _remove_legacy_sentence_files_sync(hass: HomeAssistant) -> set[str]:
    """Remove previously generated files without touching user-authored files."""
    affected_languages: set[str] = set()
    root = Path(hass.config.path("custom_sentences"))
    if root.is_dir():
        for language_dir in root.iterdir():
            if not language_dir.is_dir():
                continue
            generated_file = language_dir / LEGACY_CUSTOM_SENTENCE_FILENAME
            if generated_file.is_file():
                generated_file.unlink()
                affected_languages.add(language_dir.name)
    return affected_languages


async def async_setup_custom_actions(hass: HomeAssistant, entry) -> None:
    """Load mappings and remove executable sentences generated by older versions."""
    records = await hass.async_add_executor_job(_read_actions_sync, hass)
    affected_languages = await hass.async_add_executor_job(
        _remove_legacy_sentence_files_sync, hass
    )
    await _reload_sentences(hass, affected_languages)
    _LOGGER.info("Restored %s learned custom phrase(s)", len(records))


async def async_unload_custom_actions(hass: HomeAssistant, entry) -> None:
    """Remove any previously registered handlers and legacy generated sentences."""
    handlers = hass.data.pop(HANDLERS_KEY, {})
    for intent_type in list(handlers):
        intent.async_remove(hass, intent_type)

    affected_languages = await hass.async_add_executor_job(
        _remove_legacy_sentence_files_sync, hass
    )
    await _reload_sentences(hass, affected_languages)


async def async_add_custom_phrase(
    hass: HomeAssistant,
    entity_id: Any,
    phrase_value: Any,
    requested_language: str,
) -> str:
    """Store a phrase-to-script mapping without registering or running an intent."""
    phrase = _normalise_phrase(phrase_value)
    language = _language_variant(hass, requested_language)
    if not isinstance(entity_id, str) or not entity_id.startswith("script.") or not phrase or not language:
        return "Custom phrase rejected: expected a valid phrase, supported language and script entity"
    if hass.states.get(entity_id) is None or not async_should_expose(hass, "conversation", entity_id):
        return "Custom phrase rejected: script is missing or not exposed to Assist"

    records = await hass.async_add_executor_job(_read_actions_sync, hass)
    for item in records:
        if item["language"] == language and item["phrase"].casefold() == phrase.casefold():
            if item["entity_id"] == entity_id:
                return "Custom phrase already recorded"
            return "Custom phrase rejected: the same phrase is already mapped to another script"

    item = {
        "phrase": phrase,
        "entity_id": entity_id,
        "language": language,
    }
    records.append(item)
    await hass.async_add_executor_job(_write_actions_sync, hass, records)
    _LOGGER.warning(
        "SMART_HOME_TEACHER_CUSTOM_PHRASE_RECORDED script=%s language=%s phrase=%s",
        entity_id, language, phrase,
    )
    return f"Custom phrase recorded only: {phrase} -> {entity_id}; nothing was executed"

