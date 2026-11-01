"""Bounded MediaWiki source retrieval and table parsing for Deepwoken weapons."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import html
import json
import logging
import re
from typing import Protocol


API_URL = "https://deepwoken.fandom.com/api.php"
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "DiscordCogs-DeepwokenWeaponUpdater/1.0"
_READ_CHUNK_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)

# Canonical lookup fields. Parsing maps wiki headers onto this shape so the
# workbook and reconciliation layers never depend on a table's column order.
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
_CRAZY_SLOTS_TEMPLATE = "AllCrazySlotsWeapons"
_FIELD_ALIASES = {
    "name": "Name",
    "requirements": "Requirements",
    "requirement": "Requirements",
    "base damage": "Base Damage",
    "scaling": "Scaling",
    "armor penetration": "Armor Penetration",
    "penetration": "Armor Penetration",
    "chip damage": "Chip Damage",
    "posture damage": "Posture Damage",
    "max posture bonus": "Posture Damage",
    "posture bonus": "Posture Damage",
    "parry posture restoration": "Posture Damage",
    "posture restoration": "Posture Damage",
    "range": "Range",
    "swing speed": "Swing Speed",
    "endlag": "Endlag",
    "scaled damage": "Scaled Damage",
}
# Display-only columns that must not become lookup fields.
_IGNORED_HEADER_LABELS = frozenset(
    {
        "hybrid type",
        "offhand cooldown",
        "provides talent(s)?",
        "provides talent",
        "talents",
    }
)
_STANDARD_REQUIRED_FIELDS = frozenset(_SOURCE_HEADER_CELLS)
_CRAZY_SLOTS_REQUIRED_FIELDS = _STANDARD_REQUIRED_FIELDS - {"Requirements"}
_POSTURE_REQUIRED_FIELDS = frozenset({"Name", "Requirements", "Posture Damage"})
_PISTOL_REQUIRED_FIELDS = frozenset(
    {
        "Name",
        "Requirements",
        "Base Damage",
        "Armor Penetration",
        "Chip Damage",
        "Posture Damage",
        "Range",
    }
)
_OFFHAND_TABLE_SCHEMAS = {
    "Shields": ("Shield", "posture", _POSTURE_REQUIRED_FIELDS),
    "Parrying Daggers": (
        "Parrying Dagger",
        "posture",
        _POSTURE_REQUIRED_FIELDS,
    ),
    "Offhand Pistols": (
        "Offhand Pistol",
        "pistol",
        _PISTOL_REQUIRED_FIELDS,
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
_COLSPAN = re.compile(r"\bcolspan\s*=", re.IGNORECASE)
_LEADING_PIPES = re.compile(r"^\s*\|+\s*")
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
    """Parse one expanded table by mapping its headers onto canonical fields."""
    try:
        if payload.template == "AllOffhandWeapons":
            return _parse_offhand_template(payload)
        return _parse_standard_template(payload)
    except WeaponSourceError as error:
        _LOGGER.warning("Unable to parse %s: %s", payload.template, error)
        raise


def _parse_standard_template(payload: TemplatePayload) -> list[ParsedWeapon]:
    header, field_map = _find_header(payload.wikitext)
    required = (
        _CRAZY_SLOTS_REQUIRED_FIELDS
        if payload.template == _CRAZY_SLOTS_TEMPLATE
        else _STANDARD_REQUIRED_FIELDS
    )
    if not required <= set(field_map.values()):
        raise WeaponSourceError("Malformed weapon source.")

    weapon_type = "Unknown"
    parsed: list[ParsedWeapon] = []
    skipped = 0
    for row in _iter_table_rows(payload.wikitext):
        cells = _split_top_level_cells(row)
        if _is_category_row(row, cells):
            heading = _normalize_weapon_type(cells[0])
            if heading and payload.template != "AllHybridWeapons":
                weapon_type = heading
            continue
        if len(cells) != len(header):
            skipped += 1
            continue
        values = {
            field: _normalize_cell(cells[index])
            for index, field in field_map.items()
        }
        canonical = _canonical_standard_cells(values, payload.template)
        if not canonical[0]:
            skipped += 1
            continue
        parsed.append(
            ParsedWeapon(
                source_template=payload.template,
                source_class=payload.weapon_class,
                weapon_type=weapon_type,
                cells=canonical,
            )
        )

    if skipped:
        _LOGGER.warning(
            "Skipped %d malformed row(s) in %s.",
            skipped,
            payload.template,
        )
    if not parsed:
        raise WeaponSourceError("Required weapon source has no valid rows.")
    return parsed


def _parse_offhand_template(payload: TemplatePayload) -> list[ParsedWeapon]:
    """Map only the three documented Offhand table layouts to lookup-only cells."""
    parsed: list[ParsedWeapon] = []
    seen_captions: set[str] = set()
    skipped = 0
    for block in _TABLE_BLOCK.findall(payload.wikitext):
        caption_lines = [
            line for line in block.splitlines() if line.lstrip().startswith("|+")
        ]
        if not caption_lines:
            continue
        caption = _normalize_cell(caption_lines[0].lstrip()[2:])
        schema = _OFFHAND_TABLE_SCHEMAS.get(caption)
        if schema is None:
            continue
        if caption in seen_captions:
            raise WeaponSourceError("Malformed weapon source.")
        seen_captions.add(caption)

        weapon_type, adapter, required = schema
        header, field_map = _find_header(block)
        if not required <= set(field_map.values()):
            raise WeaponSourceError("Malformed weapon source.")

        table_count = 0
        for row in _iter_table_rows(block):
            cells = _split_top_level_cells(row)
            if _is_category_row(row, cells):
                continue
            if len(cells) != len(header):
                skipped += 1
                continue
            values = {
                field: _normalize_cell(cells[index])
                for index, field in field_map.items()
            }
            canonical = _canonical_offhand_cells(values, adapter)
            if not canonical[0]:
                skipped += 1
                continue
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
    if skipped:
        _LOGGER.warning(
            "Skipped %d malformed row(s) in %s.",
            skipped,
            payload.template,
        )
    return parsed


def _iter_table_rows(block: str) -> Iterator[str]:
    for raw_row in _ROW_BOUNDARY.split(block):
        row = raw_row.split("\n|}", 1)[0].strip()
        if not row or row.startswith(("{|", "|+")):
            continue
        if any(line.lstrip().startswith("{|") for line in row.splitlines()):
            continue
        if row.lstrip().startswith("!"):
            continue
        yield row


def _find_header(wikitext: str) -> tuple[list[str], dict[int, str]]:
    for line in wikitext.splitlines():
        if not line.lstrip().startswith("!"):
            continue
        cells = _header_cells(line)
        if cells is None:
            continue
        field_map = _map_headers(cells)
        if "Name" in field_map.values():
            return cells, field_map
    raise WeaponSourceError("Malformed weapon source.")


def _header_cells(line: str) -> list[str] | None:
    for delimiter in ("!!", "||"):
        cells = _split_top_level_cells(line, delimiter=delimiter)
        if len(cells) > 1:
            return cells
    return None


def _map_headers(cells: list[str]) -> dict[int, str]:
    field_map: dict[int, str] = {}
    seen: set[str] = set()
    for index, cell in enumerate(cells):
        label = _normalize_cell(cell).casefold()
        if label in _IGNORED_HEADER_LABELS:
            continue
        field = _FIELD_ALIASES.get(label)
        if field is None:
            continue
        if field in seen:
            raise WeaponSourceError("Malformed weapon source.")
        seen.add(field)
        field_map[index] = field
    return field_map


def _canonical_standard_cells(
    values: Mapping[str, str],
    template: str,
) -> tuple[str, ...]:
    cells = [values.get(field, "") for field in _SOURCE_HEADER_CELLS]
    if template == _CRAZY_SLOTS_TEMPLATE and not cells[1]:
        cells[1] = "Crazy Slots"
    return tuple(cells)


def _canonical_offhand_cells(
    values: Mapping[str, str],
    adapter: str,
) -> tuple[str, ...]:
    cells = {field: "" for field in _SOURCE_HEADER_CELLS}
    cells["Name"] = values.get("Name", "")
    cells["Requirements"] = values.get("Requirements", "")
    if adapter == "posture":
        cells["Posture Damage"] = values.get("Posture Damage", "")
    else:
        for field in (
            "Base Damage",
            "Armor Penetration",
            "Chip Damage",
            "Posture Damage",
            "Range",
        ):
            cells[field] = values.get(field, "")
    return tuple(cells[field] for field in _SOURCE_HEADER_CELLS)


def _is_category_row(row: str, cells: list[str]) -> bool:
    return len(cells) == 1 and bool(_COLSPAN.search(row))


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
    value = _LEADING_PIPES.sub("", value)
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
