"""Persist learned phrases and expose them through Home Assistant native intents."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from home_assistant_intents import get_languages

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import language as language_util

from .const import CUSTOM_ACTIONS_DIRECTORY, CUSTOM_SENTENCE_FILENAME

_LOGGER = logging.getLogger(__name__)
CONFIG_BEGIN_MARKER = "# BEGIN SMART_HOME_TEACHER INTENT_SCRIPT"
CONFIG_END_MARKER = "# END SMART_HOME_TEACHER INTENT_SCRIPT"


class NativeIntentConfigError(ValueError):
    """Raised when the managed native intent configuration cannot be written."""


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
    """Validate a complete phrase without changing its punctuation."""
    if not isinstance(value, str):
        return None
    phrase = value.strip()
    if not 4 <= len(phrase) <= 100 or len(phrase.split()) > 12:
        return None
    if any(ord(char) < 32 for char in phrase) or any(
        char in phrase for char in "{}<>[]()|\\"
    ):
        return None
    return phrase


def _read_actions_sync(hass: HomeAssistant) -> list[dict[str, str]]:
    """Read and validate persisted custom phrase mappings."""
    try:
        path = _actions_path(hass)
    except OSError as exc:
        raise NativeIntentConfigError(
            "Learned custom action storage is unavailable"
        ) from exc
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.error("Could not read learned custom actions: %s", exc)
        raise NativeIntentConfigError(
            "Learned custom action storage is unreadable"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("actions"), list):
        raise NativeIntentConfigError(
            "Learned custom action storage has an invalid format"
        )
    records = payload["actions"]
    valid: list[dict[str, str]] = []
    for item in records:
        if not isinstance(item, dict):
            raise NativeIntentConfigError(
                "Learned custom action storage contains an invalid entry"
            )
        phrase = _normalise_phrase(item.get("phrase"))
        entity_id = item.get("entity_id")
        language = _language_variant(hass, item.get("language", "ru"))
        if (
            not phrase
            or not isinstance(entity_id, str)
            or not entity_id.startswith("script.")
            or not language
        ):
            raise NativeIntentConfigError(
                "Learned custom action storage contains an invalid mapping"
            )
        valid.append(
            {
                "phrase": phrase,
                "entity_id": entity_id,
                "language": language,
            }
        )
    return valid


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _intent_type(item: dict[str, str]) -> str:
    """Return a stable YAML-safe intent type for one phrase and language."""
    identity = f"{item['language']}\0{item['phrase'].casefold()}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"SmartHomeTeacher_{suffix}"


def _success_speech(language: str) -> str:
    """Return a short response for the generated native intent."""
    language_root = language.replace("_", "-").split("-", 1)[0]
    return {
        "ru": "Готово.",
        "pl": "Gotowe.",
    }.get(language_root, "Done.")


def _intent_script_entries(records: list[dict[str, str]]) -> str:
    """Render fixed-entity native intent_script mappings without slots."""
    entries: list[str] = []
    for item in records:
        entries.extend(
            [
                f"{_intent_type(item)}:",
                "  action:",
                "    - service: script.turn_on",
                "      target:",
                "        entity_id: "
                f"{json.dumps(item['entity_id'], ensure_ascii=False)}",
                "  speech:",
                "    text: "
                f"{json.dumps(_success_speech(item['language']), ensure_ascii=False)}",
            ]
        )
    return "\n".join(entries)


def _managed_config_block(
    entries: str, newline: str, indent: str = ""
) -> list[str]:
    """Build the marked portion of the intent_script configuration."""
    lines = [f"{indent}{CONFIG_BEGIN_MARKER}{newline}"]
    lines.extend(f"{indent}{line}{newline}" for line in entries.splitlines())
    lines.append(f"{indent}{CONFIG_END_MARKER}{newline}")
    return lines


def _without_managed_config_block(contents: str) -> str:
    """Remove the previous integration-managed mapping, if present."""
    retained: list[str] = []
    in_block = False
    for line in contents.splitlines(keepends=True):
        marker = line.strip()
        if marker == CONFIG_BEGIN_MARKER:
            if in_block:
                raise NativeIntentConfigError(
                    "Managed intent configuration has nested markers"
                )
            in_block = True
            continue
        if marker == CONFIG_END_MARKER:
            if not in_block:
                raise NativeIntentConfigError(
                    "Managed intent configuration has an unmatched end marker"
                )
            in_block = False
            continue
        if not in_block:
            retained.append(line)
    if in_block:
        raise NativeIntentConfigError("Managed intent configuration is incomplete")
    return "".join(retained)


def _merge_intent_script_config(contents: str, entries: str) -> str:
    """Insert managed intents into an existing mapping without replacing user data."""
    newline = "\r\n" if "\r\n" in contents else "\n"
    clean_contents = _without_managed_config_block(contents)
    lines = clean_contents.splitlines(keepends=True)
    section_matches = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^(?:['\"]?intent_script['\"]?)\s*:", line.rstrip("\r\n"))
    ]
    if len(section_matches) > 1:
        raise NativeIntentConfigError(
            "Multiple top-level intent_script sections prevent safe activation"
        )

    existing_lines = clean_contents.splitlines()
    entry_names = [
        line[:-1]
        for line in entries.splitlines()
        if line and not line[0].isspace() and line.endswith(":")
    ]
    for entry_name in entry_names:
        if any(
            re.match(rf"^\s*{re.escape(entry_name)}\s*:", line)
            for line in existing_lines
        ):
            raise NativeIntentConfigError(
                "A generated intent type conflicts with existing configuration"
            )
    if not section_matches:
        prefix = clean_contents
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        if prefix and not prefix.endswith(newline * 2):
            prefix += newline
        root_block = [
            f"{CONFIG_BEGIN_MARKER}{newline}",
            f"intent_script:{newline}",
        ]
        root_block.extend(f"  {line}{newline}" for line in entries.splitlines())
        root_block.append(f"{CONFIG_END_MARKER}{newline}")
        return prefix + "".join(root_block)

    section_start = section_matches[0]
    section_line = lines[section_start].rstrip("\r\n")
    value = section_line.split(":", 1)[1].strip()
    empty_inline = re.fullmatch(r"\{\}(?:\s+#.*)?", value)
    if empty_inline:
        comment = re.search(r"\s+(#.*)$", value)
        suffix = f" {comment.group(1)}" if comment else ""
        lines[section_start] = f"intent_script:{suffix}{newline}"
    elif value.startswith("#"):
        pass
    elif value:
        raise NativeIntentConfigError(
            "The existing intent_script section uses an include or inline value; "
            "merge the generated mappings into that section manually"
        )
    if not lines[section_start].endswith(("\n", "\r")):
        lines[section_start] += newline

    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        text = lines[index].rstrip("\r\n")
        if (
            text.strip()
            and not text.lstrip().startswith("#")
            and not text[0].isspace()
        ):
            section_end = index
            break
    child_indent = "  "
    for line in lines[section_start + 1 : section_end]:
        text = line.rstrip("\r\n")
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        prefix = text[: len(text) - len(text.lstrip())]
        if "\t" in prefix:
            raise NativeIntentConfigError(
                "The existing intent_script section uses tab indentation"
            )
        child_indent = prefix
        break
    managed = _managed_config_block(entries, newline, child_indent)
    lines[section_end:section_end] = managed
    return "".join(lines)


def _render_sentences(records: list[dict[str, str]]) -> dict[str, str]:
    """Render one no-slot custom sentence file for each language."""
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in records:
        grouped[item["language"]].append(item)

    rendered: dict[str, str] = {}
    for language, items in grouped.items():
        lines = [
            "# Managed by Smart Home Teacher",
            f"language: {json.dumps(language, ensure_ascii=False)}",
            "intents:",
        ]
        for item in items:
            lines.extend(
                [
                    f"  {_intent_type(item)}:",
                    "    data:",
                    "      - sentences:",
                    f"          - {json.dumps(item['phrase'], ensure_ascii=False)}",
                ]
            )
        rendered[language] = "\n".join(lines) + "\n"
    return rendered


def _storage_content(records: list[dict[str, str]]) -> str:
    """Serialize records in their stable storage format."""
    payload = {
        "version": 3,
        "actions": [
            {
                "phrase": item["phrase"],
                "entity_id": item["entity_id"],
                "language": item["language"],
            }
            for item in records
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _sync_native_artifacts_sync(
    hass: HomeAssistant, records: list[dict[str, str]]
) -> None:
    """Persist mappings and their HA-native sentence and intent configuration."""
    config_path = Path(hass.config.path("configuration.yaml"))
    if not config_path.is_file():
        raise NativeIntentConfigError(
            "configuration.yaml is missing; native intent configuration was not changed"
        )
    try:
        old_config = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NativeIntentConfigError("configuration.yaml could not be read") from exc

    intent_entries = _intent_script_entries(records)
    try:
        new_config = (
            _merge_intent_script_config(old_config, intent_entries)
            if intent_entries
            else _without_managed_config_block(old_config)
        )
    except NativeIntentConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise NativeIntentConfigError(
            "configuration.yaml cannot safely accept native intent mappings"
        ) from exc

    try:
        actions_path = _actions_path(hass)
    except OSError as exc:
        raise NativeIntentConfigError(
            "Learned custom action storage is unavailable"
        ) from exc
    writes: dict[Path, str | None] = {
        config_path: new_config,
        actions_path: _storage_content(records) if records else None,
    }
    rendered_sentences = _render_sentences(records)
    for language, content in rendered_sentences.items():
        writes[
            Path(
                hass.config.path(
                    "custom_sentences", language, CUSTOM_SENTENCE_FILENAME
                )
            )
        ] = content
    sentence_directory = Path(hass.config.path("custom_sentences"))
    if sentence_directory.is_dir():
        for path in sentence_directory.glob(f"*/{CUSTOM_SENTENCE_FILENAME}"):
            if path in writes:
                continue
            try:
                is_managed = path.read_text(encoding="utf-8").startswith(
                    "# Managed by Smart Home Teacher"
                )
            except OSError as exc:
                raise NativeIntentConfigError(
                    f"Could not read generated configuration file {path.name}"
                ) from exc
            if is_managed:
                writes[path] = None

    originals: dict[Path, str | None] = {}
    for path in writes:
        try:
            originals[path] = (
                path.read_text(encoding="utf-8") if path.is_file() else None
            )
        except OSError as exc:
            raise NativeIntentConfigError(
                f"Could not read generated configuration file {path.name}"
            ) from exc
        if (
            path.name == CUSTOM_SENTENCE_FILENAME
            and originals[path] is not None
            and not originals[path].startswith("# Managed by Smart Home Teacher")
        ):
            raise NativeIntentConfigError(
                f"Refusing to overwrite an unmanaged sentence file: {path.name}"
            )

    written: list[Path] = []
    try:
        for path, content in writes.items():
            if originals[path] == content:
                continue
            if content is None:
                path.unlink()
            else:
                _atomic_write(path, content)
            written.append(path)
    except OSError as exc:
        for path in reversed(written):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, original)
            except OSError:
                _LOGGER.exception(
                    "Could not roll back generated native intent file %s", path
                )
        raise NativeIntentConfigError(
            "Could not persist the complete native intent configuration"
        ) from exc


async def _activate_native_intents(
    hass: HomeAssistant, records: list[dict[str, str]]
) -> None:
    """Load generated mappings through Home Assistant's native Intent Script integration."""
    if not records:
        if hass.services.has_service("intent_script", "reload"):
            await hass.services.async_call(
                "intent_script", "reload", blocking=True
            )
        elif "intent_script" in hass.config.components:
            raise HomeAssistantError(
                "intent_script is loaded but its reload service is unavailable"
            )
        return
    intents = {
        _intent_type(item): {
            "action": [
                {
                    "service": "script.turn_on",
                    "target": {"entity_id": item["entity_id"]},
                }
            ],
            "speech": {"text": _success_speech(item["language"])},
        }
        for item in records
    }
    if hass.services.has_service("intent_script", "reload"):
        await hass.services.async_call(
            "intent_script", "reload", blocking=True
        )
        return
    if "intent_script" in hass.config.components:
        raise HomeAssistantError(
            "intent_script is loaded but its reload service is unavailable"
        )
    if not await async_setup_component(
        hass, "intent_script", {"intent_script": intents}
    ):
        raise HomeAssistantError("Home Assistant could not load intent_script")


