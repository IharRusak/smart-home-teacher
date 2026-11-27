"""Persist and register custom phrases that map to exposed scripts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from home_assistant_intents import get_languages

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.util import language as language_util

from .const import CUSTOM_ACTIONS_DIRECTORY, CUSTOM_SENTENCE_FILENAME

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


def _sentences_root(hass: HomeAssistant) -> Path:
    """Return the custom sentences root directory."""
    return Path(hass.config.path("custom_sentences"))


def _normalise_phrase(value: Any) -> str | None:
    """Validate and normalize a complete phrase for local Assist matching."""
    if not isinstance(value, str):
        return None
    phrase = " ".join(value.strip().split()).strip(" .,!?:;\"'")
    if not 4 <= len(phrase) <= 100 or len(phrase.split()) > 12:
        return None
    if any(char in phrase for char in "\r\n{}<>[]"):
        return None
    return phrase


def _intent_type(phrase: str, entity_id: str, language: str) -> str:
    """Create a deterministic intent name for a phrase, language and script pair."""
    digest = hashlib.sha256(
        f"{language}|{phrase.casefold()}|{entity_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"SmartHomeTeacherCustom{digest}"


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
            "intent_type": _intent_type(phrase, entity_id, language),
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


def _write_actions_sync(hass: HomeAssistant, records: list[dict[str, str]]) -> set[str]:
    """Persist mappings and generate one custom-sentence file per language."""
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

    root = _sentences_root(hass)
    affected_languages: set[str] = set()
    if root.is_dir():
        for language_dir in root.iterdir():
            if not language_dir.is_dir():
                continue
            generated_file = language_dir / CUSTOM_SENTENCE_FILENAME
            if generated_file.is_file():
                generated_file.unlink()
                affected_languages.add(language_dir.name)

    grouped: dict[str, list[dict[str, str]]] = {}
    for item in records:
        grouped.setdefault(item["language"], []).append(item)

    for language, language_records in grouped.items():
        sentences = {
            "language": language,
            "intents": {
                item["intent_type"]: {"data": [{"sentences": [item["phrase"]]}]}
                for item in language_records
            },
        }
        language_dir = root / language
        language_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            language_dir / CUSTOM_SENTENCE_FILENAME,
            yaml.safe_dump(sentences, allow_unicode=True, sort_keys=False),
        )
        affected_languages.add(language)

    return affected_languages


class SmartHomeTeacherCustomScriptHandler(intent.IntentHandler):
    """Run a learned script only when its custom sentence is spoken in a later turn."""

    def __init__(self, hass: HomeAssistant, intent_type: str, entity_id: str) -> None:
        self.hass = hass
        self.intent_type = intent_type
        self.entity_id = entity_id

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Recheck Assist Expose and execute the mapped script on a future exact intent."""
        response = intent_obj.create_response()
        entity_id = self.entity_id
        language_root = intent_obj.language.replace("_", "-").lower().split("-")[0]
        if language_root == "ru":
            denied = "Не могу выполнить команду: целевой сценарий не является скриптом."
            missing = "Не могу выполнить команду: сценарий больше не существует."
            not_exposed = "Не могу выполнить команду: сценарий больше не разрешён в Assist Expose."
            failed = "Не удалось выполнить сохранённую команду."
            success = "Выполняю команду."
        elif language_root == "pl":
            denied = "Nie można wykonać polecenia: wskazany cel nie jest skryptem."
            missing = "Nie można wykonać polecenia: skrypt już nie istnieje."
            not_exposed = "Nie można wykonać polecenia: skrypt nie jest już udostępniony w Assist."
            failed = "Nie udało się wykonać zapisanego polecenia."
            success = "Wykonuję polecenie."
        else:
            denied = "Cannot run this command: the target is not a script."
            missing = "Cannot run this command: the script no longer exists."
            not_exposed = "Cannot run this command: the script is no longer exposed to Assist."
            failed = "The saved command could not be executed."
            success = "Running the command."
        if not entity_id.startswith("script."):
            response.async_set_speech(denied)
            return response
        if self.hass.states.get(entity_id) is None:
            response.async_set_speech(missing)
            return response
        if not async_should_expose(self.hass, "conversation", entity_id):
            response.async_set_speech(not_exposed)
            return response
        try:
            await self.hass.services.async_call(
                "script",
                "turn_on",
                {"entity_id": entity_id},
                blocking=True,
                context=intent_obj.context,
            )
        except Exception:
            _LOGGER.exception("Failed to run the script for learned custom intent %s", self.intent_type)
            response.async_set_speech(failed)
            return response
        response.async_set_speech(success)
        return response


