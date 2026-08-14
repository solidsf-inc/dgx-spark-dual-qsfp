"""DGX Spark dual-QSFP / four-RoCE-device validation and benchmark tools."""

from ._version import VERSION
from .model import ConfigError, PairConfig, load_config

__all__ = ["ConfigError", "PairConfig", "load_config"]
__version__ = VERSION
