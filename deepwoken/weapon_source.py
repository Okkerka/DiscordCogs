"""Bounded MediaWiki source retrieval and table parsing for Deepwoken weapons."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import html
import json
import re
from typing import Protocol


API_URL = "https://deepwoken.fandom.com/api.php"
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "DiscordCogs-DeepwokenWeaponUpdater/1.0"
SOURCE_COLUMN_COUNT = 11
_READ_CHUNK_BYTES = 64 * 1024
_SOURCE_HEADER_CELLS = (
    "Name",
    "Requirements",
    "Base Damage",
    "Scaling",
    "Armor Penetration",
    "Chip Damage",
    "Posture Damage",
    "Range",
    "Swing Speed",
    "Endlag",
    "Scaled Damage",
)
_CRAZY_SLOTS_HEADER_CELLS = (
    "Name",
    "Base Damage",
    "Scaling",
    "Penetration",
    "Chip Damage",
    "Posture Damage",
    "Range",
    "Swing Speed",
    "Endlag",
    "Scaled Damage",
)
_HYBRID_HEADER_CELLS = (
    "Name",
    "Hybrid Type",
    *_SOURCE_HEADER_CELLS[1:],
)
_OFFHAND_TABLE_SCHEMAS = {
    "Shields": (
        ("Name", "Requirements", "Max Posture Bonus"),
        "Shield",
        "posture",
    ),
    "Parrying Daggers": (
        ("Name", "Requirements", "Parry Posture Restoration"),
        "Parrying Dagger",
        "posture",
    ),
    "Offhand Pistols": (
        (
            "Name", "Requirements", "Base Damage", "Penetration",
            "Chip Damage", "Posture Damage", "Range", "Offhand Cooldown",
        ),
        "Offhand Pistol",
        "pistol",
    ),
}

# Dict insertion order is intentional: reconciliation must see source classes in a
# stable order even if MediaWiki returns revision metadata in a different order.
TEMPLATE_SOURCES: Mapping[str, str] = {
    "AllLightWeapons": "Light",
    "AllMediumWeapons": "Medium",
    "AllHeavyWeapons": "Heavy",
    "AllHybridWeapons": "Hybrid",
    "AllElementalWeapons": "Elemental",
    "AllCrazySlotsWeapons": "Crazy Slots",
    "AllExclusiveWeapons": "Exclusive",
    "AllOffhandWeapons": "Offhand",
}

_TEMPLATE_HEADER_CELLS = {
    "AllLightWeapons": _SOURCE_HEADER_CELLS,
    "AllMediumWeapons": _SOURCE_HEADER_CELLS,
    "AllHeavyWeapons": _SOURCE_HEADER_CELLS,
    "AllHybridWeapons": _HYBRID_HEADER_CELLS,
    "AllElementalWeapons": _SOURCE_HEADER_CELLS,
    "AllCrazySlotsWeapons": _CRAZY_SLOTS_HEADER_CELLS,
    "AllExclusiveWeapons": _SOURCE_HEADER_CELLS,
}

WEAPON_TYPE_ALIASES = {
    "Axes": "Axe",
    "Daggers": "Dagger",
    "Fists": "Fist",
    "Greataxes": "Greataxe",
    "Greatswords": "Greatsword",
    "Hammers": "Hammer",
    "Katanas": "Katana",
    "Rapiers": "Rapier",
    "Rifles": "Rifle",
    "Spears": "Spear",
    "Staves": "Staff",
    "Swords": "Sword",
}

_MAX_CELL_CHARACTERS = 20_000
_ROW_BOUNDARY = re.compile(r"(?m)^\s*\|-[^\r\n]*$")
_TABLE_BLOCK = re.compile(r"(?ms)^\s*\{\|.*?^\s*\|\}")
_COLSPAN_ELEVEN = re.compile(r"\bcolspan\s*=\s*['\"]?11\b", re.IGNORECASE)
_COLLAPSIBLE_BLOCK = re.compile(
    r"(?is)<(div|span)\b[^>]{0,2048}\b(?:mw-)?collapsible\b[^>]{0,2048}>.*?</\1\s*>"
)
_LINE_BREAK = re.compile(r"(?is)<br\s*/?\s*>")
_HTML_TAG = re.compile(r"(?is)</?[a-z][^>]{0,2048}>")
_LINK_WITH_LABEL = re.compile(r"\[\[([^\[\]|]{1,512})\|([^\[\]]{1,1024})\]\]")
_PLAIN_LINK = re.compile(r"\[\[([^\[\]]{1,1024})\]\]")
_ATTRIBUTE_PREFIX = re.compile(
    r"^\s*(?:(?:style|class|colspan|rowspan|data-[\w-]+)\s*=\s*"
    r"(?:\"[^\"]{0,512}\"|'[^']{0,512}'|[^\s|]{1,512})\s*)+\|\s*",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


class WeaponSourceError(RuntimeError):
    """A safe, concise failure at the untrusted weapon-source boundary."""


@dataclass(frozen=True, slots=True)
class TemplatePayload:
    template: str
    weapon_class: str
    revision_id: int
    revision_timestamp: str
    wikitext: str


@dataclass(frozen=True, slots=True)
class ParsedWeapon:
    source_template: str
    source_class: str
    weapon_type: str
    cells: tuple[str, ...]


class _ResponseContent(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


class _Response(Protocol):
    status: int
    content: _ResponseContent

    async def __aenter__(self) -> "_Response": ...

    async def __aexit__(self, *args: object) -> None: ...


class _Session(Protocol):
    async def __aenter__(self) -> "_Session": ...

    async def __aexit__(self, *args: object) -> None: ...

    def get(self, url: str, **kwargs: object) -> _Response: ...


def _default_session_factory() -> _Session:
    """Build the Red-provided HTTP client only when a live fetch is requested."""
    import aiohttp

    return aiohttp.ClientSession()


class FandomWeaponSource:
    """Fetch template expansions with fixed endpoint, timeout, and body bounds."""

    def __init__(self, session_factory: Callable[[], _Session] | None = None):
        self._session_factory = session_factory or _default_session_factory

    async def fetch(self) -> tuple[TemplatePayload, ...]:
        try:
            async with self._session_factory() as session:
                revisions = await self._fetch_revisions(session)
                payloads = []
                for template, weapon_class in TEMPLATE_SOURCES.items():
                    wikitext = await self._fetch_expansion(session, template)
                    revision_id, revision_timestamp = revisions[template]
                    payloads.append(
                        TemplatePayload(
                            template=template,
                            weapon_class=weapon_class,
                            revision_id=revision_id,
                            revision_timestamp=revision_timestamp,
                            wikitext=wikitext,
                        )
                    )
                return tuple(payloads)
        except asyncio.CancelledError:
            raise
        except WeaponSourceError:
            raise
        except Exception as exc:
            raise WeaponSourceError("Unable to fetch weapon source.") from exc

    async def _fetch_revisions(self, session: _Session) -> dict[str, tuple[int, str]]:
        document = await self._request_json(
            session,
            {
                "action": "query",
                "prop": "revisions",
                "titles": "|".join(f"Template:{template}" for template in TEMPLATE_SOURCES),
                "rvprop": "ids|timestamp",
                "format": "json",
                "formatversion": 2,
            },
        )
        try:
            pages = document["query"]["pages"]
        except (KeyError, TypeError) as exc:
            raise WeaponSourceError("Weapon source response is incomplete.") from exc
        if not isinstance(pages, list):
            raise WeaponSourceError("Weapon source response is incomplete.")

        revisions: dict[str, tuple[int, str]] = {}
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("title"), str):
                continue
            title = page["title"]
            if not title.startswith("Template:"):
                continue
            template = title.removeprefix("Template:")
            if template not in TEMPLATE_SOURCES:
                continue
            page_revisions = page.get("revisions")
            if not isinstance(page_revisions, list) or not page_revisions or not isinstance(page_revisions[0], dict):
                continue
            revision = page_revisions[0]
            revision_id = revision.get("revid")
            timestamp = revision.get("timestamp")
            if isinstance(revision_id, int) and not isinstance(revision_id, bool) and isinstance(timestamp, str) and timestamp:
                revisions[template] = (revision_id, timestamp)
        if len(revisions) != len(TEMPLATE_SOURCES):
            raise WeaponSourceError("Weapon source revision metadata is incomplete.")
        return revisions

    async def _fetch_expansion(self, session: _Session, template: str) -> str:
        document = await self._request_json(
            session,
            {
                "action": "expandtemplates",
                "text": "{{" + template + "}}",
                "prop": "wikitext",
                "format": "json",
                "formatversion": 2,
            },
        )
        try:
            wikitext = document["expandtemplates"]["wikitext"]
        except (KeyError, TypeError) as exc:
            raise WeaponSourceError("Weapon source expansion is incomplete.") from exc
        if not isinstance(wikitext, str):
            raise WeaponSourceError("Weapon source expansion is incomplete.")
        return wikitext

    async def _request_json(self, session: _Session, params: dict[str, object]) -> dict[str, object]:
        try:
            async with session.get(
                API_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if not 200 <= response.status < 300:
                    raise WeaponSourceError("Weapon source request failed.")
                body = await _read_bounded_body(response.content)
        except asyncio.CancelledError:
            raise
        except WeaponSourceError:
            raise
        except Exception as exc:
            raise WeaponSourceError("Weapon source request failed.") from exc
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise WeaponSourceError("Weapon source response is too large.")
        try:
            decoded = body.decode("utf-8", errors="strict")
            document = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeaponSourceError("Weapon source response is invalid.") from exc
        if not isinstance(document, dict):
            raise WeaponSourceError("Weapon source response is invalid.")
        return document


async def _read_bounded_body(content: _ResponseContent) -> bytes:
    """Read through EOF while retaining at most the response-size limit plus one byte."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_RESPONSE_BYTES:
        limit = min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total)
        chunk = await content.read(limit)
        if not isinstance(chunk, bytes):
            raise WeaponSourceError("Weapon source response is invalid.")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise WeaponSourceError("Weapon source response is too large.")


