"""uRep — micro replication."""

from importlib import metadata

try:
    __version__ = metadata.version("urep")
except metadata.PackageNotFoundError:
    __version__ = "0.0.0+dev"

if __version__ == "0.0.0+dev":
    __version__ = "development"
