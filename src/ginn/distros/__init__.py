# SPDX-License-Identifier: Apache-2.0
"""Distribution definitions as data (NFR-060). The loader is in loader.py; the TOML is in data/."""

from ginn.distros.loader import (
    TEST_SWITCH_ENV,
    TMPDIR_FIELD,
    DistroDef,
    ExportHook,
    HostLocale,
    Integration,
    IntegrationSet,
    Verb,
    available,
    clear_cache,
    distro_fixtures_enabled,
    from_mapping,
    host_locale,
    load,
    load_file,
    parse_text,
    unknown_distribution_error,
)

__all__ = ["TEST_SWITCH_ENV", "TMPDIR_FIELD"]
__all__ += ["DistroDef", "ExportHook", "HostLocale", "Integration", "IntegrationSet", "Verb"]
__all__ += ["available", "clear_cache", "distro_fixtures_enabled", "from_mapping"]
__all__ += ["host_locale", "load", "load_file", "parse_text", "unknown_distribution_error"]
