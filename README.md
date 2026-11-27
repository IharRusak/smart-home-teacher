# Smart Home Teacher

Custom integration for Home Assistant that uses a local Ollama model to learn entity aliases and custom Assist phrases from fallback voice requests.

- **Integration domain:** `smart_home_teacher`
- **Version:** `1.2.1` (extracted from the supplied installer)
- **Python package:** `custom_components.smart_home_teacher`
- **Runtime:** Home Assistant + Ollama API
- **No host-side daemon and no Home Assistant long-lived token required.**

## What it does

1. Home Assistant Assist handles locally recognized commands first when **Prefer handling commands locally** is enabled.
2. Requests that reach this integration are acknowledged immediately and queued in a FIFO background worker.
3. The worker sends the utterance, recent chat context, language and exposed-entity metadata to Ollama.
4. The model may return one of three classifications:
   - `entity_alias` — add a literal fragment from the utterance as an alias to one exposed device entity;
   - `custom_phrase` — save the exact utterance as an inert mapping to one exposed `script.*` entity;
   - `ignore` — make no change.
5. Smart Home Teacher never executes scripts or device actions. Learned phrase mappings are stored for reference only; the integration does not register them as executable Assist sentences.

Entity aliases are stored in Home Assistant's Entity Registry. Learned custom phrase mappings are stored in `/config/smart_home_teacher/custom_actions.json`.

## Installation

### HACS / Git repository

This repository is laid out as a Home Assistant custom integration repository.

1. Add this repository to HACS as a **Custom repository** with category **Integration**.
2. Install **Smart Home Teacher**.
3. Restart Home Assistant.
4. Add the integration configuration to `configuration.yaml`:

   ```yaml
   smart_home_teacher: {}
   ```

5. Restart Home Assistant again if required by your installation.
6. In **Settings → Voice assistants**, open the Assist pipeline and select **Smart Home Teacher** (English UI) or **Учитель умного дома** (Russian UI) as the conversation agent.
7. Keep **Prefer handling commands locally** enabled.
8. Expose only the entities that may be considered for learning. Expose scripts that are valid targets for learned custom phrases.

### Manual installation

Copy the `custom_components/smart_home_teacher` directory into your Home Assistant configuration directory:

```text
/config/custom_components/smart_home_teacher/
```

Then perform steps 3–8 above.

## Ollama connectivity

The integration calls Ollama's `/api/chat` endpoint. The defaults inherited from the supplied installer are:

- URL: `http://127.0.0.1:11434`
- Model: `qwen3:4b-instruct`

These values are currently constants in `custom_components/smart_home_teacher/const.py`, not options exposed in the HA UI. If Home Assistant and Ollama run in separate containers, `127.0.0.1` refers to the Home Assistant container itself; change `OLLAMA_URL` to an address reachable **from the Home Assistant container**. Ensure the configured model exists in Ollama.

## Important safety and limitations

- The model selects a target from the exposed-entity list; the integration validates that the target was in that list.
- Alias text must be a short literal fragment of the latest utterance. Duplicate aliases and aliases already owned by another exposed entity are rejected.
- Alias targets cannot be `script.*`; custom phrase targets must be exposed `script.*` entities.
- The integration does not currently have a semantic confidence threshold or a deterministic device-type compatibility check for an alias. A wrong model-to-entity association is therefore possible even though the alias is a literal fragment. Review the Entity Registry after learning events.
- Smart Home Teacher does not execute scripts, call device services, or register learned phrases as executable intents.
- A learned custom phrase mapping is only a record; it does not make the phrase trigger its mapped script.
- The supplied installer performed host-level cleanup and restart operations. It is intentionally **not included as an auto-run installation path** in this repository; use the standard custom-integration installation above.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/INSTALLATION.md](docs/INSTALLATION.md), and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Repository validation

Run:

```bash
bash scripts/validate.sh
```

This performs Python syntax compilation and JSON parsing without requiring a running Home Assistant instance or Ollama.
