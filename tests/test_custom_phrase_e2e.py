"""End-to-end tests for native custom-phrase learning."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from homeassistant.config_entries import HANDLERS
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.smart_home_teacher import conversation as conversation_agent
from custom_components.smart_home_teacher import custom_actions
from custom_components.smart_home_teacher.const import (
    CUSTOM_ACTIONS_DIRECTORY,
    CUSTOM_SENTENCE_FILENAME,
    DOMAIN,
    EVENT_LEARNING_RESULT,
)
from custom_components.smart_home_teacher.config_flow import (
    SmartHomeTeacherConfigFlow,
)


def test_config_flow_is_registered_for_manifest_domain():
    """The manifest domain resolves to the registered config flow handler."""
    manifest_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "smart_home_teacher"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert DOMAIN == manifest["domain"]
    assert HANDLERS.get(DOMAIN) is SmartHomeTeacherConfigFlow


def _test_agent(hass):
    class TestableAgent(conversation_agent.SmartHomeTeacherAgent):
        async def async_process(self, *args, **kwargs):
            raise NotImplementedError

    agent = TestableAgent(hass, Mock(entry_id="test-entry"))
    agent.hass = hass
    return agent


async def test_user_config_flow_creates_entry(hass, monkeypatch):
    """The Add integration step uses the single-entry setup path."""
    flow = SmartHomeTeacherConfigFlow()
    flow.hass = hass
    expected_result = {"type": "create_entry", "data": {}}
    set_unique_id = AsyncMock()
    abort_if_configured = Mock()
    create_entry = Mock(return_value=expected_result)
    monkeypatch.setattr(flow, "async_set_unique_id", set_unique_id)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", abort_if_configured)
    monkeypatch.setattr(flow, "async_create_entry", create_entry)

    result = await flow.async_step_user()

    assert result is expected_result
    set_unique_id.assert_awaited_once_with(DOMAIN)
    abort_if_configured.assert_called_once_with()
    create_entry.assert_called_once_with(
        title=ANY,
        data={},
    )


@pytest.mark.e2e
async def test_phrase_learning_records_mapping_without_running_script(
    hass, monkeypatch, caplog
):
    """A phrase gets a native fixed-target intent; learning never calls the script."""
    script_entity = "script.boil_kettle"
    Path(hass.config.path("configuration.yaml")).write_text(
        "default_config:\n",
        encoding="utf-8",
    )
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
    activate_intents = AsyncMock()
    reload_sentences = AsyncMock()
    monkeypatch.setattr(custom_actions, "_activate_native_intents", activate_intents)
    monkeypatch.setattr(custom_actions, "_reload_sentences", reload_sentences)
    service_calls = []
    service_registry_type = type(hass.services)

    async def intercept_service_calls(registry, domain, service, *args, **kwargs):
        service_calls.append((domain, service))
        return None

    monkeypatch.setattr(service_registry_type, "async_call", intercept_service_calls)
    caplog.set_level(logging.DEBUG, logger=custom_actions.__name__)

    phrase = "I would like some hot water."
    result = await custom_actions.async_add_custom_phrase(
        hass,
        script_entity,
        phrase,
        "en",
        request_id="trace-test-123",
    )

    assert result.startswith("Custom phrase activated as a Home Assistant native intent:")
    actions_path = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json"))
    payload = json.loads(actions_path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["actions"] == [
        {
            "phrase": "I would like some hot water.",
            "entity_id": script_entity,
            "language": "en",
        }
    ]
    config_text = Path(hass.config.path("configuration.yaml")).read_text(encoding="utf-8")
    assert "intent_script:" in config_text
    assert 'entity_id: "script.boil_kettle"' in config_text
    sentence_path = Path(
        hass.config.path("custom_sentences", "en", CUSTOM_SENTENCE_FILENAME)
    )
    assert '"I would like some hot water."' in sentence_path.read_text(encoding="utf-8")
    assert hass.states.get(script_entity).state == "off"
    assert service_calls == []
    activate_intents.assert_awaited_once()
    reload_sentences.assert_awaited_once_with(hass, {"en"})
    integration_records = [
        record
        for record in caplog.records
        if record.name == custom_actions.__name__
    ]
    normal_logs = "\n".join(
        record.getMessage()
        for record in integration_records
        if record.levelno >= logging.INFO
    )
    debug_logs = "\n".join(
        record.getMessage()
        for record in integration_records
        if record.levelno == logging.DEBUG
    )
    assert "trace-test-123" in normal_logs
    assert phrase not in normal_logs
    assert script_entity not in normal_logs
    assert phrase in debug_logs
    assert script_entity in debug_logs


@pytest.mark.e2e
async def test_cleanup_service_removes_only_matching_custom_phrases(
    hass, monkeypatch
):
    """Cleanup removes the selected phrase and its generated native artifacts."""
    Path(hass.config.path("configuration.yaml")).write_text(
        "default_config:\n",
        encoding="utf-8",
    )
    records = [
        {
            "phrase": "хочу кипятку",
            "entity_id": "script.chajnik_kipiachenie",
            "language": "ru",
        },
        {
            "phrase": "вскипяти воду",
            "entity_id": "script.other_kettle",
            "language": "ru",
        },
    ]
    custom_actions._sync_native_artifacts_sync(hass, records)
    activate_intents = AsyncMock()
    reload_sentences = AsyncMock()
    monkeypatch.setattr(
        custom_actions, "_activate_native_intents", activate_intents
    )
    monkeypatch.setattr(custom_actions, "_reload_sentences", reload_sentences)

    await custom_actions.async_cleanup_learning_result(
        hass,
        {
            "result_type": "custom_phrase",
            "target_id": records[0]["entity_id"],
            "value": records[0]["phrase"],
            "language": records[0]["language"],
        },
        "cleanup-first",
    )

    actions_path = Path(
        hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json")
    )
    sentence_path = Path(
        hass.config.path("custom_sentences", "ru", CUSTOM_SENTENCE_FILENAME)
    )
    remaining = json.loads(actions_path.read_text(encoding="utf-8"))["actions"]
    assert remaining == [records[1]]
    assert records[1]["phrase"] in sentence_path.read_text(encoding="utf-8")
    assert records[0]["phrase"] not in sentence_path.read_text(encoding="utf-8")
    assert records[0]["entity_id"] not in Path(
        hass.config.path("configuration.yaml")
    ).read_text(encoding="utf-8")

    await custom_actions.async_cleanup_learning_result(
        hass,
        {
            "result_type": "custom_phrase",
            "target_id": records[1]["entity_id"],
            "value": records[1]["phrase"],
            "language": records[1]["language"],
        },
        "cleanup-second",
    )

    assert not actions_path.exists()
    assert not sentence_path.exists()
    assert "SMART_HOME_TEACHER" not in Path(
        hass.config.path("configuration.yaml")
    ).read_text(encoding="utf-8")
    assert activate_intents.await_count == 2
    assert [call.args for call in reload_sentences.await_args_list] == [
        (hass, {"ru"}),
        (hass, {"ru"}),
    ]


@pytest.mark.e2e
async def test_cleanup_service_removes_only_the_recorded_entity_alias(hass):
    """Cleanup leaves other aliases on the same entity untouched."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "script",
        "test",
        "boil_kettle",
        suggested_object_id="boil_kettle",
    )
    registry.async_update_entity(
        entry.entity_id,
        aliases=["test-only alias", "keep this alias"],
    )

    await custom_actions.async_cleanup_learning_result(
        hass,
        {
            "result_type": "entity_alias",
            "target_id": entry.entity_id,
            "value": "test-only alias",
            "language": "ru",
        },
        "cleanup-alias",
    )

    assert registry.async_get(entry.entity_id).aliases == ["keep this alias"]


