from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


MATERIAL_SCOPE_VERSION = "pps-material-notice-keys-v1"
MAX_MATERIAL_NOTICE_KEYS = 3_000


def canonical_material_notice_keys(values: Iterable[str]) -> list[str]:
    """Return the stable, order-independent PPS material-analysis scope."""

    return sorted(set(values))


def material_scope_sha256(values: Iterable[str]) -> str:
    keys = canonical_material_notice_keys(values)
    payload = json.dumps(
        {"schema": MATERIAL_SCOPE_VERSION, "notice_keys": keys},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def material_scope_fields(values: Iterable[str]) -> dict[str, Any]:
    keys = canonical_material_notice_keys(values)
    return {
        "material_scope_version": MATERIAL_SCOPE_VERSION,
        "material_notice_keys": keys,
        "material_notice_key_count": len(keys),
        "material_notice_keys_sha256": material_scope_sha256(keys),
    }


def validated_material_scope(config: Mapping[str, Any]) -> list[str] | None:
    """Read an exact stored scope, rejecting partial or forged audit fields."""

    if config.get("material_scope_version") != MATERIAL_SCOPE_VERSION:
        return None
    raw_keys = config.get("material_notice_keys")
    raw_count = config.get("material_notice_key_count")
    raw_digest = config.get("material_notice_keys_sha256")
    if not isinstance(raw_keys, list) or len(raw_keys) > MAX_MATERIAL_NOTICE_KEYS:
        return None
    if any(
        not isinstance(key, str)
        or not key
        or len(key) > 160
        or key != key.strip()
        for key in raw_keys
    ):
        return None
    canonical = canonical_material_notice_keys(raw_keys)
    if canonical != raw_keys:
        return None
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count != len(canonical)
    ):
        return None
    if not isinstance(raw_digest, str) or raw_digest != material_scope_sha256(canonical):
        return None
    return canonical


def validated_source_material_scope(config: Mapping[str, Any]) -> list[str] | None:
    return validated_material_scope(
        {
            "material_scope_version": config.get("source_material_scope_version"),
            "material_notice_keys": config.get("source_material_notice_keys"),
            "material_notice_key_count": config.get("source_material_notice_key_count"),
            "material_notice_keys_sha256": config.get(
                "source_material_notice_keys_sha256"
            ),
        }
    )