async def _reload_sentences(hass: HomeAssistant, languages: set[str]) -> None:
    """Refresh the built-in agent's cached sentences for generated languages."""
    if not languages:
        return
    default_agent = conversation.async_get_agent(
        hass, conversation.HOME_ASSISTANT_AGENT
    )
    if default_agent is None:
        _LOGGER.warning(
            "Built-in conversation agent is not available; sentence cache was not refreshed"
        )
        return
    for language in sorted(languages):
        await default_agent.async_reload(language)
        _LOGGER.info(
            "Refreshed built-in Assist sentence cache for language=%s", language
        )


async def async_setup_custom_actions(hass: HomeAssistant, entry) -> None:
    """Restore learned mappings and activate them as native HA intents."""
    try:
        records = await hass.async_add_executor_job(_read_actions_sync, hass)
    except NativeIntentConfigError as exc:
        _LOGGER.error("Could not restore native custom phrase intents: %s", exc)
        return
    if not records:
        _LOGGER.info("Restored 0 learned custom phrase(s)")
        return
    try:
        await hass.async_add_executor_job(_sync_native_artifacts_sync, hass, records)
        await _activate_native_intents(hass, records)
        await _reload_sentences(hass, {item["language"] for item in records})
    except (NativeIntentConfigError, HomeAssistantError) as exc:
        _LOGGER.error("Could not restore native custom phrase intents: %s", exc)
        return
    _LOGGER.info("Restored %s learned custom phrase(s)", len(records))