@pytest.mark.e2e
async def test_cleanup_service_removes_only_the_recorded_area_alias(hass):
    """Cleanup leaves other aliases on the same area untouched."""
    registry = ar.async_get(hass)
    area = registry.async_create("Kitchen")
    registry.async_update(
        area.id,
        aliases={"test-only area alias", "keep this area alias"},
    )

    await custom_actions.async_cleanup_learning_result(
        hass,
        {
            "result_type": "area_alias",
            "target_id": area.id,
            "value": "test-only area alias",
            "language": "ru",
        },
        "cleanup-area-alias",
    )

    assert set(registry.async_get_area(area.id).aliases) == {
        "keep this area alias"
    }


@pytest.mark.e2e
async def test_saved_learning_result_emits_cleanup_event(hass, monkeypatch):
    """A cleanup automation receives only successfully persisted results."""
    agent = _test_agent(hass)
    entity_id = "script.boil_kettle"
    result = {
        "type": "custom_phrase",
        "target_id": entity_id,
        "phrase": "хочу кипятку",
    }
    monkeypatch.setattr(
        agent, "_async_debug_recognize", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        agent,
        "_get_exposed_entities",
        lambda _query: [{"entity_id": entity_id}],
    )
    monkeypatch.setattr(agent, "_get_assist_areas", lambda: [])
    monkeypatch.setattr(
        agent, "_orchestrate_classification", AsyncMock(return_value=result)
    )
    add_phrase = AsyncMock(
        return_value=(
            "Custom phrase activated as a Home Assistant native intent: "
            f"{result['phrase']} -> {entity_id}"
        )
    )
    monkeypatch.setattr(conversation_agent, "async_add_custom_phrase", add_phrase)
    received_events = []
    hass.bus.async_listen(
        EVENT_LEARNING_RESULT,
        lambda event: received_events.append(event.data),
    )

    await agent._process_queued_message(
        {
            "request_id": "cleanup-event-test",
            "text": result["phrase"],
            "language": "ru",
            "chat_messages": [],
            "assist_context": "",
            "conversation_input": Mock(),
        }
    )
    await hass.async_block_till_done()

    assert received_events == [
        {
            "request_id": "cleanup-event-test",
            "result_type": "custom_phrase",
            "target_id": entity_id,
            "value": result["phrase"],
            "language": "ru",
        }
    ]
    add_phrase.assert_awaited_once()


