"""Conversation agent that queues requests and only learns aliases or custom phrases."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, Literal
from uuid import uuid4

from aiohttp import ClientError, ClientTimeout

from homeassistant.components import conversation
from homeassistant.components.conversation.models import ConversationInput, ConversationResult
from homeassistant.components.conversation.chat_log import ChatLog, UserContent, AssistantContent
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import EVENT_LEARNING_RESULT, OLLAMA_MODEL, OLLAMA_URL
from .custom_actions import async_add_custom_phrase

_LOGGER = logging.getLogger(__name__)

OLLAMA_TIMEOUT_SECONDS = 3600
_PROMPT_LANGUAGES = frozenset({"en", "ru"})
_PROMPT_ROLES = frozenset(
    {"orchestrator", "alias_worker", "intent_worker", "validator"}
)
MAX_AGENT_ATTEMPTS = 2
ORCHESTRATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_target_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "worker_brief": {"type": "string"},
    },
    "required": ["focus_target_ids", "worker_brief"],
    "additionalProperties": False,
}
ALIAS_WORKER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["entity_alias", "area_alias", "not_alias", "ignore"],
        },
        "target_id": {"type": "string"},
        "alias": {"type": "string"},
    },
    "required": ["type", "target_id", "alias"],
    "additionalProperties": False,
}
INTENT_WORKER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["custom_phrase", "ignore"]},
        "target_id": {"type": "string"},
        "phrase": {"type": "string"},
    },
    "required": ["type", "target_id", "phrase"],
    "additionalProperties": False,
}
VALIDATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["accept", "retry", "reject"],
        },
        "reason": {"type": "string"},
        "candidate_target_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "guidance": {"type": "string"},
    },
    "required": ["decision", "reason", "candidate_target_ids", "guidance"],
    "additionalProperties": False,
}
FALLBACK_RESPONSES = {
    "ru": "Не понял запрос, но подумаю, как его обработать",
    "en": "I did not understand the request. I will consider how to handle it",
    "pl": "Nie rozumiem polecenia. Zastanowię się, jak je obsłużyć",
}


@lru_cache(maxsize=len(_PROMPT_LANGUAGES) * len(_PROMPT_ROLES))
def _load_system_prompt_template(role: str, language: str) -> Template:
    """Load one localized agent-role prompt template."""
    if role not in _PROMPT_ROLES:
        raise ValueError(f"Unknown Smart Home Teacher prompt role: {role}")
    filename = f"{role}_{language}.txt"
    prompt_path = Path(__file__).parent / "prompts" / filename
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read Smart Home Teacher prompt: {prompt_path}") from exc
    return Template(prompt)


def _display_name(language: str) -> str:
    """Return the project name in the Home Assistant UI language."""
    return "Учитель умного дома" if language.replace("_", "-").lower().split("-")[0] == "ru" else "Smart Home Teacher"


def _language_root(language: str) -> str:
    """Return the base language code for user-facing acknowledgement text."""
    return language.replace("_", "-").split("-")[0].lower()


def _normalise_text(value: str) -> str:
    """Normalize whitespace and punctuation for literal-fragment checks."""
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def _clean_alias(value: Any, user_text: str) -> str | None:
    """Validate a short alias that is a literal fragment of the user's utterance."""
    if not isinstance(value, str):
        return None
    alias = " ".join(value.strip().split()).strip(" .,!?:;\"'")
    if not 2 <= len(alias) <= 48 or len(alias.split()) > 4:
        return None
    if re.search(r"[\n\r{}<>\[\]]", alias):
        return None
    alias_normalized = _normalise_text(alias)
    utterance_normalized = _normalise_text(user_text)
    if not alias_normalized or alias_normalized == utterance_normalized:
        return None
    if f" {alias_normalized} " not in f" {utterance_normalized} ":
        return None
    return alias


