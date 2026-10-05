"""Read notice version payloads header-first for read-only board projections.

A PPS notice keeps every extraction attempt as an immutable version, and each
attempt stores the complete model output, document processing audit and
quantitative record. Board projections choose the current attempt per
attachment from a few leading header fields and then read the full payload of
that attempt only. Older retries were still transferred and decoded in full on
every board read.

Payloads are serialised with insertion order, so the header fields sit at the
front of the stored JSON text. This module reads a short text prefix, parses
only the top-level members that are complete inside it, and installs a
``HeaderFirstPayload`` for attempts that are unlikely to be read in full. Any
access beyond a known header value loads the real payload (for every deferred
attempt of the same notice at once), so a wrong guess costs one query and
never changes a result.

Only read-only request sessions may use this. Never pass these objects to a
write path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from .models import Notice, NoticeVersion

PREFIX_CHARS = 4096
EXTRACTION_KIND = "OPENAI_REQUIREMENT_EXTRACTION"
_ID_CHUNK = 500
_WHITESPACE = " \t\n\r"
_DECODER = json.JSONDecoder()


def parse_payload_prefix(prefix: str | None) -> tuple[dict[str, Any], bool] | None:
    """Return the complete top-level members of a JSON object prefix.

    The flag is True when the whole object fit in the prefix. ``None`` means
    the text is not a JSON object (or is malformed) and must be read in full.
    """

    if not prefix:
        return None
    text, end = prefix, len(prefix)
    index = _skip(text, 0)
    if index >= end or text[index] != "{":
        return None
    index = _skip(text, index + 1)
    members: dict[str, Any] = {}
    if index < end and text[index] == "}":
        return members, True
    while index < end:
        try:
            key, index = _DECODER.raw_decode(text, index)
        except ValueError:
            return members, False
        if not isinstance(key, str):
            return None
        index = _skip(text, index)
        if index >= end:
            return members, False
        if text[index] != ":":
            return None
        index = _skip(text, index + 1)
        try:
            value, after = _DECODER.raw_decode(text, index)
        except ValueError:
            return members, False
        if after >= end:
            # A number or literal can decode from a cut-off prefix.
            return members, False
        members[key] = value
        index = _skip(text, after)
        if index >= end:
            return members, False
        if text[index] == "}":
            return members, True
        if text[index] != ",":
            return None
        index = _skip(text, index + 1)
    return members, False


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


class HeaderFirstPayload(dict):
    """A version payload that answers header reads before loading itself.

    ``get``/``[]`` of a member parsed from the prefix answer from it. Every
    other operation loads the real payload first, so the object behaves as the
    stored dict. The underlying storage always holds the parsed header, which
    keeps C-level emptiness checks honest until the full payload arrives.
    """

    __slots__ = ("_load", "_loaded")

    def __init__(self, header: dict[str, Any], load: Callable[[], None]) -> None:
        super().__init__(header)
        self._load = load
        self._loaded = False

    def _fill(self, payload: dict[str, Any]) -> None:
        if not self._loaded:
            dict.clear(self)
            dict.update(self, payload)
            self._loaded = True

    def _full(self) -> HeaderFirstPayload:
        if not self._loaded:
            self._load()
        return self

    def get(self, key, default=None):
        if not self._loaded and dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return dict.get(self._full(), key, default)

    def __getitem__(self, key):
        if not self._loaded and dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return dict.__getitem__(self._full(), key)

    def __bool__(self) -> bool:
        # A header member exists, so the stored object is never empty.
        return True

    def __contains__(self, key) -> bool:
        return dict.__contains__(self._full(), key)

    def __iter__(self):
        return dict.__iter__(self._full())

    def __len__(self) -> int:
        return dict.__len__(self._full())

    def __eq__(self, other) -> bool:
        if isinstance(other, HeaderFirstPayload):
            other._full()
        return dict.__eq__(self._full(), other)

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return dict.__repr__(self._full())

    def __or__(self, other):
        return dict(self.items()) | other

    def __ror__(self, other):
        return other | dict(self.items())

    def __reduce_ex__(self, protocol):
        return (dict, (dict(self.items()),))

    def __copy__(self):
        return dict(self.items())

    def __deepcopy__(self, memo):
        import copy

        return copy.deepcopy(dict(self.items()), memo)

    def keys(self):
        return dict.keys(self._full())

    def values(self):
        return dict.values(self._full())

    def items(self):
        return dict.items(self._full())

    def copy(self):
        return dict(self.items())

    # Mutation behaves exactly like the stored dict once it is loaded.
    def __setitem__(self, key, value):
        dict.__setitem__(self._full(), key, value)

    def __delitem__(self, key):
        dict.__delitem__(self._full(), key)

    def __ior__(self, other):
        dict.update(self._full(), other)
        return self

    def clear(self):
        dict.clear(self._full())

    def pop(self, *args):
        return dict.pop(self._full(), *args)

    def popitem(self):
        return dict.popitem(self._full())

    def setdefault(self, key, default=None):
        return dict.setdefault(self._full(), key, default)

    def update(self, *args, **kwargs):
        dict.update(self._full(), *args, **kwargs)


def attach_header_first_payloads(session: Session, notices: Iterable[Notice]) -> None:
    """Install payloads on versions loaded with ``source_payload`` deferred.

    Full payloads are read up front for every non-extraction version and, per
    attachment and manifest binding, for the newest extraction attempt. Other
    attempts receive a ``HeaderFirstPayload``.
    """

    versions = [version for notice in notices for version in notice.versions]
    if not versions:
        return
    prefixes: dict[str, str | None] = {}
    prefix_expression = func.substr(cast(NoticeVersion.source_payload, Text), 1, PREFIX_CHARS)
    ids = [version.id for version in versions]
    for offset in range(0, len(ids), _ID_CHUNK):
        chunk = ids[offset : offset + _ID_CHUNK]
        for version_id, prefix in session.execute(
            select(NoticeVersion.id, prefix_expression).where(NoticeVersion.id.in_(chunk))
        ).all():
            prefixes[version_id] = prefix

    complete: dict[str, dict[str, Any]] = {}
    headers: dict[str, dict[str, Any]] = {}
    full_ids: set[str] = set()
    for notice in notices:
        newest: dict[tuple[Any, Any, Any], NoticeVersion] = {}
        for version in notice.versions:
            parsed = parse_payload_prefix(prefixes.get(version.id))
            if parsed is None:
                full_ids.add(version.id)
                continue
            members, whole = parsed
            if whole:
                complete[version.id] = members
                continue
            binding = (
                members.get("attachment_id"),
                members.get("manifest_sha256"),
                members.get("current_manifest_sha256"),
            )
            if members.get("kind") != EXTRACTION_KIND or None in binding:
                full_ids.add(version.id)
                continue
            headers[version.id] = members
            current = newest.get(binding)
            if current is None or version.version_no > current.version_no:
                newest[binding] = version
        full_ids.update(version.id for version in newest.values())

    payloads = _read_payloads(session, full_ids)
    stats = session.info.setdefault(
        "version_payload_stats", {"versions": 0, "full": 0, "deferred": 0, "late": 0}
    )
    stats["versions"] += len(versions)
    stats["full"] += len(full_ids) + len(complete)
    stats["deferred"] += len(versions) - len(full_ids) - len(complete)
    for notice in notices:
        deferred: list[tuple[NoticeVersion, HeaderFirstPayload]] = []

        def load_deferred(deferred=deferred) -> None:
            pending = [item for item in deferred if not item[1]._loaded]
            loaded = _read_payloads(session, {version.id for version, _payload in pending})
            stats["late"] += len(pending)
            for version, payload in pending:
                payload._fill(loaded[version.id])

        for version in notice.versions:
            if version.id in complete:
                set_committed_value(version, "source_payload", complete[version.id])
            elif version.id in headers and version.id not in full_ids:
                lazy = HeaderFirstPayload(headers[version.id], load_deferred)
                deferred.append((version, lazy))
                set_committed_value(version, "source_payload", lazy)
            else:
                set_committed_value(version, "source_payload", payloads.get(version.id))


def _read_payloads(session: Session, version_ids: set[str]) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    ids = sorted(version_ids)
    for offset in range(0, len(ids), _ID_CHUNK):
        chunk = ids[offset : offset + _ID_CHUNK]
        for version_id, payload in session.execute(
            select(NoticeVersion.id, NoticeVersion.source_payload).where(NoticeVersion.id.in_(chunk))
        ).all():
            payloads[version_id] = payload
    return payloads
