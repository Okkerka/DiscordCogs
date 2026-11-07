from __future__ import annotations

import asyncio
from difflib import get_close_matches
import logging
import math
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Sequence, TypeVar
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, LargeZipFile

import discord
from openpyxl import load_workbook
from openpyxl.utils.exceptions import IllegalCharacterError, InvalidFileException
from redbot.core import commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.menus import SimpleMenu

from .weapon_source import FandomWeaponSource, TemplatePayload, WeaponSourceError, parse_template
from .weapon_updater import (
    CandidateValidationError,
    ReconcileResult,
    WeaponRow,
    WORKBOOK_HEADERS,
    install_runtime_workbook,
    normalize_name,
    reconcile_weapons,
    validate_candidate,
    validate_workbook_semantics,
    write_workbook,
)


try:
    from lxml.etree import XMLSyntaxError as LxmlXMLSyntaxError
except ImportError:  # pragma: no cover - depends on OpenPyXL's optional parser
    XML_SYNTAX_ERRORS = (ParseError,)
else:
    XML_SYNTAX_ERRORS = (ParseError, LxmlXMLSyntaxError)


WORKBOOK_OPEN_ERRORS = (
    OSError,
    BadZipFile,
    LargeZipFile,
    InvalidFileException,
    KeyError,
    ValueError,
    TypeError,
    *XML_SYNTAX_ERRORS,
)
LOGGER = logging.getLogger(__name__)


class RuntimeWorkbookReadError(ValueError):
    """Raised when OpenPyXL cannot open a runtime workbook archive."""


class WorkbookSchemaError(ValueError):
    """Raised when a workbook cannot supply the runtime weapon schema."""

STAT_ALIASES = {
    "light": "LHT", "lht": "LHT", "medium": "MED", "med": "MED", "heavy": "HVY", "hvy": "HVY",
    "flame": "FIR", "fire": "FIR", "fir": "FIR", "frost": "ICE", "ice": "ICE",
    "lightning": "LTN", "thunder": "LTN", "ltn": "LTN", "wind": "WND", "gale": "WND", "wnd": "WND",
    "shadow": "SDW", "sdw": "SDW", "blood": "BLD", "bloodrend": "BLD", "bld": "BLD",
    "metal": "MTL", "mtl": "MTL", "ironsing": "MTL", "iron": "MTL",
    "strength": "STR", "str": "STR", "fortitude": "FTD", "ftd": "FTD",
    "agility": "AGI", "agi": "AGI", "intelligence": "INT", "int": "INT", "charisma": "CHA", "cha": "CHA",
    "willpower": "WLL", "will": "WLL", "wll": "WLL", "mind": "MND", "body": "BDY", "bdy": "BDY",
}
STAT_CODES = frozenset(STAT_ALIASES.values())
STAT_PATTERN = "|".join(sorted(STAT_CODES))
_PROFICIENCY_ALIASES = frozenset({"prof", "proficiency"})