async def async_unload_custom_actions(hass: HomeAssistant, entry) -> None:
    """Leave persistent native intents installed when the conversation agent unloads."""


async def async_cleanup_learning_result(
    hass: HomeAssistant, result: dict[str, Any], request_id: str
) -> None:
    """Remove only the alias or custom phrase recorded for one learning request."""
    result_type = result.get("result_type")
    target_id = result.get("target_id")
    value = result.get("value")
    if not isinstance(target_id, str) or not isinstance(value, str):
        raise HomeAssistantError("The learning result has invalid cleanup data")

    if result_type == "custom_phrase":
        language = result.get("language")
        if not isinstance(language, str):
            raise HomeAssistantError("The custom phrase language is missing")
        await async_remove_custom_phrase(
            hass,
            value,
            target_id,
            language,
            request_id=request_id,
        )
    elif result_type == "entity_alias":
        registry = er.async_get(hass)
        entry = registry.async_get(target_id)
        if entry is None:
            raise HomeAssistantError("The test entity alias target no longer exists")
        aliases = list(entry.aliases)
        updated_aliases = [
            alias for alias in aliases if alias.casefold() != value.casefold()
        ]
        if len(updated_aliases) == len(aliases):
            raise HomeAssistantError("The test entity alias was not found")
        registry.async_update_entity(target_id, aliases=updated_aliases)
    elif result_type == "area_alias":
        registry = ar.async_get(hass)
        area = registry.async_get_area(target_id)
        if area is None:
            raise HomeAssistantError("The test area alias target no longer exists")
        aliases = set(area.aliases)
        updated_aliases = {
            alias for alias in aliases if alias.casefold() != value.casefold()
        }
        if len(updated_aliases) == len(aliases):
            raise HomeAssistantError("The test area alias was not found")
        registry.async_update(target_id, aliases=updated_aliases)
    else:
        raise HomeAssistantError("The learning result type cannot be cleaned")

    _LOGGER.info(
        "Cleaned learning result request_id=%s result_type=%s",
        request_id,
        result_type,
    )


