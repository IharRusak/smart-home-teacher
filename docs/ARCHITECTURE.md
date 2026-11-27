# Architecture

## Runtime components

| File | Responsibility |
|---|---|
| `manifest.json` | HA integration metadata, version and dependencies |
| `const.py` | Integration domain, Ollama URL/model and storage names |
| `config_flow.py` | Import-based Config Entry creation |
| `__init__.py` | Integration lifecycle and Conversation platform setup |
| `conversation.py` | Assist conversation agent, FIFO learning queue, entity discovery, Ollama classification and alias validation |
| `custom_actions.py` | Persistence and registration of learned phrase → script mappings |
| `translations/*.json` | English and Russian setup translations |

## Request flow

```mermaid
flowchart TD
    A[Assist request] --> B{HA local intent recognized?}
    B -->|Yes| C[HA executes native intent]
    B -->|No| D[Smart Home Teacher fallback]
    D --> E[Immediate fallback acknowledgement]
    D --> F[FIFO background queue]
    F --> G[Collect exposed entities and aliases]
    G --> H[Ollama classifies latest utterance]
    H --> I{Classification}
    I -->|entity_alias| J[Validate literal alias and entity]
    J --> K[Update Entity Registry]
    I -->|custom_phrase| L[Validate exposed script and phrase]
    L --> M[Persist JSON + generate Assist sentence]
    M --> N[Register intent handler and reload affected language]
    I -->|ignore| O[No change]
    N --> P[Later Assist turn matches learned sentence]
    P --> Q[Recheck script exists and is exposed]
    Q --> R[Run script]
```

## Safety boundary

The queue worker is training-only. It can write an entity alias or a custom sentence mapping; it does not call device services. The custom intent handler is a separate execution path: it is invoked only by a later local Assist match and calls `script.turn_on` for the mapped script after checking that the entity still exists and remains exposed to Assist.

The model receives entity identifiers, domains, names and aliases for exposed entities. It does not receive device state from the entity discovery function. The candidate list is capped at 64 entries and sorted by lexical overlap with the request.

## Known design limitation

The current alias validator proves that the alias is a short literal fragment of the utterance and that the selected entity was among the exposed candidates. It does not prove that the selected entity is semantically the device named by the user. There is no deterministic domain/device-class compatibility check and no minimum model confidence field in the response schema. For ambiguous or incorrect mappings, the model can therefore associate a valid alias with the wrong exposed entity. This is a key area for a future validator/orchestrator improvement.