def _alias_slot(native_match: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return one unknown name/area slot from HA's non-executing intent probe."""
    if not isinstance(native_match, dict) or native_match.get("match") != "partial":
        return None
    unmatched_slots = native_match.get("unmatched_slots")
    if (
        not isinstance(native_match.get("intent"), str)
        or not isinstance(unmatched_slots, dict)
        or len(unmatched_slots) != 1
    ):
        return None
    slot_name, slot_value = next(iter(unmatched_slots.items()))
    if slot_name not in {"name", "area"} or not isinstance(slot_value, str):
        return None
    return slot_name, slot_value


def _native_match_evidence(native_match: Any) -> dict[str, Any]:
    """Bound Home Assistant's debug-recognition result before sending it to a model."""
    if native_match is None:
        native_match = {}
    if not isinstance(native_match, dict):
        return {
            "match": "unavailable",
            "intent": None,
            "sentence_template": "",
            "slots": {},
            "unmatched_slots": {},
            "source": "",
        }
    intent_data = native_match.get("intent")
    intent_name = (
        intent_data.get("name")
        if isinstance(intent_data, dict)
        and isinstance(intent_data.get("name"), str)
        else None
    )
    slots = native_match.get("slots")
    unmatched_slots = native_match.get("unmatched_slots")
    return {
        "match": (
            "matched"
            if native_match.get("match") is True
            else "partial"
            if intent_name
            else "no_intent_match"
        ),
        "intent": intent_name,
        "sentence_template": (
            native_match.get("sentence_template", "")[:300]
            if isinstance(native_match.get("sentence_template"), str)
            else ""
        ),
        "slots": {
            key: value[:120]
            for key, value in slots.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(slots, dict)
        else {},
        "unmatched_slots": {
            key: str(value)[:120]
            for key, value in unmatched_slots.items()
            if isinstance(key, str) and isinstance(value, (str, int, float))
        }
        if isinstance(unmatched_slots, dict)
        else {},
        "source": (
            native_match.get("source", "")[:40]
            if isinstance(native_match.get("source"), str)
            else ""
        ),
    }


def _parse_json(content: str) -> dict[str, Any]:
    """Parse a JSON response, including a response wrapped in code fences."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("The model response is not a JSON object")
    return value


def _learning_outcome_status(outcome: str) -> str:
    """Return a log-safe status without exposing the learned text or entity."""
    if outcome.startswith(
        (
            "Alias saved:",
            "Area alias saved:",
            "Custom phrase activated as a Home Assistant native intent:",
        )
    ):
        return "saved"
    if outcome.startswith("Custom phrase saved, but"):
        return "failed"
    if "already" in outcome.casefold():
        return "duplicate"
    return "rejected"


def _learning_outcome_persisted(outcome: str) -> bool:
    """Return whether this request wrote an alias or phrase that can be cleaned."""
    return outcome.startswith(
        (
            "Alias saved:",
            "Area alias saved:",
            "Custom phrase activated as a Home Assistant native intent:",
            "Custom phrase saved, but",
        )
    )


class SmartHomeTeacherAgent(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    """Queue each fallback utterance and acknowledge it immediately."""

    _attr_has_entity_name = False

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = _display_name(hass.config.language)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        """Register the agent and start its single FIFO background worker."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)
        self._worker_task = self.entry.async_create_background_task(
            self.hass,
            self._async_queue_worker(),
            "smart_home_teacher_queue_worker",
        )
        _LOGGER.warning(
            "SMART_HOME_TEACHER_AGENT_REGISTERED entity_id=%s entry_id=%s queue_worker=started",
            self.entity_id,
            self.entry.entry_id,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the worker when the integration entity is unloaded."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Allow Assist requests in any language supported by the selected pipeline."""
        return MATCH_ALL

    def _snapshot_chat_log(self, chat_log: ChatLog, user_text: str) -> list[dict[str, str]]:
        """Copy recent chat content so the queue does not retain a mutable ChatLog."""
        messages: list[dict[str, str]] = []
        for item in chat_log.content[-4:]:
            if isinstance(item, UserContent):
                role = "user"
            elif isinstance(item, AssistantContent):
                role = "assistant"
            else:
                continue
            content = getattr(item, "content", "")
            if isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content.strip()})
        if not messages or messages[-1]["role"] != "user" or messages[-1]["content"] != user_text:
            messages.append({"role": "user", "content": user_text})
        return messages[-4:]

    async def _async_debug_recognize(
        self, user_input: ConversationInput
    ) -> dict[str, Any] | None:
        """Probe HA's native grammar without handling or executing the intent."""
        default_agent = conversation.async_get_agent(
            self.hass, conversation.HOME_ASSISTANT_AGENT
        )
        recognizer = getattr(default_agent, "async_debug_recognize", None)
        if not callable(recognizer):
            raise RuntimeError(
                "Home Assistant's non-executing intent recognizer is unavailable"
            )
        result = await recognizer(user_input)
        if result is not None and not isinstance(result, dict):
            raise RuntimeError("Home Assistant returned invalid intent match data")
        return result

    async def _async_queue_worker(self) -> None:
        """Process queued utterances sequentially; never execute a device action here."""
        _LOGGER.warning("Smart Home Teacher background learning worker started")
        while True:
            job = await self._queue.get()
            try:
                await self._process_queued_message(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.error(
                    "Learning trace stage=worker status=failed request_id=%s error_type=%s",
                    job.get("request_id", "unknown"),
                    type(exc).__name__,
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    async def _process_queued_message(self, job: dict[str, Any]) -> None:
        """Match HA grammar, then learn an alias or a native custom phrase."""
        request_id = job["request_id"]
        user_text = job["text"]
        language = job["language"]
        assist_context = job.get("assist_context", "")
        stage = "entity_discovery"
        _LOGGER.info(
            "Learning trace stage=processing_started request_id=%s language=%s pending_queue_size=%s",
            request_id,
            language,
            self._queue.qsize(),
        )
        try:
            stage = "native_intent_probe"
            native_match = await self._async_debug_recognize(
                job["conversation_input"]
            )
            native_evidence = _native_match_evidence(native_match)
            _LOGGER.info(
                "Learning trace stage=native_intent_probe request_id=%s match=%s intent=%s",
                request_id,
                native_evidence["match"],
                native_evidence["intent"] or "none",
            )
            _LOGGER.debug(
                "Learning trace detail stage=native_intent_probe "
                "request_id=%s evidence=%r",
                request_id,
                native_evidence,
            )
            if native_evidence["match"] == "matched":
                _LOGGER.info(
                    "Learning trace stage=completed request_id=%s status=skipped "
                    "reason=native_intent_already_matches",
                    request_id,
                )
                return

            entities = self._get_exposed_entities(user_text)
            areas = self._get_assist_areas()
            _LOGGER.info(
                "Learning trace stage=entity_discovery_complete request_id=%s "
                "candidate_count=%s area_count=%s",
                request_id,
                len(entities),
                len(areas),
            )
            _LOGGER.debug(
                "Learning trace detail stage=entity_discovery_complete "
                "request_id=%s candidates=%r areas=%r",
                request_id,
                entities,
                areas,
            )
            if not entities:
                _LOGGER.info(
                    "Learning trace stage=completed request_id=%s status=skipped "
                    "reason=no_exposed_entities",
                    request_id,
                )
                return

            stage = "agent_orchestration"
            result = await self._orchestrate_classification(
                user_text,
                entities,
                areas,
                job["chat_messages"],
                language,
                assist_context,
                native_evidence,
                request_id,
            )
            if result is None:
                _LOGGER.info(
                    "Learning trace stage=completed request_id=%s status=rejected",
                    request_id,
                )
                return
            result_type = result.get("type")
            outcome: str | None = None
            _LOGGER.debug(
                "Learning trace detail stage=classification_complete "
                "request_id=%s model_result=%r",
                request_id,
                result,
            )
            _LOGGER.info(
                "Learning trace stage=classification_complete request_id=%s result_type=%s",
                request_id,
                result_type,
            )

            stage = "validation_persistence"
            allowed_entity_ids = {item["entity_id"] for item in entities}
            allowed_area_ids = {item["area_id"] for item in areas}
            if result_type == "entity_alias":
                outcome = await self._teach_alias(
                    result.get("target_id", ""),
                    result.get("alias"),
                    user_text,
                    allowed_entity_ids,
                    native_match,
                    job["conversation_input"],
                    language,
                )
                _LOGGER.debug(
                    "Learning trace detail stage=validation_persistence_complete "
                    "request_id=%s model_result=%r outcome=%r",
                    request_id,
                    result,
                    outcome,
                )
                _LOGGER.info(
                    "Learning trace stage=validation_persistence_complete "
                    "request_id=%s result_type=entity_alias status=%s",
                    request_id,
                    _learning_outcome_status(outcome),
                )
            elif result_type == "area_alias":
                outcome = await self._teach_area_alias(
                    result.get("target_id", ""),
                    result.get("alias"),
                    user_text,
                    allowed_area_ids,
                    native_match,
                    job["conversation_input"],
                    language,
                )
                _LOGGER.debug(
                    "Learning trace detail stage=validation_persistence_complete "
                    "request_id=%s model_result=%r outcome=%r",
                    request_id,
                    result,
                    outcome,
                )
                _LOGGER.info(
                    "Learning trace stage=validation_persistence_complete "
                    "request_id=%s result_type=area_alias status=%s",
                    request_id,
                    _learning_outcome_status(outcome),
                )
            elif result_type == "custom_phrase":
                target_entity_id = result.get("target_id", "")
                if target_entity_id not in allowed_entity_ids:
                    outcome = "Custom phrase rejected: target was not in the exposed entity list sent to the model"
                else:
                    outcome = await async_add_custom_phrase(
                        self.hass,
                        target_entity_id,
                        result.get("phrase"),
                        language,
                        request_id=request_id,
                    )
                _LOGGER.debug(
                    "Learning trace detail stage=validation_persistence_complete "
                    "request_id=%s model_result=%r outcome=%r",
                    request_id,
                    result,
                    outcome,
                )
                _LOGGER.info(
                    "Learning trace stage=validation_persistence_complete "
                    "request_id=%s result_type=custom_phrase status=%s",
                    request_id,
                    _learning_outcome_status(outcome),
                )
            elif result_type == "ignore":
                _LOGGER.info(
                    "Learning trace stage=completed request_id=%s status=ignored",
                    request_id,
                )
            if outcome is not None and _learning_outcome_persisted(outcome):
                value_key = "phrase" if result_type == "custom_phrase" else "alias"
                value = result.get(value_key)
                target_id = result.get("target_id")
                if isinstance(value, str) and isinstance(target_id, str):
                    cleanup_event = {
                        "request_id": request_id,
                        "result_type": result_type,
                        "target_id": target_id,
                        "value": value,
                        "language": language,
                    }
                    self.hass.bus.async_fire(
                        EVENT_LEARNING_RESULT,
                        cleanup_event,
                    )
                    _LOGGER.info(
                        "Learning trace stage=cleanup_event_dispatched "
                        "request_id=%s result_type=%s",
                        request_id,
                        result_type,
                    )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ClientError, ValueError, json.JSONDecodeError) as exc:
            _LOGGER.warning(
                "Learning trace stage=%s request_id=%s status=failed error_type=%s",
                stage,
                request_id,
                type(exc).__name__,
                exc_info=True,
            )
        except Exception as exc:
            _LOGGER.error(
                "Learning trace stage=%s request_id=%s status=failed error_type=%s",
                stage,
                request_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _get_exposed_entities(self, query: str) -> list[dict[str, Any]]:
        """Read exposed entity names and aliases without sending device state."""
        registry = er.async_get(self.hass)
        query_words = {
            word for word in re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE) if len(word) > 2
        }
        entities: list[dict[str, Any]] = []
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            if not async_should_expose(self.hass, "conversation", entity_id):
                continue
            entry = registry.async_get(entity_id)
            aliases = [alias for alias in getattr(entry, "aliases", []) if isinstance(alias, str)] if entry else []
            names: list[str] = []
            if state.attributes.get("friendly_name"):
                names.append(str(state.attributes["friendly_name"]))
            if entry:
                for name in (getattr(entry, "name", None), getattr(entry, "original_name", None)):
                    if name and name not in names:
                        names.append(str(name))
            names.extend(alias for alias in aliases if alias not in names)
            item = {
                "entity_id": entity_id,
                "domain": entity_id.split(".", 1)[0],
                "names": names,
                "aliases": aliases,
                "area_id": (
                    er.async_get_effective_area_id(self.hass, entry)
                    if entry
                    else None
                ),
            }
            searchable = " ".join([entity_id, *names, *aliases]).casefold()
            item["_match_score"] = len(
                query_words & set(re.findall(r"[^\W_]+", searchable, flags=re.UNICODE))
            )
            entities.append(item)
        entities.sort(key=lambda item: item["_match_score"], reverse=True)
        for item in entities:
            item.pop("_match_score", None)
        return entities[:64]

    def _get_assist_areas(self) -> list[dict[str, Any]]:
        """Return area names and aliases available to the native intent matcher."""
        registry = ar.async_get(self.hass)
        return [
            {
                "area_id": area.id,
                "name": area.name,
                "aliases": sorted(area.aliases),
            }
            for area in registry.async_list_areas()
        ][:128]

    def _system_prompt(self, language: str, role: str = "alias_worker") -> str:
        """Return the localized, role-specific system prompt."""
        prompt_language = _language_root(language)
        if prompt_language not in _PROMPT_LANGUAGES:
            prompt_language = "en"
        return _load_system_prompt_template(role, prompt_language).substitute(
            REQUEST_LANGUAGE=json.dumps(language, ensure_ascii=False)
        )

    @staticmethod
    def _recent_context(
        chat_messages: list[dict[str, str]], user_text: str
    ) -> list[dict[str, str]]:
        """Return recent prior messages without duplicating the current utterance."""
        recent_context = chat_messages[-4:]
        if (
            recent_context
            and recent_context[-1].get("role") == "user"
            and recent_context[-1].get("content") == user_text
        ):
            recent_context = recent_context[:-1]
        return recent_context

    async def _ollama_orchestrator(
        self,
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        chat_messages: list[dict[str, str]],
        language: str,
        assist_context: str,
        native_match: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Prepare a bounded worker brief without deciding or persisting a result."""
        orchestrator_input = {
            "request_language": language,
            "latest_utterance": user_text,
            "recent_chat_context": self._recent_context(chat_messages, user_text),
            "assist_context": assist_context[:8000],
            "native_grammar_probe": native_match,
            "exposed_entities": entities,
            "areas": areas,
        }
        return await self._call_ollama_agent(
            "orchestrator",
            self._system_prompt(language, "orchestrator"),
            orchestrator_input,
            ORCHESTRATOR_RESPONSE_SCHEMA,
            request_id,
            1,
            256,
        )

    async def _ollama_alias_worker(
        self,
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        chat_messages: list[dict[str, str]],
        language: str,
        assist_context: str,
        native_match: dict[str, Any],
        request_id: str,
        validator_feedback: dict[str, Any] | None = None,
        attempt: int = 1,
        orchestrator_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run alias analysis with request and environment data isolated as JSON."""
        worker_input = {
            "request_language": language,
            "latest_utterance": user_text,
            "recent_chat_context": self._recent_context(chat_messages, user_text),
            "assist_context": assist_context[:8000],
            "native_grammar_probe": native_match,
            "exposed_entities": entities,
            "areas": areas,
            "orchestrator_guidance": orchestrator_guidance,
            "validator_feedback": validator_feedback,
        }
        return await self._call_ollama_agent(
            "alias_worker",
            self._system_prompt(language, "alias_worker"),
            worker_input,
            ALIAS_WORKER_RESPONSE_SCHEMA,
            request_id,
            attempt,
            192,
        )

    async def _ollama_intent_worker(
        self,
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        chat_messages: list[dict[str, str]],
        language: str,
        assist_context: str,
        native_match: dict[str, Any],
        request_id: str,
        validator_feedback: dict[str, Any] | None = None,
        attempt: int = 1,
        orchestrator_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose one native custom intent after alias learning was ruled out."""
        worker_input = {
            "request_language": language,
            "latest_utterance": user_text,
            "recent_chat_context": self._recent_context(chat_messages, user_text),
            "assist_context": assist_context[:8000],
            "native_grammar_probe": native_match,
            "exposed_entities": entities,
            "areas": areas,
            "orchestrator_guidance": orchestrator_guidance,
            "validator_feedback": validator_feedback,
        }
        return await self._call_ollama_agent(
            "intent_worker",
            self._system_prompt(language, "intent_worker"),
            worker_input,
            INTENT_WORKER_RESPONSE_SCHEMA,
            request_id,
            attempt,
            192,
        )

    async def _ollama_validator(
        self,
        stage: str,
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        chat_messages: list[dict[str, str]],
        language: str,
        assist_context: str,
        native_match: dict[str, Any],
        worker_result: dict[str, Any],
        deterministic_errors: list[str],
        request_id: str,
        orchestrator_guidance: dict[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        """Independently verify the worker proposal against the request snapshot."""
        validator_input = {
            "request_language": language,
            "latest_utterance": user_text,
            "recent_chat_context": self._recent_context(chat_messages, user_text),
            "assist_context": assist_context[:8000],
            "native_grammar_probe": native_match,
            "exposed_entities": entities,
            "areas": areas,
            "validation_stage": stage,
            "deterministic_validation_errors": deterministic_errors,
            "orchestrator_guidance": orchestrator_guidance,
            "worker_result": worker_result,
        }
        return await self._call_ollama_agent(
            "validator",
            self._system_prompt(language, "validator"),
            validator_input,
            VALIDATOR_RESPONSE_SCHEMA,
            request_id,
            attempt,
            256,
        )

    async def _call_ollama_agent(
        self,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        response_schema: dict[str, Any],
        request_id: str,
        attempt: int,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Make one bounded, schema-constrained Ollama chat call."""
        _LOGGER.info(
            "Learning trace stage=agent_request_started request_id=%s agent=%s "
            "attempt=%s model=%s",
            request_id,
            role,
            attempt,
            OLLAMA_MODEL,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(input_data, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        payload = {
            "model": OLLAMA_MODEL,
            "stream": False,
            "format": response_schema,
            "think": False,
            "keep_alive": "10m",
            "messages": messages,
            "options": {
                "temperature": 0,
                "top_p": 0.85,
                "num_predict": max_tokens,
            },
        }
        _LOGGER.debug(
            "Learning trace detail stage=agent_request request_id=%s agent=%s payload=%s",
            request_id,
            role,
            json.dumps(payload, ensure_ascii=False),
        )
        session = async_get_clientsession(self.hass)
        timeout = ClientTimeout(
            total=OLLAMA_TIMEOUT_SECONDS,
            connect=30,
            sock_connect=30,
            sock_read=OLLAMA_TIMEOUT_SECONDS,
        )
        async with session.post(
            f"{OLLAMA_URL.rstrip('/')}/api/chat", json=payload, timeout=timeout
        ) as response:
            response.raise_for_status()
            result = await response.json()
        if not isinstance(result, dict):
            raise ValueError(f"Ollama {role} agent returned an invalid response object")
        _LOGGER.debug(
            "Learning trace detail stage=agent_response request_id=%s agent=%s response=%s",
            request_id,
            role,
            json.dumps(result, ensure_ascii=False),
        )
        message = result.get("message")
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(content, str) or not content:
            raise ValueError(f"Ollama {role} agent returned an empty response")
        parsed = _parse_json(content)
        _LOGGER.info(
            "Learning trace stage=agent_response_parsed request_id=%s agent=%s",
            request_id,
            role,
        )
        return parsed

    async def _orchestrate_classification(
        self,
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        chat_messages: list[dict[str, str]],
        language: str,
        assist_context: str,
        native_match: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any] | None:
        """Validate aliases first; only a clear non-alias result reaches intent design."""
        entity_ids = {item["entity_id"] for item in entities}
        area_ids = {item["area_id"] for item in areas}
        allowed_target_ids = entity_ids | area_ids
        targets = {
            **{item["entity_id"]: item for item in entities},
            **{item["area_id"]: item for item in areas},
        }
        _LOGGER.info(
            "Learning trace stage=orchestration_started request_id=%s "
            "max_attempts_per_stage=%s",
            request_id,
            MAX_AGENT_ATTEMPTS,
        )
        orchestrator_result = await self._ollama_orchestrator(
            user_text,
            entities,
            areas,
            chat_messages,
            language,
            assist_context,
            native_match,
            request_id,
        )
        orchestrator_guidance = self._normalise_orchestrator_result(
            orchestrator_result, allowed_target_ids
        )
        if orchestrator_guidance is None:
            _LOGGER.warning(
                "Learning trace stage=orchestrator_validation request_id=%s "
                "status=invalid_output",
                request_id,
            )
            return None
        _LOGGER.info(
            "Learning trace stage=orchestrator_complete request_id=%s focus_count=%s",
            request_id,
            len(orchestrator_guidance["focus_target_ids"]),
        )
        _LOGGER.debug(
            "Learning trace detail stage=orchestrator_complete "
            "request_id=%s result=%r",
            request_id,
            orchestrator_guidance,
        )
        alias_feedback = None
        alias_result: dict[str, Any] | None = None
        for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
            worker_result = await self._ollama_alias_worker(
                user_text,
                entities,
                areas,
                chat_messages,
                language,
                assist_context,
                native_match,
                request_id,
                alias_feedback,
                attempt,
                orchestrator_guidance,
            )
            worker_result = self._normalise_alias_worker_result(
                worker_result, entities, areas, user_text
            )
            if worker_result is None:
                _LOGGER.warning(
                    "Learning trace stage=worker_validation request_id=%s attempt=%s "
                    "status=invalid_output",
                    request_id,
                    attempt,
                )
                return None

            deterministic_errors = self._worker_proposal_errors(
                worker_result, user_text, entities, areas, native_match
            )
            validator_result = await self._ollama_validator(
                "alias",
                user_text,
                entities,
                areas,
                chat_messages,
                language,
                assist_context,
                native_match,
                worker_result,
                deterministic_errors,
                request_id,
                orchestrator_guidance,
                attempt,
            )
            validator_result = self._normalise_validator_result(
                validator_result, allowed_target_ids
            )
            if validator_result is None:
                _LOGGER.warning(
                    "Learning trace stage=validator_validation request_id=%s attempt=%s "
                    "status=invalid_output",
                    request_id,
                    attempt,
                )
                return None

            decision = validator_result["decision"]
            _LOGGER.info(
                "Learning trace stage=validator_complete request_id=%s attempt=%s "
                "decision=%s",
                request_id,
                attempt,
                decision,
            )
            _LOGGER.debug(
                "Learning trace detail stage=validator_complete request_id=%s "
                "attempt=%s worker_result=%r validator_result=%r",
                request_id,
                attempt,
                worker_result,
                validator_result,
            )
            if decision == "accept" and not deterministic_errors:
                alias_result = worker_result
                break
            if decision == "reject":
                if (
                    native_match.get("match") == "no_intent_match"
                    and worker_result["type"] != "ignore"
                ):
                    _LOGGER.info(
                        "Learning trace stage=alias_analysis_complete request_id=%s "
                        "attempts=%s status=no_alias reason=validator_rejected",
                        request_id,
                        attempt,
                    )
                    alias_result = {
                        "type": "not_alias",
                        "target_id": "",
                        "alias": "",
                    }
                    break
                _LOGGER.info(
                    "Learning trace stage=alias_analysis_complete request_id=%s "
                    "attempts=%s status=rejected reason=%s",
                    request_id,
                    attempt,
                    "validator_rejected",
                )
                return None
            if attempt == MAX_AGENT_ATTEMPTS:
                if (
                    native_match.get("match") == "no_intent_match"
                    and worker_result["type"] == "not_alias"
                ):
                    _LOGGER.info(
                        "Learning trace stage=alias_analysis_complete request_id=%s "
                        "attempts=%s status=no_alias reason=retry_limit",
                        request_id,
                        attempt,
                    )
                    alias_result = worker_result
                    break
                _LOGGER.info(
                    "Learning trace stage=alias_analysis_complete request_id=%s "
                    "attempts=%s status=rejected reason=retry_limit",
                    request_id,
                    attempt,
                )
                return None

            option_ids = validator_result["candidate_target_ids"]
            retry_reason = validator_result["reason"]
            retry_guidance = validator_result["guidance"]
            if deterministic_errors:
                retry_reason = "Deterministic validation rejected the proposal."
                retry_guidance = "Correct these issues: " + " ".join(
                    deterministic_errors
                )
            alias_feedback = {
                "previous_worker_result": worker_result,
                "reason": retry_reason,
                "guidance": retry_guidance,
                "candidate_targets": [targets[item] for item in option_ids],
                "deterministic_validation_errors": deterministic_errors,
            }
            _LOGGER.info(
                "Learning trace stage=alias_worker_retry_scheduled request_id=%s "
                "next_attempt=%s candidate_count=%s",
                request_id,
                attempt + 1,
                len(option_ids),
            )
            _LOGGER.debug(
                "Learning trace detail stage=alias_worker_retry_scheduled "
                "request_id=%s validator_feedback=%r",
                request_id,
                alias_feedback,
            )

        if alias_result is None:
            return None
        if alias_result["type"] in {"entity_alias", "area_alias", "ignore"}:
            return alias_result
        if native_match.get("match") != "no_intent_match":
            _LOGGER.warning(
                "Learning trace stage=intent_analysis request_id=%s "
                "status=blocked reason=native_grammar_partial_match",
                request_id,
            )
            return None

        intent_feedback = None
        for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
            worker_result = await self._ollama_intent_worker(
                user_text,
                entities,
                areas,
                chat_messages,
                language,
                assist_context,
                native_match,
                request_id,
                intent_feedback,
                attempt,
                orchestrator_guidance,
            )
            worker_result = self._normalise_intent_worker_result(
                worker_result, entities, user_text
            )
            if worker_result is None:
                _LOGGER.warning(
                    "Learning trace stage=intent_worker_validation request_id=%s "
                    "attempt=%s status=invalid_output",
                    request_id,
                    attempt,
                )
                return None

            deterministic_errors = self._intent_proposal_errors(
                worker_result, user_text, native_match
            )
            validator_result = await self._ollama_validator(
                "intent",
                user_text,
                entities,
                areas,
                chat_messages,
                language,
                assist_context,
                native_match,
                worker_result,
                deterministic_errors,
                request_id,
                orchestrator_guidance,
                attempt,
            )
            validator_result = self._normalise_validator_result(
                validator_result, allowed_target_ids
            )
            if validator_result is None:
                _LOGGER.warning(
                    "Learning trace stage=intent_validator_validation "
                    "request_id=%s attempt=%s status=invalid_output",
                    request_id,
                    attempt,
                )
                return None
            decision = validator_result["decision"]
            _LOGGER.info(
                "Learning trace stage=intent_validator_complete request_id=%s "
                "attempt=%s decision=%s",
                request_id,
                attempt,
                decision,
            )
            if decision == "accept" and not deterministic_errors:
                return worker_result
            if decision == "reject" or attempt == MAX_AGENT_ATTEMPTS:
                return None
            option_ids = validator_result["candidate_target_ids"]
            retry_reason = validator_result["reason"]
            retry_guidance = validator_result["guidance"]
            if deterministic_errors:
                retry_reason = "Deterministic validation rejected the proposal."
                retry_guidance = "Correct these issues: " + " ".join(
                    deterministic_errors
                )
            intent_feedback = {
                "previous_worker_result": worker_result,
                "reason": retry_reason,
                "guidance": retry_guidance,
                "candidate_targets": [targets[item] for item in option_ids],
                "deterministic_validation_errors": deterministic_errors,
            }
        return None

    @staticmethod
    def _normalise_orchestrator_result(
        result: dict[str, Any],
        allowed_target_ids: set[str],
    ) -> dict[str, Any] | None:
        """Accept only brief orchestrator hints grounded in the HA snapshot."""
        if (
            not isinstance(result, dict)
            or set(result) != {"focus_target_ids", "worker_brief"}
        ):
            return None
        focus_target_ids = result.get("focus_target_ids")
        worker_brief = result.get("worker_brief")
        if (
            not isinstance(focus_target_ids, list)
            or not all(isinstance(item, str) for item in focus_target_ids)
            or not isinstance(worker_brief, str)
            or not worker_brief.strip()
            or len(worker_brief) > 500
        ):
            return None
        unique_focus_ids = list(dict.fromkeys(focus_target_ids))
        if (
            len(unique_focus_ids) > len(allowed_target_ids)
            or any(item not in allowed_target_ids for item in unique_focus_ids)
        ):
            return None
        return {
            "focus_target_ids": unique_focus_ids,
            "worker_brief": worker_brief.strip(),
        }

    @staticmethod
    def _normalise_alias_worker_result(
        result: dict[str, Any],
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        user_text: str,
    ) -> dict[str, Any] | None:
        """Validate alias-worker response shape and target membership."""
        if (
            not isinstance(result, dict)
            or set(result) != {"type", "target_id", "alias"}
            or any(not isinstance(value, str) for value in result.values())
        ):
            return None
        result_type = result["type"]
        target_id = result["target_id"]
        alias_value = result["alias"]
        if result_type in {"ignore", "not_alias"}:
            if target_id or alias_value:
                return None
            return {"type": result_type, "target_id": "", "alias": ""}
        if result_type not in {"entity_alias", "area_alias"}:
            return None
        targets = entities if result_type == "entity_alias" else areas
        target_key = "entity_id" if result_type == "entity_alias" else "area_id"
        target = next((item for item in targets if item.get(target_key) == target_id), None)
        alias = _clean_alias(alias_value, user_text)
        if target is None or alias is None:
            return None
        return {"type": result_type, "target_id": target_id, "alias": alias}

    @staticmethod
    def _normalise_intent_worker_result(
        result: dict[str, Any],
        entities: list[dict[str, Any]],
        user_text: str,
    ) -> dict[str, Any] | None:
        """Validate the intent worker's exact phrase and fixed-script target."""
        if (
            not isinstance(result, dict)
            or set(result) != {"type", "target_id", "phrase"}
            or any(not isinstance(value, str) for value in result.values())
        ):
            return None
        result_type = result["type"]
        target_id = result["target_id"]
        phrase = result["phrase"]
        if result_type == "ignore":
            if target_id or phrase:
                return None
            return {"type": "ignore", "target_id": "", "phrase": ""}
        target = next(
            (item for item in entities if item.get("entity_id") == target_id),
            None,
        )
        if (
            result_type != "custom_phrase"
            or target is None
            or target.get("domain") != "script"
            or phrase != user_text
        ):
            return None
        return {"type": result_type, "target_id": target_id, "phrase": phrase}

    @staticmethod
    def _alias_collision(
        alias: str,
        target_id: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        target_type: str,
    ) -> bool:
        """Reject a proposed alias already owned by another entity or area."""
        alias_key = alias.casefold()
        for item in entities:
            if target_type == "entity_alias" and item["entity_id"] == target_id:
                known_names = [*item.get("names", []), *item.get("aliases", [])]
                if any(
                    isinstance(name, str) and name.casefold() == alias_key
                    for name in known_names
                ):
                    return True
                continue
            if any(
                isinstance(name, str) and name.casefold() == alias_key
                for name in [*item.get("names", []), *item.get("aliases", [])]
            ):
                return True
        for item in areas:
            if target_type == "area_alias" and item["area_id"] == target_id:
                known_names = [item["name"], *item.get("aliases", [])]
                if any(
                    isinstance(name, str) and name.casefold() == alias_key
                    for name in known_names
                ):
                    return True
                continue
            if any(
                isinstance(name, str) and name.casefold() == alias_key
                for name in [item.get("name"), *item.get("aliases", [])]
            ):
                return True
        return False

    @staticmethod
    def _worker_proposal_errors(
        result: dict[str, Any],
        user_text: str,
        entities: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        native_match: dict[str, Any],
    ) -> list[str]:
        """Enforce HA grammar evidence before approving alias learning."""
        alias_slot = _alias_slot(native_match)
        if result["type"] in {"entity_alias", "area_alias"}:
            expected_slot = "name" if result["type"] == "entity_alias" else "area"
            expected_type = (
                "entity_alias"
                if alias_slot and alias_slot[0] == "name"
                else "area_alias"
                if alias_slot and alias_slot[0] == "area"
                else None
            )
            if expected_type != result["type"]:
                return ["Proposal does not match HA's one unmatched name or area slot."]
            if _normalise_text(result["alias"]) != _normalise_text(alias_slot[1]):
                return ["Alias must exactly fill the unmatched slot reported by HA."]
            if _normalise_text(result["alias"]) not in _normalise_text(user_text):
                return ["Alias is not present in the latest utterance."]
            targets = entities if expected_slot == "name" else areas
            target_key = "entity_id" if expected_slot == "name" else "area_id"
            if not any(item.get(target_key) == result["target_id"] for item in targets):
                return ["Alias target is not present in the relevant HA registry snapshot."]
            if SmartHomeTeacherAgent._alias_collision(
                result["alias"],
                result["target_id"],
                entities,
                areas,
                result["type"],
            ):
                return ["Alias already matches another entity or area name."]
            return []
        if alias_slot:
            return ["HA matched a command pattern with an unmatched target slot; use alias analysis."]
        if native_match.get("match") != "no_intent_match":
            return ["HA has an existing or partial intent match; do not shadow it."]
        return []

    @staticmethod
    def _intent_proposal_errors(
        result: dict[str, Any],
        user_text: str,
        native_match: dict[str, Any],
    ) -> list[str]:
        """Prevent new phrases from shadowing an existing native HA intent."""
        if result["type"] == "ignore":
            return []
        if result["phrase"] != user_text:
            return ["A custom intent must use the exact latest utterance."]
        if native_match.get("match") != "no_intent_match":
            return ["HA has an existing or partial intent match."]
        return []

    @staticmethod
    def _normalise_validator_result(
        result: dict[str, Any],
        allowed_target_ids: set[str],
    ) -> dict[str, Any] | None:
        """Validate the validator's bounded accept/retry/reject decision."""
        if (
            not isinstance(result, dict)
            or set(result)
            != {"decision", "reason", "candidate_target_ids", "guidance"}
        ):
            return None
        decision = result.get("decision")
        reason = result.get("reason")
        candidate_target_ids = result.get("candidate_target_ids")
        guidance = result.get("guidance")
        if (
            decision not in {"accept", "retry", "reject"}
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 400
            or not isinstance(candidate_target_ids, list)
            or not all(isinstance(item, str) for item in candidate_target_ids)
            or not isinstance(guidance, str)
            or len(guidance) > 600
        ):
            return None
        if decision == "retry":
            unique_candidate_ids = list(dict.fromkeys(candidate_target_ids))
            if (
                any(item not in allowed_target_ids for item in unique_candidate_ids)
                or not guidance.strip()
            ):
                return None
            return {
                "decision": decision,
                "reason": reason.strip(),
                "candidate_target_ids": unique_candidate_ids,
                "guidance": guidance.strip(),
            }
        if decision == "reject":
            return {
                "decision": decision,
                "reason": reason.strip(),
                "candidate_target_ids": [],
                "guidance": "",
            }
        if candidate_target_ids or guidance:
            return None
        return {
            "decision": decision,
            "reason": reason.strip(),
            "candidate_target_ids": [],
            "guidance": "",
        }

    async def _teach_alias(
        self,
        entity_id: str,
        alias_value: Any,
        user_text: str,
        allowed_entity_ids: set[str],
        native_match: dict[str, Any] | None,
        user_input: ConversationInput,
        language: str,
    ) -> str:
        """Add an alias only when it fills HA's unmatched entity-name slot."""
        alias = _clean_alias(alias_value, user_text)
        if not isinstance(entity_id, str) or not alias:
            return "Alias rejected: invalid entity_id or alias"
        if entity_id not in allowed_entity_ids:
            return "Alias rejected: target was not in the exposed entity list sent to the model"
        baseline = _native_match_evidence(native_match)
        slot = _alias_slot(baseline)
        if (
            slot is None
            or slot[0] != "name"
            or _normalise_text(slot[1]) != _normalise_text(alias)
        ):
            return "Alias rejected: it does not fill HA's unmatched entity-name slot"
        current_match = await self._async_debug_recognize(user_input)
        current_evidence = _native_match_evidence(current_match)
        if current_evidence != baseline:
            return "Alias rejected: Home Assistant's intent match changed during validation"
        state = self.hass.states.get(entity_id)
        if state is None or not async_should_expose(self.hass, "conversation", entity_id):
            return "Alias rejected: entity is missing or no longer exposed to Assist"
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if entry is None:
            return "Alias rejected: entity has no Entity Registry entry"
        aliases = list(entry.aliases)
        alias_key = alias.casefold()
        for other_state in self.hass.states.async_all():
            other_entity_id = other_state.entity_id
            if not async_should_expose(self.hass, "conversation", other_entity_id):
                continue
            other_entry = registry.async_get(other_entity_id)
            other_names = [other_state.attributes.get("friendly_name")]
            if other_entry:
                other_names.extend([
                    getattr(other_entry, "name", None),
                    getattr(other_entry, "original_name", None),
                    *getattr(other_entry, "aliases", []),
                ])
            if any(isinstance(name, str) and name.casefold() == alias_key for name in other_names):
                if other_entity_id == entity_id:
                    return "Alias already exists as an entity name or alias"
                return "Alias rejected: the same name or alias already belongs to another exposed entity"
        for area in ar.async_get(self.hass).async_list_areas():
            if any(name.casefold() == alias_key for name in [area.name, *area.aliases]):
                return "Alias rejected: the same name or alias already belongs to an area"

        registry.async_update_entity(entity_id, aliases=[*aliases, alias])
        try:
            verified = _native_match_evidence(
                await self._async_debug_recognize(user_input)
            )
            if (
                verified.get("match") != "matched"
                or verified.get("intent") != baseline.get("intent")
            ):
                registry.async_update_entity(entity_id, aliases=aliases)
                return "Alias rejected: HA did not resolve the utterance after adding the alias"
        except Exception:
            registry.async_update_entity(entity_id, aliases=aliases)
            raise
        return f"Alias saved: {alias} -> {entity_id}"

    async def _teach_area_alias(
        self,
        area_id: str,
        alias_value: Any,
        user_text: str,
        allowed_area_ids: set[str],
        native_match: dict[str, Any] | None,
        user_input: ConversationInput,
        language: str,
    ) -> str:
        """Add an area alias only when it fills HA's unmatched area slot."""
        alias = _clean_alias(alias_value, user_text)
        baseline = _native_match_evidence(native_match)
        slot = _alias_slot(baseline)
        if (
            not isinstance(area_id, str)
            or area_id not in allowed_area_ids
            or alias is None
            or slot is None
            or slot[0] != "area"
            or _normalise_text(slot[1]) != _normalise_text(alias)
        ):
            return "Area alias rejected: it does not fill HA's unmatched area slot"
        current_match = await self._async_debug_recognize(user_input)
        if _native_match_evidence(current_match) != baseline:
            return "Area alias rejected: Home Assistant's intent match changed during validation"
        registry = ar.async_get(self.hass)
        area = registry.async_get_area(area_id)
        if area is None:
            return "Area alias rejected: target area no longer exists"
        aliases = set(area.aliases)
        alias_key = alias.casefold()
        for other_area in registry.async_list_areas():
            if any(name.casefold() == alias_key for name in [other_area.name, *other_area.aliases]):
                if other_area.id == area_id:
                    return "Area alias already exists"
                return "Area alias rejected: the same name or alias already belongs to another area"
        entity_registry = er.async_get(self.hass)
        for state in self.hass.states.async_all():
            if not async_should_expose(self.hass, "conversation", state.entity_id):
                continue
            entry = entity_registry.async_get(state.entity_id)
            names = [
                state.attributes.get("friendly_name"),
                getattr(entry, "name", None) if entry else None,
                getattr(entry, "original_name", None) if entry else None,
                *(getattr(entry, "aliases", []) if entry else []),
            ]
            if any(isinstance(name, str) and name.casefold() == alias_key for name in names):
                return "Area alias rejected: the same name or alias already belongs to an exposed entity"

        registry.async_update(area_id, aliases=aliases | {alias})
        try:
            verified = _native_match_evidence(
                await self._async_debug_recognize(user_input)
            )
            if (
                verified.get("match") != "matched"
                or verified.get("intent") != baseline.get("intent")
            ):
                registry.async_update(area_id, aliases=aliases)
                return "Area alias rejected: HA did not resolve the utterance after adding the alias"
        except Exception:
            registry.async_update(area_id, aliases=aliases)
            raise
        return f"Alias saved: {alias} -> area {area_id}"

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Enqueue the request and immediately acknowledge it without waiting for Ollama."""
        user_text = user_input.text.strip()
        language = user_input.language or self.hass.config.language
        request_id = None
        if user_text:
            request_id = uuid4().hex[:12]
            job = {
                "request_id": request_id,
                "text": user_text,
                "language": language,
                "chat_messages": self._snapshot_chat_log(chat_log, user_text),
                "assist_context": getattr(user_input, "extra_system_prompt", None) or "",
                "conversation_input": user_input,
            }
            _LOGGER.debug(
                "Learning trace detail stage=assist_input request_id=%s text=%r "
                "language=%s chat_messages=%r assist_context=%r",
                request_id,
                job["text"],
                language,
                job["chat_messages"],
                job["assist_context"],
            )
            self._queue.put_nowait(job)
            _LOGGER.info(
                "Learning trace stage=queued request_id=%s language=%s pending_queue_size=%s",
                request_id,
                language,
                self._queue.qsize(),
            )
        else:
            _LOGGER.info("Empty voice request received; nothing queued")

        response = intent.IntentResponse(language=language)
        response_text = FALLBACK_RESPONSES.get(
            _language_root(language), FALLBACK_RESPONSES["en"]
        )
        response.async_set_speech(response_text)
        if request_id:
            _LOGGER.info(
                "Learning trace stage=assist_response_ready request_id=%s",
                request_id,
            )
            _LOGGER.debug(
                "Learning trace detail stage=assist_response_ready "
                "request_id=%s speech=%r",
                request_id,
                response_text,
            )
        return ConversationResult(response=response, conversation_id=user_input.conversation_id)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the Smart Home Teacher conversation entity."""
    async_add_entities([SmartHomeTeacherAgent(hass, entry)])