def test_native_intent_config_preserves_existing_mapping_and_is_idempotent():
    """The managed intent block is inserted without replacing user mappings."""
    config = (
        "default_config:\n"
        "intent_script:\n"
        "    ExistingIntent:\n"
        "      speech:\n"
        "        text: Existing response\n"
        "logger:\n"
        "  default: warning\n"
    )
    records = [
        {
            "phrase": "вскипяти воды",
            "entity_id": "script.boil_kettle",
            "language": "ru",
        }
    ]

    merged = custom_actions._merge_intent_script_config(
        config,
        custom_actions._intent_script_entries(records),
    )

    assert "ExistingIntent:" in merged
    assert "default: warning" in merged
    assert 'entity_id: "script.boil_kettle"' in merged
    intent_type = custom_actions._intent_type(records[0])
    assert f"\n    {intent_type}:\n      action:" in merged
    assert custom_actions._merge_intent_script_config(
        merged,
        custom_actions._intent_script_entries(records),
    ) == merged


def test_native_intent_config_rejects_include_section():
    """Unsupported include-style intent_script configuration is left untouched."""
    with pytest.raises(custom_actions.NativeIntentConfigError):
        custom_actions._merge_intent_script_config(
            "intent_script: !include intent_scripts.yaml\n",
            custom_actions._intent_script_entries(
                [
                    {
                        "phrase": "вскипяти воды",
                        "entity_id": "script.boil_kettle",
                        "language": "ru",
                    }
                ]
            ),
        )


