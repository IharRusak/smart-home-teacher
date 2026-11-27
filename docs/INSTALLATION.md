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

## Configure Ollama endpoint

Edit `custom_components/smart_home_teacher/const.py`:

```python
OLLAMA_URL = "http://ollama-host-or-container:11434"
OLLAMA_MODEL = "qwen3:4b-instruct"
```

The URL must be reachable from Home Assistant's network namespace. In Docker deployments, `127.0.0.1` inside the Home Assistant container is not automatically the host or another container.

## Assist settings

- Select Smart Home Teacher as the Conversation agent.
- Keep **Prefer handling commands locally** enabled so native HA intents are tried first.
- Expose only intended devices and scripts.
- Review learned aliases and mappings after tests.
