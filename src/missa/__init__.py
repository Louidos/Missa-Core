"""Missa – persönliche KI-Assistentin."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("missa")
except PackageNotFoundError:  # pragma: no cover - nur ohne Installation
    __version__ = "0.6.0"
