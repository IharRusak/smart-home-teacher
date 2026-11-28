# Development and validation

## Local checks

```bash
bash scripts/validate.sh
```

The script compiles every Python module and parses all JSON files. It does not require HA or Ollama.

## End-to-end tests

Install the test dependencies and run the Home Assistant integration tests:

```bash
python -m pip install -r requirements_test.txt
python -m pytest -q
```

The E2E tests use a Home Assistant test instance. They verify fixed-target native intent and sentence generation while intercepting service calls so no script can run during learning.

## Runtime verification

For a full integration test, use a test Home Assistant instance and a reachable Ollama instance. Validate at least:

1. Integration setup and Conversation entity registration.
2. Immediate acknowledgement while the learning job is queued.
3. FIFO processing when several utterances arrive in sequence.
4. Correct language propagation (Russian, English and Polish).
5. An existing native intent match is not learned again.
6. Alias proposals must exactly fill one unmatched HA `name` or `area` slot.
7. Duplicate aliases and aliases owned by another exposed entity or area are rejected.
8. A failed post-write grammar probe rolls back an entity or area alias.
9. `custom_phrase` accepts only an exact latest utterance and one exposed `script.*` when HA reports no intent match.
10. A custom phrase generates a no-slot sentence and a fixed `script.turn_on` target in the managed `intent_script` configuration.
11. Learning and native intent reload never call the target script or any device service.
12. Existing mapping-style `intent_script` entries and user-authored sentence files are preserved; unsupported includes fail explicitly.
13. `ignore` does not mutate HA state or execute device services.

## Logs

The four localized prompt roles use the `prompts/{role}_{language}.txt` naming
pattern, including `prompts/orchestrator_en.txt`,
`prompts/alias_worker_en.txt`, `prompts/intent_worker_en.txt`, and
`prompts/validator_en.txt`. Russian equivalents are provided. Other language
tags use the English prompt while retaining the request language.

The Python controller invokes the orchestrator, `alias_worker`, validator and,
when appropriate, `intent_worker` as separate Ollama chat roles using the
configured `qwen3:4b-instruct` model. Alias analysis runs before intent design;
the validator checks each stage against the complete snapshot and grammar
probe. Each role uses an Ollama JSON schema, temperature 0, and `think: false`.
A validator rejection stops the request. Feedback allows at most one retry per
stage. Alias learning normally uses three model calls; custom-intent learning
normally uses five. A retry adds two calls.

The original installer used this diagnostic command:

```bash
docker logs --since 10m homeassistant | grep -i smart_home_teacher
```

Adapt the container name to the local deployment.

To see per-request processing stages, enable info logs for the conversation and
phrase-storage modules in `configuration.yaml` (merge with any existing
`logger:` section):

```yaml
logger:
  logs:
    custom_components.smart_home_teacher.conversation: info
    custom_components.smart_home_teacher.custom_actions: info
```

Each query gets a `request_id` shared by queueing, candidate discovery,
orchestrator, `alias_worker`, `intent_worker`, validator, controller, and
persistence logs. At INFO level, these markers omit utterance text, prompts,
entity IDs, and learned phrases/aliases.

For an explicitly enabled debug session, change those module levels to `debug`.
Debug records include the complete Assist input, recent chat/context, exposed
candidate details, each role's Ollama request payload and response, validator
feedback on ambiguity, and the final validation outcome.
This can contain private conversation text and entity names; keep the logs local
and return the levels to `info` when finished.

## Scope of current automated checks

The repository validation script is static. It does not certify Home Assistant API compatibility for every Core release, Ollama model behavior, or semantic correctness of model-selected entities. Automated orchestration tests cover validator rejection and bounded ambiguity retry; they do not prove semantic correctness for every request.
