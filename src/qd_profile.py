"""Resolve Quick Desktop's active-profile data directory.

Quick moved per-user data under ~/.quickwork/profiles/<profile>/ (multi-profile
layout, ~May 2026). This resolves the active profile from profiles.json
(last_active), falling back to the legacy top-level layout. All QD sync scripts
import their source paths from here so a future profile switch needs no code change.
"""

import glob
import json
import logging
import os
import time

log = logging.getLogger(__name__)

QD_ROOT = os.path.expanduser("~/.quickwork")
_PROFILES_JSON = os.path.join(QD_ROOT, "profiles.json")


def profile_base():
    """Return the active profile's data dir, or QD_ROOT for the legacy layout."""
    try:
        with open(_PROFILES_JSON) as f:
            data = json.load(f)
        active = data.get("last_active")
        for e in data.get("entries", []):
            if e.get("id") == active:
                return os.path.join(QD_ROOT, e["data_path"])
    except (OSError, ValueError, KeyError):
        pass
    return QD_ROOT


def qd_path(*parts):
    """Build a path under the active profile base."""
    return os.path.join(profile_base(), *parts)


def slack_cache_dir():
    """Newest slack_builtin_cache/<uuid>/ dir, falling back to legacy slack_cache/."""
    for d in sorted(glob.glob(qd_path("slack_builtin_cache", "*")),
                    key=os.path.getmtime, reverse=True):
        if os.path.isdir(d):
            return d
    return qd_path("slack_cache")


def warn_if_stale(path, max_age_days=7):
    """Log a warning if a source is missing or older than max_age_days.

    Catches future relocations (the failure mode where the sync silently runs
    against frozen files). Warning-only by design so one stale source never
    aborts the rest of the sync chain.
    """
    if not os.path.exists(path):
        log.warning("QD source MISSING: %s", path)
        return
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    if age_days > max_age_days:
        log.warning("QD source STALE (%.1fd old): %s", age_days, path)
