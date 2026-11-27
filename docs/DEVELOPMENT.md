# Development and validation

## Local checks

```bash
bash scripts/validate.sh
```

The script compiles every Python module and parses all JSON files. It does not require HA or Ollama.

## Runtime verification

For a full integration test, use a test Home Assistant instance and a reachable Ollama instance. Validate at least:

1. Integration setup and Conversation entity registration.
2. Immediate acknowledgement while the learning job is queued.
3. FIFO processing when several utterances arrive in sequence.
4. Correct language propagation (Russian, English and Polish).
5. `entity_alias` accepts only a literal phrase fragment and an exposed non-script entity.
6. Duplicate aliases and aliases owned by another exposed entity are rejected.
7. `custom_phrase` accepts only an exposed `script.*` entity.
8. A custom phrase is persisted, registered, and restored after HA restart.
9. A custom phrase does not run the script during learning; it runs only on a later matching Assist turn.
10. Missing or no-longer-exposed scripts are rejected at execution time.
11. `ignore` does not mutate HA state or execute services.

## Logs

The original installer used this diagnostic command:

```bash
docker logs --since 10m homeassistant | grep -i smart_home_teacher
```

Adapt the container name to the local deployment.

## Scope of current automated checks

The repository validation script is static. It does not certify Home Assistant API compatibility for every Core release, Ollama model behavior, or semantic correctness of model-selected entities.
