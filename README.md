# Smart Home Teacher

Custom integration for Home Assistant that uses a local Ollama model to learn entity aliases and custom Assist phrases from fallback voice requests.

- **Integration domain:** `smart_home_teacher`
- **Version:** `1.2.2`
- **Python package:** `custom_components.smart_home_teacher`
- **Runtime:** Home Assistant + Ollama API
- **No host-side daemon and no Home Assistant long-lived token required.**

## What it does

1. Home Assistant Assist handles locally recognized commands first when **Prefer handling commands locally** is enabled.
2. Requests that reach this integration are acknowledged immediately and queued in a FIFO background worker.
3. A non-executing Home Assistant grammar probe checks native coverage:
   - an existing match is left to Home Assistant;
   - one unmatched `name` or `area` slot may lead to an alias proposal;
   - no intent match may lead to new-intent investigation.
4. The orchestrator prepares a short factual brief. The `alias_worker` investigates only exact unmatched entity/area names, and a separate validator checks each result:
   - a consistent result is accepted;
   - an incorrect or invalid result is rejected without persistence;
   - feedback may trigger one retry in the same branch.
5. Only a validated `not_alias` result with no native intent match reaches `intent_worker`. It may propose the exact latest utterance as a no-slot intent bound to one clearly supported exposed script. The validator checks it independently.
6. Smart Home Teacher never calls a script or device service. Learning a custom phrase writes a fixed-target `intent_script` entry and custom sentence; Home Assistant may run that script when Assist later recognizes the phrase.

Orchestrator, `alias_worker`, `intent_worker`, and validator are separate model roles coordinated by deterministic Python orchestration. All use the configured Ollama model (`qwen3:4b-instruct` by default); each call is constrained to a JSON schema, uses temperature 0, and disables Qwen3 thinking output. Alias learning normally uses three model calls; intent learning normally uses five. A single retry adds two calls.

Entity aliases are stored in Home Assistant's Entity Registry. Learned custom phrase mappings are stored in `/config/smart_home_teacher/custom_actions.json`.

Alias learning is allowed only when Home Assistant's native grammar probe reports exactly one unmatched `name` or `area` slot. The alias must exactly match that slot; standard verbs and other command text are not inferred or stripped.
The native intent generator adds a managed block to `configuration.yaml`.
Existing mapping-style `intent_script` entries are preserved; an unsupported
include-style section is left untouched and the phrase is rejected with an
explicit error.

When a learning result is saved, the integration fires the
`smart_home_teacher_learning_result` event with the request ID, result type,
target, value, and language. The `smart_home_teacher.cleanup_learning_result`
service removes exactly that alias or phrase and refreshes Home Assistant's
native intent data; it never runs the target script. An automation can filter
the event to a test phrase and call this service to clean up after QA.

```yaml
alias: Clean up a test learning result
trigger:
  - platform: event
    event_type: smart_home_teacher_learning_result
condition:
  - condition: template
    value_template: >-
      {{ trigger.event.data.value == 'test phrase'
         and trigger.event.data.target_id == 'script.example' }}
action:
  - service: smart_home_teacher.cleanup_learning_result
    data:
      request_id: "{{ trigger.event.data.request_id }}"
      result_type: "{{ trigger.event.data.result_type }}"
      target_id: "{{ trigger.event.data.target_id }}"
      value: "{{ trigger.event.data.value }}"
      language: "{{ trigger.event.data.language }}"
mode: single
```

Replace the example phrase and entity ID with the exact values to clean; remove
the temporary automation after the test.

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
- A validator checks semantic consistency against the same exposed-entity snapshot, but this model-based check is not a formal guarantee. Ambiguity is retried once; unresolved ambiguity fails closed without saving.
- Alias text must be a short literal fragment of the latest utterance. Duplicate aliases and aliases already owned by another exposed entity are rejected.
- Alias targets may include exposed `script.*` entities; custom phrase targets must also be exposed `script.*` entities.
- The validator does not receive live device state and cannot prove that a semantically plausible mapping is correct. Review the Entity Registry after learning events.
- Smart Home Teacher does not call device services. It generates native Home Assistant custom sentences and fixed-target `intent_script` mappings; Home Assistant may execute the configured script on a future matching request.
- Native phrase activation modifies `configuration.yaml` and the generated `custom_sentences/<language>/smart_home_teacher_native.yaml` file. Review each mapping: it intentionally targets one specific exposed script and has no slots.
- The supplied installer performed host-level cleanup and restart operations. It is intentionally **not included as an auto-run installation path** in this repository; use the standard custom-integration installation above.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/INSTALLATION.md](docs/INSTALLATION.md), and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Repository validation

Run:

```bash
bash scripts/validate.sh
```

This performs Python syntax compilation and JSON parsing without requiring a running Home Assistant instance or Ollama.
