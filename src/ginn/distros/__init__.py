# SPDX-License-Identifier: Apache-2.0
"""Distribution definitions as data (NFR-060). The loader is in loader.py; the TOML is in data/."""

from ginn.distros.loader import (
    DistroDef,
    HostLocale,
    Integration,
    IntegrationSet,
    Verb,
    available,
    from_mapping,
    host_locale,
    load,
    load_file,
    parse_text,
)

__all__ = [
    "DistroDef",
    "HostLocale",
    "Integration",
    "IntegrationSet",
    "Verb",
    "available",
    "from_mapping",
    "host_locale",
    "load",
    "load_file",
    "parse_text",
]