RANKING_EXCLUSIONS = frozenset({
    "Ebonshard Lexicon", "The Rock", "The Endless Wave", "Unsung Scythern", "Worldpainter Brush",
    "Keyblade", "Soulshot", "Metal Greatsword", "Prototype Railblade", "Par's Glaive", "Saintsblade",
    "Ferractine", "Formless Shard", "Handcuffs", "Sovereign Bangle",
})
_NORMALIZED_RANKING_EXCLUSIONS = frozenset(
    normalize_name(name) for name in RANKING_EXCLUSIONS
)
RANKING_PAGE_SIZE = 15
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FOOTER_LIMIT = 2048
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_TOTAL_LIMIT = 6000
DISCORD_MESSAGE_LIMIT = 2000
LOOKUP_FIELDS = (
    ("Requirements", "Requirements"),
    ("Base Damage", "Base Damage"),
    ("Scaled Damage", "Scaled Damage"),
    ("Scaling", "Scaling"),
    ("Armor Penetration", "Armor Penetration"),
    ("Chip Damage", "Chip Damage"),
    ("Posture Damage", "Posture Damage"),
    ("Range", "Range"),
    ("Swing Speed", "Swing Speed"),
    ("Endlag", "Endlag"),
    ("Tags", "Tags"),
)
LOOKUP_FIELD_COUNT = 1 + len(LOOKUP_FIELDS)
LOOKUP_FIELD_VALUE_LIMIT = min(
    400,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TOTAL_LIMIT // LOOKUP_FIELD_COUNT,
)
LOOKUP_FOOTER = "Example: [p]dwweapon heavy 100 medium 100 prof 6"
_USAGE_GUIDE = (
    "**Deepwoken weapon lookup & ranking**\n\n"
    "**Look up a weapon**\n"
    "`[p]dwweapon Enforcer's Axe`\n"
    "Case-insensitive; ignores formatting and tolerates small typos.\n\n"
    "**Rank a build**\n"
    "`[p]dwweapon heavy 100 medium 100 prof 6`\n"
    "Use light/medium/heavy, attunements (flame, frost, thunder, wind, shadow, "
    "blood, metal or ironsing), and attributes. Stats are 0-100 and proficiency "
    "is 0-6. Unlisted stats count as 0 and numeric requirements are enforced. "
    "DPS = M1 / (0.5 / swing speed + endlag).\n\n"
    "**Resistance damage**\n"
    "`[p]dwweapon heavy 100 medium 100 prof 6 res 35 50`\n"
    "`res` takes one or two resistance percentages. Each result shows normal PEN "
    "and 3★ PEN damage. 3★ adds +15% PEN; PEN caps at 50%, and effective "
    "resistance is `RES * (1 - PEN/100)`.\n\n"
    "**Sorting**\n"
    "`... res 35 50 sort 50` sorts by 50% RES DPS (normal PEN).\n"
    "`... res 35 50 sort 50 m1` sorts by 50% RES M1 damage with 3★ PEN.\n"
    "Without `sort`, results use the first `res` value. The `sort` target must "
    "match one of the `res` values.\n\n"
    "Aliases: `[p]dw`, `[p]weapon`. Bot owner: `[p]dwweapon updatelist`, "
    "`[p]dwreload`."
)
_RESISTANCE_TOKENS = frozenset({"res", "resist", "resistance"})
_SORT_TOKEN = "sort"
_M1_SORT_TOKEN = "m1"
_MAX_RESISTANCE_VALUES = 2
_PENETRATION_CAP = 50.0
_THREE_STAR_PENETRATION_BONUS = 15.0
_RESISTANCE_PAGE_SIZE = 8
_FUZZY_LOOKUP_CUTOFF = 0.8
_FUZZY_LOOKUP_MIN_LENGTH = 5
_LOOKUP_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
MULTIPLE_MATCH_NAME_LIMIT = 160
UPDATE_CONFLICT_SAMPLE_SIZE = 3
UPDATE_CONFLICT_NAME_LIMIT = 80
LOG_DETAIL_LIMIT = 160
_ThreadResult = TypeVar("_ThreadResult")


class PublicSimpleMenu(SimpleMenu):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True


