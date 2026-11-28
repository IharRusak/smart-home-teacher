# Installation

## Prerequisites

- A working Home Assistant installation with the Conversation/Assist APIs used by this integration.
- A running Ollama API reachable from the Home Assistant runtime.
- The configured Ollama model (default `qwen3:4b-instruct`) pulled locally.
- Assist entities explicitly exposed where appropriate.

## Install from Git

1. Add the Git repository to HACS as a custom **Integration** repository.
2. Install Smart Home Teacher from HACS.
3. Restart Home Assistant.
4. Add the following to `configuration.yaml`:

   ```yaml
   smart_home_teacher: {}
   ```

5. Restart Home Assistant.
6. Select the integration as the Assist pipeline's conversation agent.

## Install manually

Copy the entire `custom_components/smart_home_teacher` directory into `/config/custom_components/`. Ensure the final path contains `manifest.json`, `__init__.py`, `conversation.py`, `custom_actions.py`, `config_flow.py`, `const.py` and `translations/`.

Add `smart_home_teacher: {}` to `configuration.yaml`, then restart Home Assistant.

When a custom phrase is learned, Smart Home Teacher adds its fixed-target
`intent_script` mapping to `configuration.yaml` and writes a custom sentence
under `custom_sentences/<language>/smart_home_teacher_native.yaml`. Home
Assistant may run the selected script on future Assist requests matching that
phrase. Review the generated
configuration and expose only scripts you intend to activate. The integration
does not call scripts during learning. Existing mapping-style `intent_script`
configuration is preserved; include-based or inline sections that cannot be
merged safely are rejected without being overwritten.

Alias learning uses Home Assistant's non-executing native-grammar probe. It is
available only when HA reports exactly one unmatched entity `name` or `area`
slot; the integration saves only that exact slot text as an alias. If the
probe is unavailable, the integration fails closed and does not learn.

To remove all learned custom intents, remove the managed `intent_script` block
from `configuration.yaml`, the marked
`custom_sentences/*/smart_home_teacher_native.yaml` files, and
`smart_home_teacher/custom_actions.json`. Reload `intent_script` and the
affected Assist sentence languages, or restart Home Assistant.

## Configure Ollama endpoint

Edit `custom_components/smart_home_teacher/const.py`:

```python
OLLAMA_URL = "http://ollama-host-or-container:11434"
OLLAMA_MODEL = "qwen3:4b-instruct"
```

The URL must be reachable from Home Assistant's network namespace. In Docker deployments, `127.0.0.1` inside the Home Assistant container is not automatically the host or another container.

The default `qwen3:4b-instruct` model is used for orchestrator, `alias_worker`, `intent_worker` and validator roles. Alias learning normally uses three `/api/chat` calls; custom-intent learning normally uses five. A single worker retry adds two calls. Ollama must support structured outputs and the Qwen3 `think` request field. The integration sends `think: false`, a JSON schema per role, and temperature `0` to keep these short classification calls predictable. See the [Qwen3 model page](https://ollama.com/library/qwen3), [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs), and [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking).

## Assist settings

- Select Smart Home Teacher as the Conversation agent.
- Keep **Prefer handling commands locally** enabled so native HA intents are tried first.
- Expose only intended devices and scripts.
- Review learned aliases and mappings after tests.