def _register_record(hass: HomeAssistant, item: dict[str, str]) -> None:
    """Register the runtime intent handler for one learned phrase."""
    handlers = hass.data.setdefault(HANDLERS_KEY, {})
    intent_type = item["intent_type"]
    if intent_type in handlers:
        intent.async_remove(hass, intent_type)
    handler = SmartHomeTeacherCustomScriptHandler(hass, intent_type, item["entity_id"])
    intent.async_register(hass, handler)
    handlers[intent_type] = handler


async def _reload_sentences(hass: HomeAssistant, languages: set[str]) -> None:
    """Invalidate only the built-in agent's cached intents for changed languages."""
    if not languages:
        return
    default_agent = conversation.async_get_agent(hass, conversation.HOME_ASSISTANT_AGENT)
    if default_agent is None:
        _LOGGER.warning("Built-in conversation agent is not available; sentence cache was not refreshed")
        return
    for language in sorted(languages):
        await default_agent.async_reload(language)
        _LOGGER.info("Refreshed built-in Assist sentence cache for language=%s", language)


def _remove_generated_sentence_files_sync(hass: HomeAssistant) -> set[str]:
    """Remove generated sentence files without touching user-authored files."""
    affected_languages: set[str] = set()
    root = _sentences_root(hass)
    if root.is_dir():
        for language_dir in root.iterdir():
            if not language_dir.is_dir():
                continue
            generated_file = language_dir / CUSTOM_SENTENCE_FILENAME
            if generated_file.is_file():
                generated_file.unlink()
                affected_languages.add(language_dir.name)
    return affected_languages


async def async_setup_custom_actions(hass: HomeAssistant, entry) -> None:
    """Restore persisted custom actions using the executor for all filesystem work."""
    records = await hass.async_add_executor_job(_read_actions_sync, hass)
    affected_languages = await hass.async_add_executor_job(_write_actions_sync, hass, records)
    for item in records:
        _register_record(hass, item)
    await _reload_sentences(hass, affected_languages)
    _LOGGER.info("Restored %s learned custom phrase(s)", len(records))


async def async_unload_custom_actions(hass: HomeAssistant, entry) -> None:
    """Unregister custom handlers and remove generated files in the executor."""
    handlers = hass.data.pop(HANDLERS_KEY, {})
    for intent_type in list(handlers):
        intent.async_remove(hass, intent_type)

    affected_languages = await hass.async_add_executor_job(
        _remove_generated_sentence_files_sync, hass
    )
    await _reload_sentences(hass, affected_languages)


async def async_add_custom_phrase(
    hass: HomeAssistant,
    entity_id: Any,
    phrase_value: Any,
    requested_language: str,
) -> str:
    """Learn a phrase-to-script mapping without running the script."""
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
                _register_record(hass, item)
                return "Custom phrase already learned"
            return "Custom phrase rejected: the same phrase is already mapped to another script"

    item = {
        "intent_type": _intent_type(phrase, entity_id, language),
        "phrase": phrase,
        "entity_id": entity_id,
        "language": language,
    }
    records.append(item)
    affected_languages = await hass.async_add_executor_job(_write_actions_sync, hass, records)
    _register_record(hass, item)
    await _reload_sentences(hass, affected_languages)
    _LOGGER.warning(
        "SMART_HOME_TEACHER_CUSTOM_PHRASE_LEARNED intent_type=%s script=%s language=%s phrase=%s",
        item["intent_type"], entity_id, language, phrase,
    )
    return f"Custom phrase learned: {phrase} -> {entity_id}; no device action was executed"