class Deepwoken(commands.Cog):
    """Deepwoken weapon lookup and valid multi-stat sustained-DPS comparison."""

    def __init__(self, bot):
        self.bot = bot
        self.bundled_workbook_path = Path(__file__).parent / "data" / "weapons.xlsx"
        self.runtime_data_path = Path(cog_data_path(self))
        self.runtime_workbook_path = self.runtime_data_path / "weapons.xlsx"
        self.backup_workbook_path = self.runtime_data_path / "weapons.backup.xlsx"
        self._update_lock = asyncio.Lock()
        self._weapon_source = FandomWeaponSource()
        self.weapons = self._load_preferred_weapons()

    @staticmethod
    def _number(value: Any) -> float | None:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value) if value is not None else "")
        return float(match.group()) if match else None

    @staticmethod
    def _requirements(value: Any) -> dict[str, int]:
        text = str(value or "").upper()
        return {
            stat: int(amount)
            for amount, stat in re.findall(rf"(\d+)\s*({STAT_PATTERN})", text)
        }

    @staticmethod
    def _scaling(value: Any) -> dict[str, float]:
        return {
            stat: float(amount)
            for stat, amount in re.findall(r"([A-Z]{2,4})\s*:\s*(\d+(?:\.\d+)?)", str(value or "").upper())
        }

    @staticmethod
    def _integer(value: str) -> int | None:
        if not re.fullmatch(r"-?[0-9]+", value):
            return None
        try:
            return int(value)
        except ValueError:
            # Python limits integer conversion length; a slash string can exceed it.
            return None

    @staticmethod
    def _looks_like_stat_query(args: tuple[str, ...]) -> bool:
        if len(args) < 2 or args[0].casefold() not in STAT_ALIASES:
            return False
        numeric_token = args[1].lstrip("+-")
        looks_numeric = numeric_token.replace(".", "", 1).isnumeric()
        has_proficiency = any(token.casefold() in {"prof", "proficiency"} for token in args[2:])
        return looks_numeric or has_proficiency

    def _row_score(self, row: dict[str, Any]) -> int:
        requirements = str(row.get("Requirements") or "")
        scaling = str(row.get("Scaling") or "")
        score = int(bool(self._requirements(requirements))) * 4
        score += int("CRAZY SLOTS" in requirements.upper()) * 4
        score += int(":" in scaling) * 4
        score += int(self._number(row.get("Base Damage")) is not None) * 2
        score += int("x" in str(row.get("Swing Speed") or "").lower()) * 2
        return score

    def _load_preferred_weapons(self) -> list[dict[str, Any]]:
        """Load the valid runtime workbook when available, else bundled data."""
        rows, path = self._read_preferred_weapons()
        self.workbook_path = path
        return rows

    def _read_preferred_weapons(self) -> tuple[list[WeaponRow], Path]:
        """Read a complete snapshot without publishing state from a worker thread."""
        if self.runtime_workbook_path.exists():
            try:
                weapons = self._load_weapons(self.runtime_workbook_path)
            except (RuntimeWorkbookReadError, WorkbookSchemaError) as error:
                LOGGER.warning(
                    "Ignoring runtime weapons workbook (%s); using bundled fallback.",
                    type(error).__name__,
                )
            else:
                return weapons, self.runtime_workbook_path

        weapons = self._load_weapons(self.bundled_workbook_path)
        return weapons, self.bundled_workbook_path

    def _load_weapons(self, path: Path) -> list[dict[str, Any]]:
        """Load and deduplicate weapon rows from one fixed workbook path."""
        try:
            workbook = load_workbook(path, read_only=True, data_only=False)
        except WORKBOOK_OPEN_ERRORS as error:
            raise RuntimeWorkbookReadError("Unable to read workbook archive.") from error
        try:
            try:
                sheet = workbook["All Weapons"]
                headers = tuple(str(cell.value or "").strip() for cell in next(sheet.iter_rows(max_row=1)))
            except (KeyError, StopIteration) as error:
                raise WorkbookSchemaError("Workbook is missing the weapon worksheet or headers.") from error
            if headers != WORKBOOK_HEADERS:
                raise WorkbookSchemaError("Workbook has an unexpected weapon schema.")

            best_rows: dict[str, tuple[int, dict[str, Any]]] = {}
            blank_name_rows: list[dict[str, Any]] = []
            for values in sheet.iter_rows(min_row=2, values_only=True):
                row = dict(zip(headers, values))
                name = str(row.get("Name") or "").strip()
                if not name:
                    if any(str(value or "").strip() for value in values):
                        blank_name_rows.append(row)
                    continue
                key = normalize_name(name)
                score = self._row_score(row)
                current = best_rows.get(key)
                if current is None or score > current[0]:
                    best_rows[key] = (score, row)
            rows = [*blank_name_rows, *(row for _score, row in best_rows.values())]
            try:
                validate_workbook_semantics(
                    rows,
                    recognized_stats=frozenset(STAT_ALIASES.values()),
                    ranking_exclusions=RANKING_EXCLUSIONS,
                )
            except CandidateValidationError as error:
                raise WorkbookSchemaError("Workbook contains invalid weapon data.") from error
            return rows
        except WorkbookSchemaError:
            raise
        except WORKBOOK_OPEN_ERRORS as error:
            raise RuntimeWorkbookReadError("Unable to read workbook worksheet.") from error
        finally:
            workbook.close()

    @classmethod
    def _extract_resistances(
        cls,
        args: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[int, ...]] | None:
        """Split optional `res <percent> [percent]` modifiers from a stat query."""
        remaining: list[str] = []
        resistances: list[int] = []
        index = 0
        while index < len(args):
            token = args[index].casefold()
            if token not in _RESISTANCE_TOKENS:
                remaining.append(args[index])
                index += 1
                continue
            index += 1
            values: list[int] = []
            while index < len(args):
                value = cls._integer(args[index].removesuffix("%"))
                if value is None:
                    break
                values.append(value)
                index += 1
            if not values or any(not 0 <= value <= 100 for value in values):
                return None
            resistances.extend(values)
        if len(resistances) > _MAX_RESISTANCE_VALUES:
            return None
        return tuple(remaining), tuple(dict.fromkeys(resistances))

    @classmethod
    def _extract_resistance_sort(
        cls,
        args: tuple[str, ...],
        resistances: tuple[int, ...],
    ) -> tuple[tuple[str, ...], int | None, bool] | None:
        """Split an optional `sort <requested resistance> [m1]` modifier."""
        if not resistances:
            return tuple(args), None, False
        remaining: list[str] = []
        sort_resistance: int | None = None
        sort_by_m1 = False
        index = 0
        while index < len(args):
            if args[index].casefold() != _SORT_TOKEN:
                remaining.append(args[index])
                index += 1
                continue
            if sort_resistance is not None or index + 1 >= len(args):
                return None
            value = cls._integer(args[index + 1].removesuffix("%"))
            if value is None or value not in resistances:
                return None
            sort_resistance = value
            index += 2
            if index < len(args) and args[index].casefold() == _M1_SORT_TOKEN:
                sort_by_m1 = True
                index += 1
        return tuple(remaining), sort_resistance, sort_by_m1

    def _parse_stat_query(self, args: tuple[str, ...]) -> tuple[dict[str, int], int] | None:
        if len(args) < 2:
            return None
        first_stat = STAT_ALIASES.get(args[0].casefold())
        if first_stat is None:
            return None

        # Legacy shorthand: "<stat> <value> <proficiency>".
        if len(args) == 3:
            stat_value = self._integer(args[1])
            prof_value = self._integer(args[2])
            if stat_value is not None and prof_value is not None:
                return self._validated_stat_query(
                    {first_stat: stat_value}, prof_value, first_stat
                )

        stats: dict[str, int] = {}
        prof = 0
        index = 0
        while index < len(args):
            token = args[index].casefold()
            if token in _PROFICIENCY_ALIASES:
                if index + 1 >= len(args):
                    return None
                value = self._integer(args[index + 1])
                if value is None:
                    return None
                prof = value
                index += 2
                continue
            stat = STAT_ALIASES.get(token)
            if stat is None or index + 1 >= len(args):
                return None
            value = self._integer(args[index + 1])
            if value is None:
                return None
            stats[stat] = value
            index += 2
        return self._validated_stat_query(stats, prof, first_stat)

    @staticmethod
    def _validated_stat_query(
        stats: dict[str, int],
        prof: int,
        required_stat: str,
    ) -> tuple[dict[str, int], int] | None:
        if required_stat not in stats or not 0 <= prof <= 6:
            return None
        if any(not 0 <= value <= 100 for value in stats.values()):
            return None
        return stats, prof

    def _meets_requirements(self, row: dict[str, Any], stats: dict[str, int]) -> bool:
        return all(stats.get(stat, 0) >= required for stat, required in self._requirements(row.get("Requirements")).items())

    def _damage(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        base = self._number(row.get("Base Damage"))
        if base is None:
            return None
        damage = base
        for stat, scale in self._scaling(row.get("Scaling")).items():
            damage += 0.00075 * base * scale * stats.get(stat, 0) * (1 + prof * 0.065)
        return damage

    def _dps(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        return self._dps_for_damage(row, self._damage(row, stats, prof))

    def _dps_for_damage(self, row: WeaponRow, damage: float | None) -> float | None:
        speed = self._number(row.get("Swing Speed"))
        endlag = self._number(row.get("Endlag")) or 0
        if (damage is None or speed is None or speed <= 0 or endlag < 0
                or not all(math.isfinite(value) for value in (damage, speed, endlag))):
            return None
        # Swing Speed is a multiplier over a 0.5-second baseline swing, not
        # a duration in seconds. Listed Endlag is added to the cycle.
        duration = 0.5 / speed + endlag
        if duration <= 0:
            return None
        dps = damage / duration
        return dps if math.isfinite(dps) else None

    @classmethod
    def _penetration(cls, row: dict[str, Any]) -> float:
        value = cls._number(row.get("Armor Penetration"))
        if value is None or not math.isfinite(value):
            return 0.0
        return min(max(value, 0.0), _PENETRATION_CAP)

    @staticmethod
    def _damage_after_resistance(
        damage: float,
        resistance: float,
        penetration: float,
    ) -> float:
        resistance_fraction = min(max(resistance, 0.0), 100.0) / 100.0
        penetration_fraction = min(max(penetration, 0.0), _PENETRATION_CAP) / 100.0
        return damage * (1.0 - resistance_fraction * (1.0 - penetration_fraction))

    def _rankable(self, row: dict[str, Any]) -> bool:
        if normalize_name(row.get("Name")) in _NORMALIZED_RANKING_EXCLUSIONS:
            return False
        if str(row.get("Weapon Class") or "") in {"Special / Other", "Elemental", "Fighting Style"}:
            return False
        requirements = self._requirements(row.get("Requirements"))
        return not any(value > 100 for value in requirements.values())

    @commands.hybrid_command(name="dwweapon", aliases=["dw", "weapon"])
    async def dwweapon(self, ctx: commands.Context, *, query: str = "") -> None:
        """Look up a weapon or rank a build, e.g. heavy 100 medium 100 prof 6.

        Append `res 35 50` to a stat query to compare damage against those
        resistances, including normal and 3-star penetration. Add `sort 50`
        to order by DPS at one of the requested resistances, or `sort 50 m1`
        to order by 3-star M1 damage.

        Parameters
        ----------
        query: str
            Weapon name, build stats, or updatelist (bot owner only).
        """
        if ctx.interaction is not None:
            await ctx.defer()
        query = query.strip()
        if len(query) >= 2 and query[0] == query[-1] and query[0] in ("\"", "'"):
            query = query[1:-1]
        await self._weapon_query(ctx, *query.split())

    async def _weapon_query(self, ctx: commands.Context, *args: str) -> None:
        """Look up a weapon or rank every weapon usable with a multi-stat build."""
        if not args:
            await ctx.send(_USAGE_GUIDE)
            return
        if args[0].casefold() == "updatelist":
            if len(args) != 1:
                await ctx.send("Use `[p]dwweapon updatelist` with no extra arguments.")
                return
            await self._update_weapon_list(ctx)
            return
        extracted = self._extract_resistances(args)
        if extracted is None:
            await ctx.send(
                "Invalid resistance query. Use `res` followed by one or two values "
                "from 0 to 100, e.g. `res 35 50`."
            )
            return
        stat_args, resistances = extracted
        sort_args = self._extract_resistance_sort(stat_args, resistances)
        if sort_args is None:
            await ctx.send(
                "Invalid sort query. Use `sort` after `res` with one of the "
                "requested values, e.g. `res 35 50 sort 50 m1`."
            )
            return
        stat_args, sort_resistance, sort_by_m1 = sort_args
        parsed = self._parse_stat_query(stat_args)
        if parsed:
            await self._compare(
                ctx,
                *parsed,
                resistances=resistances,
                sort_resistance=sort_resistance,
                sort_by_m1=sort_by_m1,
            )
        elif resistances and not stat_args:
            await ctx.send(
                "Resistance calculations need a stat query, e.g. "
                "`[p]dwweapon heavy 100 medium 100 prof 6 res 35 50`."
            )
        elif self._looks_like_stat_query(stat_args):
            await ctx.send("Invalid stat query. Stats must be from 0 to 100 and proficiency must be from 0 to 6.")
        elif resistances:
            await ctx.send(
                "Resistance calculations need a valid stat query, e.g. "
                "`[p]dwweapon heavy 100 medium 100 prof 6 res 35 50`."
            )
        else:
            await self._lookup(ctx, " ".join(stat_args))

    def _ranking_pages(
        self,
        stats: dict[str, int],
        prof: int,
        *,
        weapons: Sequence[WeaponRow] | None = None,
        resistances: Sequence[int] = (),
        sort_resistance: int | None = None,
        sort_by_m1: bool = False,
    ) -> list[discord.Embed]:
        sort_target = (
            sort_resistance
            if sort_resistance in resistances
            else (resistances[0] if resistances else None)
        )
        ranked: list[tuple[float, float, float, float, float, WeaponRow]] = []
        for row in self.weapons if weapons is None else weapons:
            if not self._rankable(row):
                continue
            if not self._scaling(row.get("Scaling")).keys() & stats.keys():
                continue
            if not self._meets_requirements(row, stats):
                continue
            damage = self._damage(row, stats, prof)
            dps = self._dps_for_damage(row, damage)
            if damage is None or dps is None:
                continue
            penetration = self._penetration(row)
            star_penetration = min(
                penetration + _THREE_STAR_PENETRATION_BONUS,
                _PENETRATION_CAP,
            )
            if sort_by_m1:
                # Mirror the 3★ column: +15% PEN, capped like normal PEN.
                rank_value = (
                    self._damage_after_resistance(
                        damage,
                        sort_target,
                        star_penetration,
                    )
                    if sort_target is not None
                    else damage
                )
            else:
                rank_value = dps
                if sort_target is not None:
                    resisted = self._damage_after_resistance(
                        damage,
                        sort_target,
                        penetration,
                    )
                    rank_value = self._dps_for_damage(row, resisted)
                    if rank_value is None:
                        continue
            ranked.append(
                (rank_value, dps, damage, penetration, star_penetration, row)
            )
        if not ranked:
            return []
        ranked.sort(
            key=lambda item: (
                -item[0],
                normalize_name(item[5].get("Name")),
            )
        )
        stat_text = " | ".join(f"{stat} {value}" for stat, value in sorted(stats.items()))
        compact_stats = " · ".join(
            f"{stat} {value}" for stat, value in sorted(stats.items())
        )
        resistance_text = "/".join(f"{value}%" for value in resistances)
        page_size = _RESISTANCE_PAGE_SIZE if resistances else RANKING_PAGE_SIZE
        pages = []
        for start in range(0, len(ranked), page_size):
            lines = []
            if resistances:
                lines.extend([f"Stats: {compact_stats}", ""])
            for position, (
                _rank_value,
                dps,
                damage,
                penetration,
                star_penetration,
                row,
            ) in enumerate(
                ranked[start:start + page_size],
                start + 1,
            ):
                name = self._safe_discord_text(row.get("Name") or "Unknown", 128)
                label = self._safe_discord_text(row.get("Weapon Type") or "Unknown", 32)
                if str(row.get("Weapon Class") or "") == "Crazy Slots":
                    label += " | Crazy Slots"
                if resistances:
                    weapon_lines = [
                        f"**{position}. {name}** ({label}) · "
                        f"PEN {penetration:g}% (3★ {star_penetration:g}%)",
                        f"**Base** {dps:.2f} DPS / {damage:.2f} M1",
                    ]
                    for resistance in resistances:
                        normal_damage = self._damage_after_resistance(
                            damage,
                            resistance,
                            penetration,
                        )
                        star_damage = self._damage_after_resistance(
                            damage,
                            resistance,
                            star_penetration,
                        )
                        normal_dps = self._dps_for_damage(row, normal_damage)
                        star_dps = self._dps_for_damage(row, star_damage)
                        if normal_dps is None or star_dps is None:
                            continue
                        weapon_lines.append(
                            f"**{resistance}% RES** {normal_dps:.2f} DPS / "
                            f"{normal_damage:.2f} M1 · **3★** {star_dps:.2f} DPS / "
                            f"{star_damage:.2f} M1"
                        )
                    lines.extend(weapon_lines)
                    lines.append("")
                else:
                    dps_text = self._bounded_embed_text(f"{dps:.2f}", 24)
                    damage_text = self._bounded_embed_text(f"{damage:.2f}", 24)
                    lines.append(
                        f"`{position:>2}.` **{name}** ({label})\n"
                        f"`DPS:` {dps_text} | `M1:` {damage_text}"
                    )
            if resistances and lines and not lines[-1]:
                lines.pop()
            if resistances:
                title = self._bounded_embed_text(
                    f"Weapon ranking · {resistance_text} RES · Prof {prof}",
                    DISCORD_EMBED_TITLE_LIMIT,
                )
            else:
                title = self._bounded_embed_text(
                    f"Weapon ranking | {stat_text} | Prof {prof}",
                    DISCORD_EMBED_TITLE_LIMIT,
                )
            description = "\n".join(lines)
            embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())
            if resistances:
                sort_metric = "M1 (3★)" if sort_by_m1 else "DPS"
                footer = (
                    f"Showing {start + 1}-{min(start + page_size, len(ranked))} of {len(ranked)} · "
                    f"Sorted by {sort_target}% RES {sort_metric}. Each target shows normal and "
                    "3★ damage as DPS / M1. 3★ adds +15% PEN (cap 50%); effective "
                    "RES = RES * (1 - PEN/100). Bleed, crits, procs, enchants, and "
                    "talents are excluded."
                )
            else:
                footer = (
                    f"Showing {start + 1}-{min(start + page_size, len(ranked))} of {len(ranked)}. "
                    "Numeric stat requirements are enforced and unlisted stats count as 0; "
                    "non-stat unlock alternatives are not modeled. Crazy Slots and qualifying "
                    "hybrids are included. DPS = M1 / (0.5 / swing speed + endlag); bleed, "
                    "crits, procs, enchants, and talents are excluded. PEN and resistance "
                    "are excluded."
                )
            embed.set_footer(text=footer)
            pages.append(embed)
        return pages

    async def _compare(
        self,
        ctx: commands.Context,
        stats: dict[str, int],
        prof: int,
        *,
        resistances: Sequence[int] = (),
        sort_resistance: int | None = None,
        sort_by_m1: bool = False,
    ) -> None:
        pages = self._ranking_pages(
            stats,
            prof,
            resistances=resistances,
            sort_resistance=sort_resistance,
            sort_by_m1=sort_by_m1,
        )
        if not pages:
            await ctx.send("No rankable weapons meet those exact stats.")
            return
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await PublicSimpleMenu(pages).start(ctx)

    async def _update_weapon_list(self, ctx: commands.Context) -> None:
        if not await ctx.bot.is_owner(ctx.author):
            await ctx.send("Only the bot owner can update the weapon list.")
            return

        async with self._update_lock:
            candidate_path: Path | None = None
            try:
                payloads = await self._weapon_source.fetch()
                parsed_rows = tuple(
                    parsed
                    for payload in payloads
                    for parsed in parse_template(payload)
                )
                active_snapshot = tuple(dict(row) for row in self.weapons)
                result = reconcile_weapons(parsed_rows, active_snapshot)
                validate_candidate(
                    result.rows,
                    active_unique_count=len(active_snapshot),
                    source_unique_count=result.source_unique_count,
                    recognized_stats=STAT_CODES,
                    ranking_exclusions=RANKING_EXCLUSIONS,
                )
                representative_pages = self._ranking_pages(
                    {stat: 100 for stat in STAT_CODES},
                    6,
                    weapons=result.rows,
                )
                self._validate_ranking_page_sizes(representative_pages)

                self.runtime_data_path.mkdir(parents=True, exist_ok=True)
                runtime_data_path = self.runtime_data_path.resolve(strict=True)
                with NamedTemporaryFile(
                    delete=False,
                    dir=runtime_data_path,
                    suffix=".xlsx",
                ) as candidate_file:
                    candidate_path = Path(candidate_file.name)

                await self._run_blocking(write_workbook, candidate_path, result.rows)
                installed_rows = await self._run_blocking(
                    install_runtime_workbook,
                    candidate_path,
                    self.runtime_workbook_path,
                    self.backup_workbook_path,
                    self._load_weapons,
                    result.rows,
                    on_cancelled_success=self._activate_runtime_rows,
                )
                self._activate_runtime_rows(installed_rows)
                summary = self._update_summary(payloads, result, len(installed_rows))
            except asyncio.CancelledError:
                raise
            except (
                WeaponSourceError,
                CandidateValidationError,
                RuntimeWorkbookReadError,
                WorkbookSchemaError,
                IllegalCharacterError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
            ) as error:
                LOGGER.error(
                    "Deepwoken weapon list update failed (%s): %s",
                    type(error).__name__,
                    self._safe_log_detail(error),
                )
                await ctx.send("Weapon list update failed; the previous list is still active.")
                return
            finally:
                if candidate_path is not None:
                    try:
                        candidate_path.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.warning("Unable to remove a Deepwoken workbook candidate.")

            await ctx.send(summary)

    @staticmethod
    async def _run_blocking(
        function: Callable[..., _ThreadResult],
        *args: object,
        on_cancelled_success: Callable[[_ThreadResult], None] | None = None,
    ) -> _ThreadResult:
        """Defer caller cancellation until the worker has a terminal outcome.

        Repeated caller cancellations are absorbed while the worker is pending.
        A terminally cancelled worker has no result that can safely be published,
        so cancellation propagates immediately instead of re-awaiting it forever.
        """
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            while True:
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    if worker.done() and worker.cancelled():
                        break
                    continue
                except Exception as error:
                    LOGGER.error(
                        "A cancelled Deepwoken workbook worker also failed (%s): %s",
                        type(error).__name__,
                        Deepwoken._safe_log_detail(error),
                    )
                    break
                else:
                    if on_cancelled_success is not None:
                        on_cancelled_success(result)
                    break
            raise cancellation

    def _activate_runtime_rows(self, rows: list[WeaponRow]) -> None:
        """Publish rows only after the atomic installer has returned successfully."""
        self.weapons = rows
        self.workbook_path = self.runtime_workbook_path

    @staticmethod
    def _bounded_embed_text(value: object, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    @staticmethod
    def _safe_discord_text(value: object, limit: int, *, fallback: str = "N/A") -> str:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            text = fallback
        text = text.replace("\\", "\\\\")
        for character in ("*", "_", "`", "~", "|", ">"):
            text = text.replace(character, "\\" + character)
        text = text.replace("@", "@\u200b")
        return Deepwoken._bounded_embed_text(text, limit)

    @staticmethod
    def _safe_log_detail(error: BaseException) -> str:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(error))
        text = re.sub(r"\s+", " ", text).strip() or "No detail"
        return Deepwoken._bounded_embed_text(text, LOG_DETAIL_LIMIT)

    @staticmethod
    def _validate_ranking_page_sizes(pages: Sequence[discord.Embed]) -> None:
        for page in pages:
            footer = getattr(page.footer, "text", page.footer)
            if (
                len(page.title or "") > DISCORD_EMBED_TITLE_LIMIT
                or len(page.description or "") > DISCORD_EMBED_DESCRIPTION_LIMIT
                or len(footer or "") > DISCORD_EMBED_FOOTER_LIMIT
            ):
                raise CandidateValidationError("Candidate ranking output exceeds Discord limits.")

    @classmethod
    def _update_summary(
        cls,
        payloads: Sequence[TemplatePayload],
        result: ReconcileResult,
        active_count: int,
    ) -> str:
        revisions = ", ".join(str(revision) for revision in sorted({payload.revision_id for payload in payloads}))
        summary = (
            f"Weapon list updated. Revisions: {revisions}. Fetched: {result.fetched_count}; "
            f"added: {len(result.added)}; changed: {len(result.changed)}; "
            f"conflicts retained: {len(result.conflicts)}; "
            f"removals retained: {len(result.removals_retained)}; active: {active_count}."
        )
        if result.conflicts:
            sample = [
                cls._safe_summary_name(name)
                for name in result.conflicts[:UPDATE_CONFLICT_SAMPLE_SIZE]
            ]
            remainder = len(result.conflicts) - len(sample)
            summary += " Conflicts: " + ", ".join(sample)
            if remainder:
                summary += f" (+{remainder} more)"
            summary += "."
        return summary[:2000]

    @staticmethod
    def _safe_summary_name(value: object) -> str:
        text = re.sub(r"[^\w .,'()\-]", "?", str(value), flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip() or "Unknown"
        return Deepwoken._bounded_embed_text(text, UPDATE_CONFLICT_NAME_LIMIT)

    def _fuzzy_lookup_matches(self, query: str) -> list[dict[str, Any]]:
        """Return close name matches for minor spelling mistakes."""
        query_key = _LOOKUP_SUFFIX.sub("", query).strip()
        if len(query_key) < _FUZZY_LOOKUP_MIN_LENGTH:
            return []
        rows_by_key: dict[str, dict[str, Any]] = {}
        for row in self.weapons:
            key = _LOOKUP_SUFFIX.sub("", normalize_name(row.get("Name"))).strip()
            if key:
                rows_by_key.setdefault(key, row)
        close_keys = get_close_matches(
            query_key,
            rows_by_key,
            n=10,
            cutoff=_FUZZY_LOOKUP_CUTOFF,
        )
        return [rows_by_key[key] for key in close_keys]

    async def _lookup(self, ctx: commands.Context, query: str) -> None:
        query_key = normalize_name(query)
        exact = [row for row in self.weapons if normalize_name(row.get("Name")) == query_key]
        matches = exact or [
            row for row in self.weapons if query_key in normalize_name(row.get("Name"))
        ]
        if not matches:
            matches = self._fuzzy_lookup_matches(query_key)
        if not matches:
            await ctx.send("Weapon not found.")
            return
        matches.sort(key=lambda row: normalize_name(row.get("Name")))
        if len(matches) > 1:
            names = [
                self._safe_discord_text(
                    row.get("Name"), MULTIPLE_MATCH_NAME_LIMIT, fallback="Unknown"
                )
                for row in matches[:10]
            ]
            message = "Multiple matches: " + ", ".join(names)
            if len(matches) > len(names):
                message += f" (+{len(matches) - len(names)} more)"
            await ctx.send(message[:DISCORD_MESSAGE_LIMIT])
            return
        row = matches[0]
        embed = discord.Embed(
            title=self._safe_discord_text(
                row.get("Name") or "Unknown weapon",
                DISCORD_EMBED_TITLE_LIMIT,
                fallback="Unknown weapon",
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Class / Type",
            value=self._safe_discord_text(
                f"{row.get('Weapon Class') or '?'} / {row.get('Weapon Type') or '?'}",
                LOOKUP_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )
        for label, key in LOOKUP_FIELDS:
            value = row.get(key)
            embed.add_field(
                name=self._safe_discord_text(label, DISCORD_EMBED_FIELD_NAME_LIMIT),
                value=self._safe_discord_text(
                    "N/A" if value in (None, "") else value,
                    LOOKUP_FIELD_VALUE_LIMIT,
                ),
                inline=label != "Requirements",
            )
        embed.set_footer(text=LOOKUP_FOOTER)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="dwreload")
    @commands.is_owner()
    async def dwreload(self, ctx: commands.Context) -> None:
        """Reload the saved weapon workbook (bot owner only)."""
        if ctx.interaction is not None:
            await ctx.defer()
        async with self._update_lock:
            try:
                snapshot = await self._run_blocking(
                    self._read_preferred_weapons,
                    on_cancelled_success=self._activate_loaded_snapshot,
                )
            except (RuntimeWorkbookReadError, WorkbookSchemaError, OSError) as error:
                LOGGER.warning("Weapon reload failed: %s", self._safe_log_detail(error))
                await ctx.send("Weapon reload failed; the previous list is still active.")
                return
            self._activate_loaded_snapshot(snapshot)
        await ctx.send(f"Reloaded {len(self.weapons)} unique weapon rows.")

    def _activate_loaded_snapshot(self, snapshot: tuple[list[WeaponRow], Path]) -> None:
        self.weapons, self.workbook_path = snapshot
