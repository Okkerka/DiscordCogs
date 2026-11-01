from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from deepwoken.weapon_source import (
    API_URL,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    TEMPLATE_SOURCES,
    USER_AGENT,
    FandomWeaponSource,
    TemplatePayload,
    WeaponSourceError,
    parse_template,
)


FIXTURES = Path(__file__).parent / "fixtures"


def payload(template: str, weapon_class: str, wikitext: str) -> TemplatePayload:
    return TemplatePayload(template, weapon_class, 123, "2026-08-27T00:00:00Z", wikitext)


class FakeResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"", chunks: list[bytes] | None = None):
        self.status = status
        self._chunks = list(chunks) if chunks is not None else [body]
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def read(self, limit: int = -1) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if limit < 0 or len(chunk) <= limit:
            return self._chunks.pop(0)
        self._chunks[0] = chunk[limit:]
        return chunk[:limit]


class FakeSession:
    def __init__(self, responses: list[FakeResponse | BaseException]):
        self._responses = responses
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(value: object, *, status: int = 200) -> FakeResponse:
    return FakeResponse(status=status, body=json.dumps(value).encode("utf-8"))


def fragmented_response(value: object) -> FakeResponse:
    body = json.dumps(value).encode("utf-8")
    return FakeResponse(chunks=[body[:5], body[5:19], body[19:]])


def revision_document() -> dict[str, object]:
    return {
        "query": {
            "pages": [
                {
                    "title": f"Template:{template}",
                    "revisions": [{"revid": index, "timestamp": f"2026-08-{index:02}T00:00:00Z"}],
                }
                for index, template in enumerate(TEMPLATE_SOURCES, start=1)
            ]
        }
    }


def revision_response() -> FakeResponse:
    return response(revision_document())


