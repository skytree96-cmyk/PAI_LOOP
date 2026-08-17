from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any


RESOURCE = "data/pricing_method_profiles_v1.json"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@lru_cache(maxsize=1)
def _profiles() -> dict[str, dict[str, Any]]:
    payload = json.loads(resources.files("pai_loop").joinpath(RESOURCE).read_text(encoding="utf-8"))
    if payload.get("classification") != "PUBLIC_DOCUMENT_DERIVED":
        raise RuntimeError("pricing profile classification is invalid")
    result: dict[str, dict[str, Any]] = {}
    for profile in payload.get("profiles", []):
        digest = str(profile.get("document_sha256") or "").casefold()
        if not _SHA256.fullmatch(digest) or digest in result:
            raise RuntimeError("pricing profile document digest is invalid")
        result[digest] = profile
    return result


def pricing_profile_for_document(document_sha256: str | None) -> dict[str, Any] | None:
    """Return a grounded profile only on an exact document digest match."""

    digest = str(document_sha256 or "").casefold()
    profile = _profiles().get(digest)
    if profile is None:
        return None
    result = copy.deepcopy(profile)
    result["applicability"] = "EXACT_DOCUMENT_SHA256"
    result["score_prediction"] = None
    result["warning"] = "산식 근거만 확인했습니다. 유효 최저가·해당 투찰가·예정가격 없이는 가격점수를 계산하지 않습니다."
    return result
