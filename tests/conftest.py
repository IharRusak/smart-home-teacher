import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
import homeassistant.components.conversation as ha_conversation


pytest_plugins = "pytest_homeassistant_custom_component"

# The pinned HA test dependency predates the conversation API used by this integration.
if not hasattr(ha_conversation, "ConversationEntity"):
    ha_conversation.ConversationEntity = type("ConversationEntity", (), {})

for module_name, class_names in (
    (
        "homeassistant.components.conversation.models",
        ("ConversationInput", "ConversationResult"),
    ),
    (
        "homeassistant.components.conversation.chat_log",
        ("ChatLog", "UserContent", "AssistantContent"),
    ),
):
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        compatibility_module = ModuleType(module_name)
        for class_name in class_names:
            setattr(compatibility_module, class_name, type(class_name, (), {}))
        sys.modules[module_name] = compatibility_module


package_name = "custom_components.smart_home_teacher"
package = ModuleType(package_name)
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "smart_home_teacher")
]
# Import integration modules without loading the newer conversation platform setup.
sys.modules[package_name] = package


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: Home Assistant integration end-to-end tests")


@pytest.fixture
def event_loop(socket_enabled):
    """Create Home Assistant's event loop after its Windows socketpair is allowed."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def isolate_hass_config_dir(hass, tmp_path):
    """Keep test persistence isolated from the shared plugin fixture directory."""
    hass.config.config_dir = str(tmp_path)
