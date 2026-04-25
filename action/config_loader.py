"""Loads .prism/config.yml from a target Laravel repository.

If the file is absent or malformed, all fields fall back to safe defaults.
Repo admins control behaviour by committing .prism/config.yml to their repo.

Supported keys:
  scan_paths     - list of path glob patterns (** supported); default: ["app/**", "database/migrations/"]
  disabled_rules - list of issue type strings to suppress; default: []

NOT repo-configurable (Prism repo controls these):
  cost_threshold_usd - set via COST_THRESHOLD_USD env var in review.yml
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

_DEFAULTS: dict = {
    "scan_paths": ["app/**", "database/migrations/"],
    "disabled_rules": [],
}

_VALID_KEYS = set(_DEFAULTS.keys())


def _compile_scan_pattern(pattern: str) -> re.Pattern:
    """Convert a path glob pattern (supporting ** and *) to a compiled regex.

    Examples:
        "app/**"           → matches any file under app/
        "app/**/Models"    → matches app/Business/Models/Foo.php, app/Models/Foo.php
        "database/migrations/" → matches files directly under that prefix
    """
    parts = re.split(r'(\*\*|\*)', pattern)
    result = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p == '**':
            # If the next literal starts with '/', absorb it into the ** group so
            # that ** can match zero path segments (e.g. "app/**/Models" matches
            # both "app/Models/..." and "app/Foo/Models/...").
            if i + 1 < len(parts) and parts[i + 1] not in ('**', '*') and parts[i + 1].startswith('/'):
                result.append('(.*/)?')
                parts[i + 1] = parts[i + 1][1:]  # strip consumed leading slash
            else:
                result.append('.*')
        elif p == '*':
            result.append('[^/]*')
        else:
            result.append(re.escape(p))
        i += 1

    regex = ''.join(result)
    # A trailing '/' already acts as a directory prefix — no boundary anchor needed.
    if pattern.endswith('/'):
        return re.compile(r'^' + regex)
    return re.compile(r'^' + regex + r'(/|$)')


@dataclass
class PrismConfig:
    scan_paths: list[str]
    disabled_rules: list[str]

    def should_scan(self, file_path: str) -> bool:
        """Return True if file_path matches any scan_paths glob pattern."""
        return any(_compile_scan_pattern(p).match(file_path) for p in self.scan_paths)

    def is_rule_disabled(self, rule_type: str) -> bool:
        return rule_type in self.disabled_rules


def load_config(laravel_path: str | None) -> PrismConfig:
    """Load .prism/config.yml from the target repo, merging with defaults.

    Args:
        laravel_path: Absolute path to the checked-out target repo root.
                      Pass None to use defaults only.

    Returns:
        PrismConfig with all fields populated (from file or defaults).
    """
    merged = dict(_DEFAULTS)

    if laravel_path:
        config_file = os.path.join(laravel_path, ".prism", "config.yml")
        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as fh:
                    repo_config: dict = yaml.safe_load(fh) or {}

                unknown = set(repo_config) - _VALID_KEYS
                if unknown:
                    logger.warning("[Config] Unknown keys in .prism/config.yml: %s — ignored", unknown)

                for key in _VALID_KEYS:
                    if key in repo_config and repo_config[key] is not None:
                        merged[key] = repo_config[key]

                logger.info("[Config] Loaded .prism/config.yml — scan_paths=%s", merged["scan_paths"])
            except yaml.YAMLError as exc:
                logger.warning("[Config] Malformed .prism/config.yml: %s — using defaults", exc)
            except OSError as exc:
                logger.warning("[Config] Cannot read .prism/config.yml: %s — using defaults", exc)
        else:
            logger.info("[Config] No .prism/config.yml found — using defaults")

    return PrismConfig(
        scan_paths=merged["scan_paths"],
        disabled_rules=list(merged["disabled_rules"]),
    )