class WeaponSourceParserTests(unittest.TestCase):
    def test_fixture_rows_have_normalized_eleven_source_cells(self):
        expected = {
            "AllLightWeapons": ("Light", "Dagger", ("Whaling Knife", "Flareblood Kamas (Bleed)", "Soulwrought Dagger")),
            "AllMediumWeapons": ("Medium", "Sword", ("Wyrmtooth",)),
            "AllHeavyWeapons": ("Heavy", "Greatsword", ("Wyrmtooth",)),
        }
        fixture_names = {
            "AllLightWeapons": "light_weapons.wiki",
            "AllMediumWeapons": "medium_weapons.wiki",
            "AllHeavyWeapons": "heavy_weapons.wiki",
        }

        for template, (weapon_class, weapon_type, names) in expected.items():
            with self.subTest(template=template):
                rows = parse_template(payload(template, weapon_class, (FIXTURES / fixture_names[template]).read_text("utf-8")))
                self.assertEqual(tuple(row.cells[0] for row in rows), names)
                for row in rows:
                    self.assertEqual(row.source_class, weapon_class)
                    self.assertEqual(row.weapon_type, weapon_type)
                    self.assertEqual(len(row.cells), 11)

    def test_presentation_markup_keeps_visible_text_without_collapsible_variant(self):
        rows = parse_template(payload("AllLightWeapons", "Light", (FIXTURES / "light_weapons.wiki").read_text("utf-8")))
        whaling_knife = rows[0]
        flareblood_kamas = rows[1]
        soulwrought_dagger = rows[2]

        self.assertEqual(whaling_knife.cells[0], "Whaling Knife")
        self.assertNotIn("Alloyed Whaling Knife", whaling_knife.cells[0])
        self.assertEqual(whaling_knife.cells[1:4], ("40 LHT", "16", "LHT: 5"))
        self.assertEqual(flareblood_kamas.cells[0], "Flareblood Kamas (Bleed)")
        self.assertEqual(flareblood_kamas.cells[-1], "27.1 (+4.1 BLD)")
        self.assertEqual(soulwrought_dagger.cells[1], "Crazy Slots")

    def test_current_crazy_slots_schema_is_canonicalized(self):
        fixture = (FIXTURES / "crazy_slots_weapons.wiki").read_text("utf-8")

        rows = parse_template(payload("AllCrazySlotsWeapons", "Crazy Slots", fixture))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_class, "Crazy Slots")
        self.assertEqual(
            rows[0].cells,
            ("Soulwrought Dagger", "Crazy Slots", "19", "LHT: 9", "-", "-", "4", "6", "1.25x", "-", "31.8"),
        )

    def test_current_hybrid_schema_discards_only_type_metadata(self):
        fixture = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8")

        rows = parse_template(payload("AllHybridWeapons", "Hybrid", fixture))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_class, "Hybrid")
        self.assertEqual(rows[0].weapon_type, "Unknown")
        self.assertEqual(
            rows[0].cells,
            (
                "Wyrmtooth", "60 MED 40 HVY LVL 10", "20", "MED: 6.2 HVY: 4.2",
                "25%", "-", "7", "9", "0.93x", "-", "35.6",
            ),
        )

    def test_hybrid_type_metadata_column_is_optional(self):
        fixture = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8")
        without_metadata = fixture.replace("Hybrid Type!!", "").replace("Medium & Heavy||", "")

        rows = parse_template(payload("AllHybridWeapons", "Hybrid", without_metadata))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cells[0], "Wyrmtooth")
        self.assertEqual(rows[0].cells[3], "MED: 6.2 HVY: 4.2")

    def test_hybrid_schema_requires_every_statistical_header(self):
        fixture = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8")
        missing_scaling = fixture.replace("Scaling!!", "").replace("MED: 6.2 HVY: 4.2||", "")

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllHybridWeapons", "Hybrid", missing_scaling))

    def test_headers_are_mapped_by_name_not_position(self):
        header = (
            "!Name!!Scaling!!Requirements!!Base Damage!!Posture Damage!!Range!!"
            "Swing Speed!!Endlag!!Scaled Damage!!Armor Penetration!!Chip Damage!!"
            "Provides Talent(s)?"
        )
        row = (
            "| [[Reordered Dagger]] || LHT: 5 || 10 LHT || 12 || 2 || 6 || "
            "1.2x || - || 18 || - || - || -"
        )
        text = "{|\n|-\n" + header + "\n|-\n" + row + "\n|}"

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(
            rows[0].cells,
            ("Reordered Dagger", "10 LHT", "12", "LHT: 5", "-", "-", "2", "6", "1.2x", "-", "18"),
        )

    def test_metadata_and_unknown_columns_are_ignored(self):
        header = (
            "!Name!!Requirements!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!"
            "Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage!!"
            "Provides Talent(s)?!!Internal ID"
        )
        row = (
            "| [[Valid Dagger]] || 10 LHT || 12 || LHT: 5 || - || - || 2 || 6 || "
            "1.2x || - || 18 || - || secret-id"
        )
        text = "{|\n|-\n" + header + "\n|-\n" + row + "\n|}"

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(
            rows[0].cells,
            ("Valid Dagger", "10 LHT", "12", "LHT: 5", "-", "-", "2", "6", "1.2x", "-", "18"),
        )
        self.assertNotIn("secret-id", rows[0].cells)

    def test_current_offhand_tables_are_canonicalized_as_lookup_only_rows(self):
        fixture = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8")

        rows = parse_template(payload("AllOffhandWeapons", "Offhand", fixture))

        self.assertEqual(tuple(row.source_class for row in rows), ("Offhand",) * 3)
        self.assertEqual(
            tuple(row.weapon_type for row in rows),
            ("Shield", "Parrying Dagger", "Offhand Pistol"),
        )
        self.assertEqual(
            tuple(row.cells for row in rows),
            (
                ("Targe", "10 FTD", "", "", "", "", "4", "", "", "", ""),
                ("Parrying Dagger", "10 AGI", "", "", "", "", "20%", "", "", "", ""),
                ("Silversix", "N/A", "10 (8)", "", "-", "-", "1", "10", "", "", ""),
            ),
        )

    def test_offhand_unknown_tables_are_ignored(self):
        fixture = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8")
        extra = "{|\n|+[[Trinkets|Trinkets]]\n|-\n!Name\n|-\n| Lucky Charm\n|}\n"

        rows = parse_template(payload("AllOffhandWeapons", "Offhand", extra + fixture))

        self.assertEqual(tuple(row.cells[0] for row in rows), ("Targe", "Parrying Dagger", "Silversix"))

    def test_offhand_required_headers_and_captions_are_validated(self):
        fixture = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8")
        renamed_header = fixture.replace("Max Posture Bonus", "Posture Value")
        renamed_caption = fixture.replace(">Shields</div>", ">Shield Items</div>")

        for text in (renamed_header, renamed_caption):
            with self.subTest(text=text[:40]):
                with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
                    parse_template(payload("AllOffhandWeapons", "Offhand", text))

    def test_offhand_malformed_rows_are_skipped_when_a_valid_row_exists(self):
        fixture = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8")
        malformed = "| [[Broken Shield]] || 10 FTD || 4 || - || extra"
        text = fixture.replace("|[[Targe]]||10 FTD||4|| -", malformed + "\n|-\n|[[Targe]]||10 FTD||4|| -")

        rows = parse_template(payload("AllOffhandWeapons", "Offhand", text))

        self.assertEqual(tuple(row.cells[0] for row in rows), ("Targe", "Parrying Dagger", "Silversix"))

    def test_reordered_crazy_slots_requirements_column_is_accepted(self):
        header = (
            "!Name!!Requirements!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!"
            "Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage!!Provides Talent(s)?"
        )
        row = "| [[Soulwrought Dagger]] || Crazy Slots || 19 || LHT: 9 || - || - || 4 || 6 || 1.25x || - || 31.8 || -"
        text = "{|\n|-\n" + header + "\n|-\n" + row + "\n|}"

        rows = parse_template(payload("AllCrazySlotsWeapons", "Crazy Slots", text))

        self.assertEqual(rows[0].cells[1], "Crazy Slots")
        self.assertEqual(rows[0].cells[4], "-")

    def test_duplicate_canonical_headers_are_rejected(self):
        header = (
            "!Name!!Penetration!!Armor Penetration!!Base Damage!!Scaling!!Chip Damage!!"
            "Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage"
        )
        row = "| [[Valid Dagger]] || - || - || 12 || LHT: 5 || - || 2 || 6 || 1.2x || - || 18"
        text = "{|\n|-\n" + header + "\n|-\n" + row + "\n|}"

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))

    def test_standard_template_rejects_renamed_required_header(self):
        fixture = (FIXTURES / "light_weapons.wiki").read_text("utf-8")
        renamed = fixture.replace("Base Damage", "Weapon Damage", 1)

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllLightWeapons", "Light", renamed))

    def test_crazy_slots_row_with_an_extra_cell_is_skipped(self):
        fixture = (FIXTURES / "crazy_slots_weapons.wiki").read_text("utf-8")
        malformed = fixture.replace("||31.8|| -", "||31.8|| - ||unexpected")

        with self.assertRaisesRegex(WeaponSourceError, r"^Required weapon source has no valid rows\.$"):
            parse_template(payload("AllCrazySlotsWeapons", "Crazy Slots", malformed))

    def test_malformed_rows_are_skipped_before_valid_rows(self):
        _header, valid_table = self._table_with_header_category_and_valid_row()

        for cell_count in (9, 10, 13):
            with self.subTest(cell_count=cell_count):
                malformed = "| " + " || ".join(
                    ["[[Malformed Weapon]]", *[str(index) for index in range(cell_count - 1)]]
                )
                text = valid_table.replace(
                    "\n|-\n| [[Valid Dagger]]",
                    "\n|-\n" + malformed + "\n|-\n| [[Valid Dagger]]",
                )
                rows = parse_template(payload("AllLightWeapons", "Light", text))
                self.assertEqual(tuple(row.cells[0] for row in rows), ("Valid Dagger",))

    def test_template_with_only_malformed_rows_raises_no_valid_rows(self):
        _header, valid_table = self._table_with_header_category_and_valid_row()
        malformed = "| " + " || ".join(["[[Malformed Weapon]]", *[str(index) for index in range(9)]])
        text = valid_table.replace("\n|-\n| [[Valid Dagger]]", "\n|-\n" + malformed)

        with self.assertRaisesRegex(WeaponSourceError, r"^Required weapon source has no valid rows\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))

    def test_bang_prefixed_row_is_treated_as_a_header_and_skipped(self):
        _header, valid_table = self._table_with_header_category_and_valid_row()
        malformed = "! " + " || ".join(["[[Weapon]]", *[str(index) for index in range(9)]])
        text = valid_table.replace(
            "\n|-\n| [[Valid Dagger]]",
            "\n|-\n" + malformed + "\n|-\n| [[Valid Dagger]]",
        )

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(tuple(row.cells[0] for row in rows), ("Valid Dagger",))

    def test_ten_column_data_without_requirements_is_rejected_for_standard_templates(self):
        header = (
            "!Name!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!Posture Damage!!"
            "Range!!Swing Speed!!Endlag!!Scaled Damage"
        )
        row = "| " + " || ".join(["[[Weapon]]", "19", "LHT: 9", "-", "-", "4", "6", "1.25x", "-", "31.8"])
        text = "{|\n" + header + "\n|-\n" + row + "\n|}"

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))

    def test_expected_header_and_colspan_category_are_controls(self):
        _header, text = self._table_with_header_category_and_valid_row()

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(rows[0].weapon_type, "Dagger")
        self.assertEqual(rows[0].cells[0], "Valid Dagger")

    def test_category_colspan_width_is_not_hardcoded(self):
        _header, text = self._table_with_header_category_and_valid_row()
        text = text.replace('colspan="12"', 'colspan="99"')

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(rows[0].weapon_type, "Dagger")

    def test_required_template_without_weapon_rows_is_rejected(self):
        header, _table = self._table_with_header_category_and_valid_row()
        text = "{|\n|-\n" + header + "\n|-\n! colspan=\"12\" | Daggers\n|}"

        with self.assertRaisesRegex(WeaponSourceError, r"^Required weapon source has no valid rows\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))

    @staticmethod
    def _table_with_header_category_and_valid_row() -> tuple[str, str]:
        header = (
            "!Name!!Requirements!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!"
            "Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage!!Provides Talent(s)?"
        )
        category = '|colspan="12" style="background:#FE7316"|[[Daggers|<span>Daggers</span>]]'
        valid = (
            "| " + " || ".join(
                ["[[Valid Dagger]]", "10 LHT", "12", "LHT: 5", "-", "-", "2", "6", "1.2x", "-", "18", "-"]
            )
        )
        return header, "{|\n|-\n" + header + "\n|-\n" + category + "\n|-\n" + valid + "\n|}"


class FandomWeaponSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def source(self, responses: list[FakeResponse | BaseException]) -> tuple[FandomWeaponSource, FakeSession]:
        session = FakeSession(responses)
        return FandomWeaponSource(session_factory=lambda: session), session

    async def test_fetches_payloads_in_template_order_with_bounded_requests(self):
        source, session = self.source(
            [revision_response()]
            + [response({"expandtemplates": {"wikitext": f"table for {template}"}}) for template in TEMPLATE_SOURCES]
        )

        fetched = await source.fetch()

        self.assertEqual(
            tuple(item.template for item in fetched),
            (
                "AllLightWeapons",
                "AllMediumWeapons",
                "AllHeavyWeapons",
                "AllHybridWeapons",
                "AllElementalWeapons",
                "AllCrazySlotsWeapons",
                "AllExclusiveWeapons",
                "AllOffhandWeapons",
            ),
        )
        self.assertEqual(tuple(item.template for item in fetched), tuple(TEMPLATE_SOURCES))
        self.assertEqual(tuple(item.weapon_class for item in fetched), tuple(TEMPLATE_SOURCES.values()))
        self.assertEqual([request["url"] for request in session.requests], [API_URL] * (len(TEMPLATE_SOURCES) + 1))
        for request in session.requests:
            self.assertEqual(request["headers"], {"User-Agent": USER_AGENT})
            self.assertEqual(request["timeout"], REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(session.requests[0]["params"]["action"], "query")
        for request in session.requests[1:]:
            self.assertEqual(request["params"].get("action"), "expandtemplates")
            self.assertEqual(request["params"].get("prop"), "wikitext")
            self.assertEqual(request["params"].get("format"), "json")
            self.assertEqual(request["params"].get("formatversion"), 2)

    async def test_fetches_valid_fragmented_stream_responses(self):
        source, _session = self.source(
            [fragmented_response(revision_document())]
            + [fragmented_response({"expandtemplates": {"wikitext": template}}) for template in TEMPLATE_SOURCES]
        )

        fetched = await source.fetch()

        self.assertEqual(tuple(item.wikitext for item in fetched), tuple(TEMPLATE_SOURCES))

    async def test_fetch_rejects_oversized_fragmented_response(self):
        initial_document = json.dumps(revision_document()).encode("utf-8")
        responses = [
            FakeResponse(chunks=[initial_document, b" " * MAX_RESPONSE_BYTES]),
            *[response({"expandtemplates": {"wikitext": template}}) for template in TEMPLATE_SOURCES],
        ]
        source, _session = self.source(responses)

        with self.assertRaisesRegex(WeaponSourceError, r"^Weapon source response is too large\.$"):
            await source.fetch()

    async def test_fetch_rejects_invalid_remote_responses_without_leaking_content(self):
        cases = {
            "http status": [FakeResponse(status=500, body=b"private body")],
            "invalid json": [FakeResponse(body=b"not json")],
            "missing revision": [response({"query": {"pages": []}})],
            "timeout": [asyncio.TimeoutError()],
            "oversize": [FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1))],
            "missing wikitext": [revision_response(), *[response({"expandtemplates": {}}) for _ in TEMPLATE_SOURCES]],
        }
        for label, responses in cases.items():
            with self.subTest(label=label):
                source, _session = self.source(responses)
                with self.assertRaises(WeaponSourceError) as raised:
                    await source.fetch()
                self.assertLessEqual(len(str(raised.exception)), 80)
                self.assertNotIn("private body", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
