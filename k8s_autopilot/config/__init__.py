"""Configuration module for K8s Autopilot.

Public surface:

- ``manifest`` — canonical option registry (``ConfigOption``, ``OptionKind``)
- ``settings`` — ``Settings`` dataclass + ``get_settings()``
- ``store`` — ``ConfigStore``, ``ConfigEntry``, ``ConfigCategory``
- ``store_factory`` — ``create_config_store()``
- ``paths`` — filesystem constants
- ``metadata`` — trace metadata helpers
"""

from k8s_autopilot.config.manifest import (
    ConfigOption,
    OptionKind,
    get_config_options,
    get_option,
    get_option_by_db_key,
    option_keys,
)
from k8s_autopilot.config.paths import (
    CONFIG_PATH,
    DATA_DIR,
    ENV_PREFIX,
    GLOBAL_ENV_PATH,
    SESSIONS_DB_PATH,
    STATE_DIR,
    find_project_root,
)
from k8s_autopilot.config.settings import Settings, get_settings, reload_settings
from k8s_autopilot.config.store import ConfigCategory, ConfigEntry, ConfigStore
from k8s_autopilot.config.store_factory import create_config_store

__all__ = [
    "CONFIG_PATH",
    "ConfigCategory",
    "ConfigEntry",
    "ConfigOption",
    "ConfigStore",
    "DATA_DIR",
    "ENV_PREFIX",
    "GLOBAL_ENV_PATH",
    "OptionKind",
    "SESSIONS_DB_PATH",
    "STATE_DIR",
    "Settings",
    "create_config_store",
    "find_project_root",
    "get_config_options",
    "get_option",
    "get_option_by_db_key",
    "get_settings",
    "option_keys",
    "reload_settings",
]