async def async_remove_custom_phrase(
    hass: HomeAssistant,
    phrase_value: Any,
    entity_id: Any,
    requested_language: str,
    request_id: str | None = None,
) -> None:
    """Remove one exact learned phrase and refresh the remaining native intents."""
    phrase = _normalise_phrase(phrase_value)
    language = _language_variant(hass, requested_language)
    if (
        not isinstance(entity_id, str)
        or not entity_id.startswith("script.")
        or not phrase
        or not language
    ):
        raise HomeAssistantError("The custom phrase cleanup request is invalid")

    records = await hass.async_add_executor_job(_read_actions_sync, hass)
    remaining: list[dict[str, str]] = []
    removed_record = None
    for record in records:
        if (
            removed_record is None
            and record["entity_id"] == entity_id
            and record["language"] == language
            and record["phrase"].casefold() == phrase.casefold()
        ):
            removed_record = record
        else:
            remaining.append(record)
    if removed_record is None:
        raise HomeAssistantError("The exact custom phrase mapping was not found")

    await hass.async_add_executor_job(_sync_native_artifacts_sync, hass, remaining)
    await _activate_native_intents(hass, remaining)
    await _reload_sentences(hass, {language})
    _LOGGER.info(
        "Removed test custom phrase request_id=%s language=%s",
        request_id or "untracked",
        language,
    )


