import asyncio
import sys
from pathlib import Path
from types import ModuleType

import pytest


pytest_plugins = "pytest_homeassistant_custom_component"


package_name = "custom_components.smart_home_teacher"
package = ModuleType(package_name)
package.__path__ = [
    str(Path(__file__).parents[1] / "custom_components" / "smart_home_teacher")
]
# Import the phrase module without loading the newer conversation platform setup.
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
