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

The E2E tests use a real Home Assistant test instance. They verify phrase validation and persistence while intercepting script service calls so no script can run.

## Runtime verification

For a full integration test, use a test Home Assistant instance and a reachable Ollama instance. Validate at least:

1. Integration setup and Conversation entity registration.
2. Immediate acknowledgement while the learning job is queued.
3. FIFO processing when several utterances arrive in sequence.
4. Correct language propagation (Russian, English and Polish).
5. `entity_alias` accepts only a literal phrase fragment and an exposed non-script entity.
6. Duplicate aliases and aliases owned by another exposed entity are rejected.
7. `custom_phrase` accepts only an exposed `script.*` entity.
8. A custom phrase mapping is persisted and restored after HA restart, but is not registered as an Assist intent.
9. Learning a custom phrase never registers or runs a script; no device service is called by the integration.
10. Legacy generated custom-sentence files and handlers are removed during setup/unload.
11. `ignore` does not mutate HA state or execute services.

## Logs

The original installer used this diagnostic command:

```bash
docker logs --since 10m homeassistant | grep -i smart_home_teacher
```

Adapt the container name to the local deployment.

## Scope of current automated checks

The repository validation script is static. It does not certify Home Assistant API compatibility for every Core release, Ollama model behavior, or semantic correctness of model-selected entities.