def parse_template(payload: TemplatePayload) -> list[ParsedWeapon]:
    """Parse one expanded table without allowing malformed rows to shift fields."""
    if payload.template == "AllOffhandWeapons":
        return _parse_offhand_template(payload)

    expected_header = _TEMPLATE_HEADER_CELLS.get(payload.template)
    if expected_header is None or not _has_exact_header(payload, payload.template, expected_header):
        raise WeaponSourceError("Malformed weapon source.")
    weapon_type = "Unknown"
    parsed: list[ParsedWeapon] = []
    for raw_row in _ROW_BOUNDARY.split(payload.wikitext):
        row = raw_row.split("\n|}", 1)[0].strip()
        if not row or row.startswith("{|"):
            continue
        if _is_expected_header_row(row, expected_header):
            continue
        cells = _split_top_level_cells(row)
        if _is_category_row(row, cells):
            heading = _normalize_weapon_type(cells[0])
            if heading and payload.template != "AllHybridWeapons":
                weapon_type = heading
            continue
        if not row.lstrip().startswith(("|", "!")):
            continue
        if len(cells) != len(expected_header):
            raise WeaponSourceError("Malformed weapon source.")
        if payload.template == "AllHybridWeapons":
            cells = [cells[0], *cells[2:]]
        elif payload.template == "AllCrazySlotsWeapons":
            cells = [cells[0], "Crazy Slots", *cells[1:]]
        normalized = tuple(_normalize_cell(cell) for cell in cells)
        if not normalized[0]:
            raise WeaponSourceError("Malformed weapon source.")
        parsed.append(
            ParsedWeapon(
                source_template=payload.template,
                source_class=payload.weapon_class,
                weapon_type=weapon_type,
                cells=normalized,
            )
        )
    if not parsed:
        raise WeaponSourceError("Required weapon source has no valid rows.")
    return parsed