@pytest.mark.e2e
async def test_script_alias_is_saved_without_running_script(hass, monkeypatch):
    """An exposed script can receive an alias without running the script."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "script",
        "test",
        "boil_kettle",
        suggested_object_id="boil_kettle",
    )
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": "Boil kettle"})
    monkeypatch.setattr(
        conversation_agent,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: True,
    )
    service_calls = []
    service_registry_type = type(hass.services)

    async def intercept_service_calls(registry, domain, service, *args, **kwargs):
        service_calls.append((domain, service))
        return None

    monkeypatch.setattr(service_registry_type, "async_call", intercept_service_calls)

    agent = _test_agent(hass)
    native_match = {
        "match": False,
        "intent": {"name": "HassTurnOn"},
        "unmatched_slots": {"name": "кипятилку"},
    }
    agent._async_debug_recognize = AsyncMock(
        side_effect=[
            native_match,
            {
                "match": True,
                "intent": {"name": "HassTurnOn"},
                "unmatched_slots": {},
            },
        ]
    )
    result = await agent._teach_alias(
        entry.entity_id,
        "кипятилку",
        "Включи кипятилку.",
        {entry.entity_id},
        native_match,
        Mock(),
        "ru",
    )

    assert result == f"Alias saved: кипятилку -> {entry.entity_id}"
    assert "кипятилку" in registry.async_get(entry.entity_id).aliases
    assert hass.states.get(entry.entity_id).state == "off"
    assert service_calls == []
    assert agent._async_debug_recognize.await_count == 2


@pytest.mark.parametrize(
    ("language", "localized_intro"),
    [
        ("en-US", "You are the ALIAS_WORKER"),
        ("ru-RU", "Ты — ALIAS_WORKER"),
        ("pl", "You are the ALIAS_WORKER"),
    ],
)
def test_worker_prompt_uses_localized_template_and_english_fallback(
    hass, language, localized_intro
):
    """Use GUI-supported worker prompts and English fallback for other locales."""
    prompt = _test_agent(hass)._system_prompt(language, "alias_worker")

    assert prompt.startswith(localized_intro)
    assert json.dumps(language, ensure_ascii=False) in prompt
    assert "untrusted data" in prompt or "недоверенными данными" in prompt
    assert "standard_action_verbs" not in prompt


def test_custom_phrase_requires_exact_text_and_empty_alias(hass):
    """Custom phrases preserve punctuation and require an exposed script target."""
    entity = _entity("script.boil_kettle", "Boil kettle")
    result = {
        "type": "custom_phrase",
        "target_id": "script.boil_kettle",
        "phrase": "вскипяти воды",
    }

    assert (
        conversation_agent.SmartHomeTeacherAgent._normalise_intent_worker_result(
            result,
            [entity],
            "вскипяти воды.",
        )
        is None
    )
    result["phrase"] = "вскипяти воды."
    assert conversation_agent.SmartHomeTeacherAgent._normalise_intent_worker_result(
        result,
        [entity],
        "вскипяти воды.",
    ) == {
        "type": "custom_phrase",
        "target_id": "script.boil_kettle",
        "phrase": "вскипяти воды.",
    }
    result["target_id"] = "light.kettle"
    assert (
        conversation_agent.SmartHomeTeacherAgent._normalise_intent_worker_result(
            result,
            [entity, _entity("light.kettle", "Kettle light")],
            "вскипяти воды.",
        )
        is None
    )


@pytest.mark.parametrize(
    ("language", "localized_intro"),
    [
        ("en-US", "You are the independent VALIDATOR"),
        ("ru-RU", "Ты — независимый VALIDATOR"),
        ("pl", "You are the independent VALIDATOR"),
    ],
)
def test_validator_prompt_uses_localized_template_and_english_fallback(
    hass, language, localized_intro
):
    """Validator role has a localized prompt and fail-closed decision rules."""
    prompt = _test_agent(hass)._system_prompt(language, "validator")

    assert prompt.startswith(localized_intro)
    assert json.dumps(language, ensure_ascii=False) in prompt
    assert "deterministic_validation_errors" in prompt
    assert "retry" in prompt


@pytest.mark.parametrize(
    ("language", "localized_intro"),
    [
        ("en-US", "You are the ORCHESTRATOR"),
        ("ru-RU", "Ты — ORCHESTRATOR"),
        ("pl", "You are the ORCHESTRATOR"),
    ],
)
def test_orchestrator_prompt_uses_localized_template_and_english_fallback(
    hass, language, localized_intro
):
    """Orchestrator role has a localized, bounded-brief prompt."""
    prompt = _test_agent(hass)._system_prompt(language, "orchestrator")

    assert prompt.startswith(localized_intro)
    assert json.dumps(language, ensure_ascii=False) in prompt
    assert "worker_brief" in prompt
    assert (
        "select a final target" in prompt
        or "не выбирай окончательную цель" in prompt
    )


def _entity(entity_id, name):
    return {
        "entity_id": entity_id,
        "domain": entity_id.split(".", 1)[0],
        "names": [name],
        "aliases": [],
        "area_id": None,
    }


def _worker_alias(entity_id, alias="kettle"):
    return {
        "type": "entity_alias",
        "target_id": entity_id,
        "alias": alias,
    }


def _orchestrator_result(focus_target_ids=None):
    return {
        "focus_target_ids": focus_target_ids or [],
        "worker_brief": "Compare the request only with exposed entity names and aliases.",
    }


def _validator_result(decision, candidate_target_ids=None, guidance=""):
    return {
        "decision": decision,
        "reason": "The proposal matches the supplied request and environment.",
        "candidate_target_ids": candidate_target_ids or [],
        "guidance": guidance,
    }


async def test_orchestrator_retries_worker_with_validator_ambiguity(
    hass, monkeypatch
):
    """Validator retry feedback stays in alias analysis and is bounded."""
    agent = _test_agent(hass)
    entities = [
        _entity("light.kettle", "Kitchen kettle light"),
        _entity("switch.kettle", "Kitchen kettle switch"),
    ]
    worker = AsyncMock(
        side_effect=[
            _worker_alias("light.kettle"),
            _worker_alias("switch.kettle"),
        ]
    )
    validator = AsyncMock(
        side_effect=[
            _validator_result(
                "retry",
                ["light.kettle", "switch.kettle"],
                "The utterance refers to the kettle; distinguish light from switch.",
            ),
            _validator_result("accept"),
        ]
    )
    orchestrator = AsyncMock(
        return_value=_orchestrator_result(
            ["light.kettle", "switch.kettle"]
        )
    )
    monkeypatch.setattr(agent, "_ollama_orchestrator", orchestrator)
    monkeypatch.setattr(agent, "_ollama_alias_worker", worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    native_match = {
        "match": "partial",
        "intent": "HassTurnOn",
        "unmatched_slots": {"name": "kettle"},
    }
    result = await agent._orchestrate_classification(
        "turn on kettle",
        entities,
        [],
        [{"role": "user", "content": "turn on kettle"}],
        "en",
        "",
        native_match,
        "orchestration-test",
    )

    assert result == _worker_alias("switch.kettle")
    assert worker.await_count == 2
    assert validator.await_count == 2
    orchestrator.assert_awaited_once()
    feedback = worker.await_args_list[1].args[8]
    assert feedback["candidate_targets"] == entities
    assert "distinguish light from switch" in feedback["guidance"]
    assert worker.await_args_list[0].args[10]["focus_target_ids"] == [
        "light.kettle",
        "switch.kettle",
    ]
    assert (
        validator.await_args_list[0].args[11]["worker_brief"]
        == worker.await_args_list[0].args[10]["worker_brief"]
    )


async def test_orchestrator_rejects_repeated_ambiguity(hass, monkeypatch):
    """A second ambiguous validation is terminal and never reaches persistence."""
    agent = _test_agent(hass)
    entities = [
        _entity("light.kettle", "Kitchen kettle light"),
        _entity("switch.kettle", "Kitchen kettle switch"),
    ]
    worker = AsyncMock(
        side_effect=[
            _worker_alias("light.kettle"),
            _worker_alias("switch.kettle"),
        ]
    )
    validator = AsyncMock(
        side_effect=[
            _validator_result(
                "retry",
                ["light.kettle", "switch.kettle"],
                "The target is still unclear.",
            ),
            _validator_result(
                "retry",
                ["light.kettle", "switch.kettle"],
                "The target remains unclear.",
            ),
        ]
    )
    orchestrator = AsyncMock(
        return_value=_orchestrator_result(["light.kettle", "switch.kettle"])
    )
    monkeypatch.setattr(agent, "_ollama_orchestrator", orchestrator)
    monkeypatch.setattr(agent, "_ollama_alias_worker", worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        "turn on kettle",
        entities,
        [],
        [{"role": "user", "content": "turn on kettle"}],
        "en",
        "",
        {
            "match": "partial",
            "intent": "HassTurnOn",
            "unmatched_slots": {"name": "kettle"},
        },
        "repeated-ambiguity-test",
    )

    assert result is None
    assert worker.await_count == 2
    assert validator.await_count == 2
    orchestrator.assert_awaited_once()


async def test_orchestrator_rejects_validator_rejection_without_retry(
    hass, monkeypatch
):
    """A validator rejection is terminal even when the worker output is well-formed."""
    agent = _test_agent(hass)
    entities = [_entity("switch.kettle", "Kitchen kettle switch")]
    worker = AsyncMock(return_value=_worker_alias("switch.kettle"))
    validator = AsyncMock(
        return_value={
            **_validator_result("reject"),
            "reason": "The entity does not match the user's intended target.",
        }
    )
    orchestrator = AsyncMock(
        return_value=_orchestrator_result(["switch.kettle"])
    )
    monkeypatch.setattr(agent, "_ollama_orchestrator", orchestrator)
    monkeypatch.setattr(agent, "_ollama_alias_worker", worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        "turn on kettle",
        entities,
        [],
        [{"role": "user", "content": "turn on kettle"}],
        "en",
        "",
        {
            "match": "partial",
            "intent": "HassTurnOn",
            "unmatched_slots": {"name": "kettle"},
        },
        "validator-reject-test",
    )

    assert result is None
    worker.assert_awaited_once()
    validator.assert_awaited_once()
    orchestrator.assert_awaited_once()


async def test_orchestrator_rejects_worker_target_outside_environment(
    hass, monkeypatch
):
    """A target absent from exposed entities is rejected before validator or persistence."""
    agent = _test_agent(hass)
    orchestrator = AsyncMock(
        return_value=_orchestrator_result(["switch.kettle"])
    )
    worker = AsyncMock(return_value=_worker_alias("light.kettle"))
    validator = AsyncMock()
    monkeypatch.setattr(agent, "_ollama_orchestrator", orchestrator)
    monkeypatch.setattr(agent, "_ollama_alias_worker", worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        "turn on kettle",
        [_entity("switch.kettle", "Kitchen kettle switch")],
        [],
        [{"role": "user", "content": "turn on kettle"}],
        "en",
        "",
        {
            "match": "partial",
            "intent": "HassTurnOn",
            "unmatched_slots": {"name": "kettle"},
        },
        "outside-environment-test",
    )

    assert result is None
    orchestrator.assert_awaited_once()
    worker.assert_awaited_once()
    validator.assert_not_awaited()


async def test_orchestrator_rejects_focus_ids_outside_environment(
    hass, monkeypatch
):
    """Orchestrator hints cannot introduce entities absent from the HA snapshot."""
    agent = _test_agent(hass)
    orchestrator = AsyncMock(
        return_value=_orchestrator_result(["script.not_exposed"])
    )
    worker = AsyncMock()
    validator = AsyncMock()
    monkeypatch.setattr(agent, "_ollama_orchestrator", orchestrator)
    monkeypatch.setattr(agent, "_ollama_alias_worker", worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        "turn on kettle",
        [_entity("switch.kettle", "Kitchen kettle switch")],
        [],
        [{"role": "user", "content": "turn on kettle"}],
        "en",
        "",
        {"match": "no_intent_match"},
        "invalid-orchestrator-focus-test",
    )

    assert result is None
    orchestrator.assert_awaited_once()
    worker.assert_not_awaited()
    validator.assert_not_awaited()


async def test_no_intent_match_routes_exact_request_to_script_intent(
    hass, monkeypatch
):
    """A non-alias Russian request can become an exact native script intent."""
    agent = _test_agent(hass)
    user_text = "хочу кипятку"
    script_id = "script.chajnik_kipiachenie"
    entities = [_entity(script_id, "Чайник кипячение")]
    alias_worker = AsyncMock(
        return_value={"type": "not_alias", "target_id": "", "alias": ""}
    )
    intent_worker = AsyncMock(
        return_value={
            "type": "custom_phrase",
            "target_id": script_id,
            "phrase": user_text,
        }
    )
    validator = AsyncMock(
        side_effect=[_validator_result("accept"), _validator_result("accept")]
    )
    monkeypatch.setattr(
        agent, "_ollama_orchestrator", AsyncMock(return_value=_orchestrator_result())
    )
    monkeypatch.setattr(agent, "_ollama_alias_worker", alias_worker)
    monkeypatch.setattr(agent, "_ollama_intent_worker", intent_worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        user_text,
        entities,
        [],
        [],
        "ru",
        "",
        {"match": "no_intent_match"},
        "hot-water-intent-test",
    )

    assert result == {
        "type": "custom_phrase",
        "target_id": script_id,
        "phrase": user_text,
    }
    alias_worker.assert_awaited_once()
    intent_worker.assert_awaited_once()
    assert [call.args[0] for call in validator.await_args_list] == [
        "alias",
        "intent",
    ]


async def test_rejected_alias_candidate_falls_through_to_intent_worker(
    hass, monkeypatch
):
    """A rejected alias proposal on a no-match request can enter intent validation."""
    agent = _test_agent(hass)
    user_text = "хочу кипятку"
    script_id = "script.chajnik_kipiachenie"
    entities = [_entity(script_id, "Чайник кипячение")]
    alias_worker = AsyncMock(
        return_value={
            "type": "entity_alias",
            "target_id": script_id,
            "alias": "кипятку",
        }
    )
    intent_worker = AsyncMock(
        return_value={
            "type": "custom_phrase",
            "target_id": script_id,
            "phrase": user_text,
        }
    )
    rejected_alias = {
        **_validator_result("reject"),
        "candidate_target_ids": ["script.chaj_001"],
    }
    validator = AsyncMock(
        side_effect=[rejected_alias, _validator_result("accept")]
    )
    monkeypatch.setattr(
        agent, "_ollama_orchestrator", AsyncMock(return_value=_orchestrator_result())
    )
    monkeypatch.setattr(agent, "_ollama_alias_worker", alias_worker)
    monkeypatch.setattr(agent, "_ollama_intent_worker", intent_worker)
    monkeypatch.setattr(agent, "_ollama_validator", validator)

    result = await agent._orchestrate_classification(
        user_text,
        entities,
        [],
        [],
        "ru",
        "",
        {"match": "no_intent_match"},
        "rejected-alias-falls-through-test",
    )

    assert result == {
        "type": "custom_phrase",
        "target_id": script_id,
        "phrase": user_text,
    }
    alias_worker.assert_awaited_once()
    intent_worker.assert_awaited_once()
    assert [call.args[0] for call in validator.await_args_list] == [
        "alias",
        "intent",
    ]


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
    Path(hass.config.path("configuration.yaml")).write_text(
        "default_config:\n",
        encoding="utf-8",
    )
    first_script = "script.boil_kettle"
    second_script = "script.make_tea"
    hass.states.async_set(first_script, "off")
    hass.states.async_set(second_script, "off")
    monkeypatch.setattr(
        custom_actions,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: True,
    )
    monkeypatch.setattr(
        custom_actions,
        "_activate_native_intents",
        AsyncMock(),
    )
    monkeypatch.setattr(
        custom_actions,
        "_reload_sentences",
        AsyncMock(),
    )

    phrase = "I would like some hot water"
    first_result = await custom_actions.async_add_custom_phrase(
        hass, first_script, phrase, "en"
    )
    assert "activated as a Home Assistant native intent" in first_result
    assert "already recorded" in await custom_actions.async_add_custom_phrase(
        hass, first_script, phrase.upper(), "en"
    )
    remap_result = await custom_actions.async_add_custom_phrase(
        hass, second_script, phrase, "en"
    )
    assert "already mapped to another script" in remap_result

    actions_path = Path(hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json"))
    payload = json.loads(actions_path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["actions"] == [
        {
            "phrase": phrase,
            "entity_id": first_script,
            "language": "en",
        }
    ]


@pytest.mark.e2e
async def test_phrase_learning_does_not_overwrite_unmanaged_sentence_file(
    hass, monkeypatch
):
    """A user-authored file at the generated path is preserved on conflict."""
    Path(hass.config.path("configuration.yaml")).write_text(
        "default_config:\n",
        encoding="utf-8",
    )
    script_entity = "script.boil_kettle"
    hass.states.async_set(script_entity, "off")
    monkeypatch.setattr(
        custom_actions,
        "async_should_expose",
        lambda _hass, _domain, _entity_id: True,
    )
    sentence_path = Path(
        hass.config.path("custom_sentences", "en", CUSTOM_SENTENCE_FILENAME)
    )
    sentence_path.parent.mkdir(parents=True, exist_ok=True)
    sentence_path.write_text("language: en\nintents: {}\n", encoding="utf-8")

    result = await custom_actions.async_add_custom_phrase(
        hass,
        script_entity,
        "I would like some hot water",
        "en",
    )

    assert "Refusing to overwrite an unmanaged sentence file" in result
    assert sentence_path.read_text(encoding="utf-8") == "language: en\nintents: {}\n"
    assert not Path(
        hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json")
    ).exists()


@pytest.mark.e2e
async def test_setup_restores_native_intents_without_touching_user_sentences(
    hass, monkeypatch
):
    """Setup rebuilds managed artifacts and preserves user-authored sentence files."""
    Path(hass.config.path("configuration.yaml")).write_text(
        "default_config:\n",
        encoding="utf-8",
    )
    actions_path = Path(
        hass.config.path(CUSTOM_ACTIONS_DIRECTORY, "custom_actions.json")
    )
    actions_path.parent.mkdir(parents=True, exist_ok=True)
    actions_path.write_text(
        json.dumps(
            {
                "version": 2,
                "actions": [
                    {
                        "phrase": "вскипяти воды",
                        "entity_id": "script.boil_kettle",
                        "language": "ru",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    language_dir = Path(hass.config.path("custom_sentences", "en"))
    language_dir.mkdir(parents=True, exist_ok=True)
    user_file = language_dir / "user_sentences.yaml"
    user_file.write_text("user intent", encoding="utf-8")
    activate_intents = AsyncMock()
    reload_sentences = AsyncMock()
    monkeypatch.setattr(custom_actions, "_activate_native_intents", activate_intents)
    monkeypatch.setattr(custom_actions, "_reload_sentences", reload_sentences)

    await custom_actions.async_setup_custom_actions(hass, None)

    assert user_file.read_text(encoding="utf-8") == "user intent"
    assert Path(
        hass.config.path("custom_sentences", "ru", CUSTOM_SENTENCE_FILENAME)
    ).is_file()
    config_text = Path(hass.config.path("configuration.yaml")).read_text(
        encoding="utf-8"
    )
    assert 'entity_id: "script.boil_kettle"' in config_text
    assert "вскипяти воды" in Path(
        hass.config.path("custom_sentences", "ru", CUSTOM_SENTENCE_FILENAME)
    ).read_text(encoding="utf-8")
    activate_intents.assert_awaited_once()
    reload_sentences.assert_awaited_once_with(hass, {"ru"})
