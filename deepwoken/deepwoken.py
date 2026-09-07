from __future__ import annotations

import asyncio
import logging
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
    "metal": "MTL", "mtl": "MTL", "strength": "STR", "str": "STR", "fortitude": "FTD", "ftd": "FTD",
    "agility": "AGI", "agi": "AGI", "intelligence": "INT", "int": "INT", "charisma": "CHA", "cha": "CHA",
    "willpower": "WLL", "will": "WLL", "wll": "WLL", "mind": "MND", "body": "BDY", "bdy": "BDY",
}
STAT_PATTERN = "|".join(sorted(set(STAT_ALIASES.values())))

RANKING_EXCLUSIONS = {
    "Ebonshard Lexicon", "The Rock", "The Endless Wave", "Unsung Scythern", "Worldpainter Brush",
    "Keyblade", "Soulshot", "Metal Greatsword", "Prototype Railblade", "Par's Glaive", "Saintsblade",
    "Ferractine", "Formless Shard", "Handcuffs", "Sovereign Bangle",
}
RANKING_PAGE_SIZE = 15
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FOOTER_LIMIT = 2048
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_TOTAL_LIMIT = 6000
DISCORD_MESSAGE_LIMIT = 2000
LOOKUP_FIELD_COUNT = 12
LOOKUP_FIELD_VALUE_LIMIT = min(
    400,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TOTAL_LIMIT // LOOKUP_FIELD_COUNT,
)
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
        match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
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
        return int(value) if re.fullmatch(r"-?[0-9]+", value) else None

    @staticmethod
    def _looks_like_stat_query(args: tuple[str, ...]) -> bool:
        if len(args) < 2 or args[0].casefold() not in STAT_ALIASES:
            return False
        numeric_token = args[1].lstrip("+-")
        looks_numeric = numeric_token.replace(".", "", 1).isnumeric()
        has_proficiency = any(token.casefold() in {"prof", "proficiency"} for token in args[2:])
        return looks_numeric or has_proficiency

    def _row_score(self, row: dict[str, Any]) -> int:
        score = 0
        requirements = str(row.get("Requirements") or "")
        scaling = str(row.get("Scaling") or "")
        if self._requirements(requirements): score += 4
        if "CRAZY SLOTS" in requirements.upper(): score += 4
        if ":" in scaling: score += 4
        if self._number(row.get("Base Damage")) is not None: score += 2
        if "x" in str(row.get("Swing Speed") or "").lower(): score += 2
        return score

    def _load_preferred_weapons(self) -> list[dict[str, Any]]:
        """Load the valid runtime workbook when available, else bundled data."""
        if self.runtime_workbook_path.exists():
            try:
                weapons = self._load_weapons(self.runtime_workbook_path)
            except (RuntimeWorkbookReadError, WorkbookSchemaError) as error:
                LOGGER.warning(
                    "Ignoring runtime weapons workbook (%s); using bundled fallback.",
                    type(error).__name__,
                )
            else:
                self.workbook_path = self.runtime_workbook_path
                return weapons

        weapons = self._load_weapons(self.bundled_workbook_path)
        self.workbook_path = self.bundled_workbook_path
        return weapons

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

            best_rows: dict[str, dict[str, Any]] = {}
            blank_name_rows: list[dict[str, Any]] = []
            for values in sheet.iter_rows(min_row=2, values_only=True):
                row = dict(zip(headers, values))
                name = str(row.get("Name") or "").strip()
                if not name:
                    if any(str(value or "").strip() for value in values):
                        blank_name_rows.append(row)
                    continue
                key = name.casefold()
                if key not in best_rows or self._row_score(row) > self._row_score(best_rows[key]):
                    best_rows[key] = row
            rows = [*blank_name_rows, *best_rows.values()]
            try:
                validate_workbook_semantics(
                    rows,
                    recognized_stats=frozenset(STAT_ALIASES.values()),
                    ranking_exclusions=RANKING_EXCLUSIONS,
                )
            except CandidateValidationError as error:
                raise WorkbookSchemaError("Workbook contains invalid weapon data.") from error
            return rows
        finally:
            workbook.close()

    def _parse_stat_query(self, args: tuple[str, ...]) -> tuple[dict[str, int], int] | None:
        if len(args) < 2 or args[0].casefold() not in STAT_ALIASES:
            return None
        first_stat = STAT_ALIASES[args[0].casefold()]
        if len(args) == 3 and (stat_value := self._integer(args[1])) is not None and (prof_value := self._integer(args[2])) is not None:
            stats, prof = {first_stat: stat_value}, prof_value
            return (stats, prof) if 0 <= stats[first_stat] <= 100 and 0 <= prof <= 6 else None
        stats, index, prof = {}, 0, 0
        while index < len(args):
            token = args[index].casefold()
            if token in {"prof", "proficiency"}:
                if index + 1 >= len(args) or (value := self._integer(args[index + 1])) is None: return None
                prof, index = value, index + 2
            elif token in STAT_ALIASES and index + 1 < len(args) and (value := self._integer(args[index + 1])) is not None:
                stats[STAT_ALIASES[token]], index = value, index + 2
            else:
                return None
        return (stats, prof) if first_stat in stats and all(0 <= x <= 100 for x in stats.values()) and 0 <= prof <= 6 else None

    def _meets_requirements(self, row: dict[str, Any], stats: dict[str, int]) -> bool:
        return all(stats.get(stat, 0) >= required for stat, required in self._requirements(row.get("Requirements")).items())

    def _damage(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        base = self._number(row.get("Base Damage"))
        if base is None: return None
        damage = base
        for stat, scale in self._scaling(row.get("Scaling")).items():
            damage += 0.00075 * base * scale * stats.get(stat, 0) * (1 + prof * 0.065)
        return damage

    def _dps(self, row: dict[str, Any], stats: dict[str, int], prof: int) -> float | None:
        damage, speed = self._damage(row, stats, prof), self._number(row.get("Swing Speed"))
        if damage is None or speed is None or speed <= 0: return None
        return damage / ((1 / (speed * 2)) + (self._number(row.get("Endlag")) or 0))

    def _rankable(self, row: dict[str, Any]) -> bool:
        if str(row.get("Name") or "").strip() in RANKING_EXCLUSIONS: return False
        if str(row.get("Weapon Class") or "") in {"Special / Other", "Elemental", "Fighting Style"}: return False
        return not any(value > 100 for value in self._requirements(row.get("Requirements")).values())

    @commands.command(name="dwweapon", aliases=["dw", "weapon"])
    async def dwweapon(self, ctx: commands.Context, *args: str):
        """Look up a weapon or rank every weapon usable with a multi-stat build."""
        if not args:
            await ctx.send("Use `[p]dwweapon <name>` or `[p]dwweapon heavy 100 medium 100 prof 6`.")
            return
        if args[0].casefold() == "updatelist":
            if len(args) != 1:
                await ctx.send("Use `[p]dwweapon updatelist` with no extra arguments.")
                return
            await self._update_weapon_list(ctx)
            return
        parsed = self._parse_stat_query(args)
        if parsed:
            await self._compare(ctx, *parsed)
        elif self._looks_like_stat_query(args):
            await ctx.send("Invalid stat query. Stats must be from 0 to 100 and proficiency must be from 0 to 6.")
        else:
            await self._lookup(ctx, " ".join(args))

    def _ranking_pages(
        self,
        stats: dict[str, int],
        prof: int,
        *,
        weapons: Sequence[WeaponRow] | None = None,
    ) -> list[discord.Embed]:
        ranked = []
        for row in self.weapons if weapons is None else weapons:
            if not self._rankable(row): continue
            if not self._scaling(row.get("Scaling")).keys() & stats.keys(): continue
            if not self._meets_requirements(row, stats): continue
            damage, dps = self._damage(row, stats, prof), self._dps(row, stats, prof)
            if damage is not None and dps is not None: ranked.append((dps, damage, row))
        if not ranked:
            return []
        ranked.sort(key=lambda item: (-item[0], str(item[2].get("Name") or "").casefold()))
        stat_text = " | ".join(f"{stat} {value}" for stat, value in sorted(stats.items()))
        pages = []
        for start in range(0, len(ranked), RANKING_PAGE_SIZE):
            lines = []
            for position, (dps, damage, row) in enumerate(ranked[start:start + RANKING_PAGE_SIZE], start + 1):
                name = self._bounded_embed_text(row.get("Name") or "Unknown", 128)
                label = self._bounded_embed_text(row.get("Weapon Type") or "Unknown", 32)
                if str(row.get("Weapon Class") or "") == "Crazy Slots": label += " | Crazy Slots"
                dps_text = self._bounded_embed_text(f"{dps:.2f}", 24)
                damage_text = self._bounded_embed_text(f"{damage:.2f}", 24)
                lines.append(f"`{position:>2}.` **{name}** ({label})\n`DPS:` {dps_text} | `M1:` {damage_text}")
            title = self._bounded_embed_text(
                f"Weapon ranking | {stat_text} | Prof {prof}",
                DISCORD_EMBED_TITLE_LIMIT,
            )
            description = "\n".join(lines)
            embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())
            embed.set_footer(text=f"Showing {start + 1}-{min(start + RANKING_PAGE_SIZE, len(ranked))} of {len(ranked)}. Numeric stat requirements are enforced; non-stat unlock alternatives are not modeled. Crazy Slots and qualifying hybrids are included. DPS includes listed Endlag; bleed, crits, procs, enchants, talents, PEN, and resistance are excluded.")
            pages.append(embed)
        return pages

    async def _compare(self, ctx: commands.Context, stats: dict[str, int], prof: int):
        pages = self._ranking_pages(stats, prof)
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
                recognized_stats = frozenset(STAT_ALIASES.values())
                validate_candidate(
                    result.rows,
                    active_unique_count=len(active_snapshot),
                    source_unique_count=result.source_unique_count,
                    recognized_stats=recognized_stats,
                    ranking_exclusions=RANKING_EXCLUSIONS,
                )
                representative_pages = self._ranking_pages(
                    {stat: 100 for stat in recognized_stats},
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

    async def _lookup(self, ctx: commands.Context, query: str):
        query = query.casefold().strip()
        exact = [row for row in self.weapons if str(row.get("Name") or "").casefold() == query]
        matches = exact or [row for row in self.weapons if query in str(row.get("Name") or "").casefold()]
        if not matches:
            await ctx.send("Weapon not found.")
            return
        if len(matches) > 1:
            names = [
                self._safe_discord_text(row["Name"], MULTIPLE_MATCH_NAME_LIMIT, fallback="Unknown")
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
        fields = [("Requirements", "Requirements"), ("Base Damage", "Base Damage"), ("Scaled Damage", "Scaled Damage"), ("Scaling", "Scaling"), ("Armor Penetration", "Armor Penetration"), ("Chip Damage", "Chip Damage"), ("Posture Damage", "Posture Damage"), ("Range", "Range"), ("Swing Speed", "Swing Speed"), ("Endlag", "Endlag"), ("Tags", "Tags")]
        for label, key in fields:
            embed.add_field(
                name=self._safe_discord_text(label, DISCORD_EMBED_FIELD_NAME_LIMIT),
                value=self._safe_discord_text(
                    row.get(key) or "N/A",
                    min(LOOKUP_FIELD_VALUE_LIMIT, DISCORD_EMBED_FIELD_VALUE_LIMIT),
                ),
                inline=label != "Requirements",
            )
        embed.set_footer(text="Example: [p]dwweapon heavy 100 medium 100 prof 6")
        await ctx.send(embed=embed)

    @commands.command(name="dwreload")
    @commands.is_owner()
    async def dwreload(self, ctx: commands.Context):
        self.weapons = self._load_preferred_weapons()
        await ctx.send(f"Reloaded {len(self.weapons)} unique weapon rows.")