def _is_expected_header_row(row: str, expected: tuple[str, ...]) -> bool:
    """Recognize only the template's exact documented header."""
    if not row.lstrip().startswith("!"):
        return False
    cells = _split_top_level_cells(row, delimiter="!!")
    return tuple(_normalize_cell(cell) for cell in cells) == expected


def _has_exact_header(
    payload: TemplatePayload,
    template: str,
    expected: tuple[str, ...],
) -> bool:
    """Gate an alternate schema on both its fixed template and exact header."""
    if payload.template != template:
        return False
    for line in payload.wikitext.splitlines():
        if not line.lstrip().startswith("!"):
            continue
        cells = _split_top_level_cells(line, delimiter="!!")
        return tuple(_normalize_cell(cell) for cell in cells) == expected
    return False


def _parse_offhand_template(payload: TemplatePayload) -> list[ParsedWeapon]:
    """Map only the three documented Offhand table layouts to lookup-only cells."""
    parsed: list[ParsedWeapon] = []
    seen_captions: set[str] = set()
    for block in _TABLE_BLOCK.findall(payload.wikitext):
        caption_lines = [line for line in block.splitlines() if line.lstrip().startswith("|+")]
        header_lines = [line for line in block.splitlines() if line.lstrip().startswith("!")]
        if len(caption_lines) != 1 or len(header_lines) != 1:
            raise WeaponSourceError("Malformed weapon source.")
        caption = _normalize_cell(caption_lines[0].lstrip()[2:])
        header = tuple(
            _normalize_cell(cell)
            for cell in _split_top_level_cells(header_lines[0], delimiter="!!")
        )
        schema = _OFFHAND_TABLE_SCHEMAS.get(caption)
        if schema is None or header != schema[0] or caption in seen_captions:
            raise WeaponSourceError("Malformed weapon source.")
        seen_captions.add(caption)
        weapon_type, adapter = schema[1], schema[2]
        table_count = 0
        for raw_row in _ROW_BOUNDARY.split(block)[1:]:
            row = raw_row.split("\n|}", 1)[0].strip()
            if not row:
                continue
            cells = _split_top_level_cells(row)
            if row.lstrip().startswith("!"):
                if tuple(_normalize_cell(cell) for cell in cells) == header:
                    continue
                raise WeaponSourceError("Malformed weapon source.")
            expected_count = 3 if adapter == "posture" else 8
            if len(cells) != expected_count:
                raise WeaponSourceError("Malformed weapon source.")
            normalized = tuple(_normalize_cell(cell) for cell in cells)
            if not normalized[0]:
                raise WeaponSourceError("Malformed weapon source.")
            canonical = _canonical_offhand_cells(normalized, adapter)
            parsed.append(
                ParsedWeapon(
                    source_template=payload.template,
                    source_class=payload.weapon_class,
                    weapon_type=weapon_type,
                    cells=canonical,
                )
            )
            table_count += 1
        if table_count == 0:
            raise WeaponSourceError("Required weapon source has no valid rows.")
    if seen_captions != set(_OFFHAND_TABLE_SCHEMAS):
        raise WeaponSourceError("Malformed weapon source.")
    return parsed