async def async_add_custom_phrase(
    hass: HomeAssistant,
    entity_id: Any,
    phrase_value: Any,
    requested_language: str,
    request_id: str | None = None,
) -> str:
    """Persist an exact phrase and activate its fixed-target native HA intent."""
    phrase = _normalise_phrase(phrase_value)
    language = _language_variant(hass, requested_language)
    if (
        not isinstance(entity_id, str)
        or not entity_id.startswith("script.")
        or not phrase
        or not language
    ):
        return (
            "Custom phrase rejected: expected a valid phrase, supported language "
            "and script entity"
        )
    if hass.states.get(entity_id) is None or not async_should_expose(
        hass, "conversation", entity_id
    ):
        return "Custom phrase rejected: script is missing or not exposed to Assist"

    try:
        records = await hass.async_add_executor_job(_read_actions_sync, hass)
    except NativeIntentConfigError as exc:
        _LOGGER.error(
            "Could not read learned custom actions request_id=%s: %s",
            request_id or "untracked",
            exc,
        )
        return "Custom phrase rejected: saved mappings could not be read"
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
    updated_records = [*records, item]
    try:
        await hass.async_add_executor_job(
            _sync_native_artifacts_sync, hass, updated_records
        )
    except NativeIntentConfigError as exc:
        _LOGGER.error(
            "Could not activate learned custom phrase request_id=%s: %s",
            request_id or "untracked",
            exc,
        )
        return f"Custom phrase rejected: {exc}"

    try:
        await _activate_native_intents(hass, updated_records)
        await _reload_sentences(hass, {language})
    except HomeAssistantError as exc:
        _LOGGER.error(
            "Native intent configuration was saved but could not be activated "
            "request_id=%s: %s",
            request_id or "untracked",
            exc,
        )
        return (
            "Custom phrase saved, but Home Assistant could not activate its "
            "native intent; check the logs"
        )

    _LOGGER.debug(
        "Learning trace detail stage=phrase_persisted request_id=%s "
        "entity_id=%s language=%s phrase=%r",
        request_id or "untracked",
        entity_id,
        language,
        phrase,
    )
    _LOGGER.warning(
        "SMART_HOME_TEACHER_CUSTOM_PHRASE_RECORDED request_id=%s language=%s",
        request_id or "untracked",
        language,
    )
    return (
        "Custom phrase activated as a Home Assistant native intent: "
        f"{phrase} -> {entity_id}"
    )