def _canonical_offhand_cells(cells: tuple[str, ...], adapter: str) -> tuple[str, ...]:
    if adapter == "posture":
        name, requirements, posture = cells
        return (name, requirements, "", "", "", "", posture, "", "", "", "")
    name, requirements, base, penetration, chip, posture, weapon_range, _cooldown = cells
    return (
        name, requirements, base, "", penetration, chip, posture, weapon_range,
        "", "", "",
    )


def _is_category_row(row: str, cells: list[str]) -> bool:
    return len(cells) == 1 and bool(_COLSPAN_ELEVEN.search(row))


def _split_top_level_cells(row: str, *, delimiter: str = "||") -> list[str]:
    """Split a table delimiter only outside MediaWiki links, templates, and HTML tags."""
    text = row.lstrip()
    if text.startswith(("|", "!")):
        text = text[1:]
    cells: list[str] = []
    start = 0
    index = 0
    link_depth = 0
    template_depth = 0
    in_tag = False
    quote: str | None = None
    while index < len(text):
        pair = text[index : index + 2]
        if in_tag:
            character = text[index]
            if quote:
                if character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == ">":
                in_tag = False
            index += 1
            continue
        if pair == "[[":
            link_depth += 1
            index += 2
            continue
        if pair == "]]" and link_depth:
            link_depth -= 1
            index += 2
            continue
        if pair == "{{":
            template_depth += 1
            index += 2
            continue
        if pair == "}}" and template_depth:
            template_depth -= 1
            index += 2
            continue
        if text[index] == "<":
            in_tag = True
            index += 1
            continue
        if text.startswith(delimiter, index) and not link_depth and not template_depth:
            cells.append(text[start:index].strip())
            start = index + len(delimiter)
            index += len(delimiter)
            continue
        index += 1
    cells.append(text[start:].strip())
    return cells


def _normalize_weapon_type(value: str) -> str:
    normalized = _normalize_cell(value)
    normalized = normalized.strip("|! ")
    return WEAPON_TYPE_ALIASES.get(normalized, normalized)


def _normalize_cell(value: str) -> str:
    value = value[:_MAX_CELL_CHARACTERS]
    value = _ATTRIBUTE_PREFIX.sub("", value)
    value = _COLLAPSIBLE_BLOCK.sub("", value)
    value = _LINE_BREAK.sub(" ", value)
    for _ in range(3):
        updated = _LINK_WITH_LABEL.sub(r"\2", value)
        if updated == value:
            break
        value = updated
    value = _PLAIN_LINK.sub(r"\1", value)
    value = _HTML_TAG.sub("", value)
    value = value.replace("'''", "").replace("''", "")
    return _WHITESPACE.sub(" ", html.unescape(value)).strip()
