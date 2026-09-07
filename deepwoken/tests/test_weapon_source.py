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

    def test_current_crazy_slots_ten_column_schema_is_canonicalized(self):
        fixture = (FIXTURES / "crazy_slots_weapons.wiki").read_text("utf-8")

        rows = parse_template(payload("AllCrazySlotsWeapons", "Crazy Slots", fixture))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_class, "Crazy Slots")
        self.assertEqual(
            rows[0].cells,
            ("Soulwrought Dagger", "Crazy Slots", "19", "LHT: 9", "-", "-", "4", "6", "1.25x", "-", "31.8"),
        )

    def test_current_hybrid_twelve_column_schema_discards_only_type_metadata(self):
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

    def test_hybrid_template_rejects_eleven_columns_that_omit_hybrid_type(self):
        fixture = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8")
        near_miss = fixture.replace("Hybrid Type!!", "").replace("Medium & Heavy||", "")

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllHybridWeapons", "Hybrid", near_miss))

    def test_hybrid_template_rejects_eleven_cell_row_under_exact_header(self):
        fixture = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8")
        near_miss = fixture.replace("||Medium & Heavy||", "||", 1)

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllHybridWeapons", "Hybrid", near_miss))

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

    def test_hybrid_and_offhand_near_miss_schemas_are_rejected(self):
        hybrid = (FIXTURES / "hybrid_weapons.wiki").read_text("utf-8").replace(
            "Hybrid Type!!Requirements",
            "Weapon Type!!Requirements",
        )
        offhand_header = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8").replace(
            "Max Posture Bonus",
            "Posture Bonus",
        )
        offhand_caption = (FIXTURES / "offhand_weapons.wiki").read_text("utf-8").replace(
            ">Shields</div>",
            ">Shield Items</div>",
        )

        for template, weapon_class, text in (
            ("AllHybridWeapons", "Hybrid", hybrid),
            ("AllOffhandWeapons", "Offhand", offhand_header),
            ("AllOffhandWeapons", "Offhand", offhand_caption),
        ):
            with self.subTest(template=template):
                with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
                    parse_template(payload(template, weapon_class, text))

    def test_ten_column_data_without_the_exact_crazy_slots_template_and_header_is_rejected(self):
        unknown_header = "!Name!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage"
        crazy_slots_header = "!Name!!Base Damage!!Scaling!!Penetration!!Chip Damage!!Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage"
        row = "| " + " || ".join(["[[Weapon]]", "19", "LHT: 9", "-", "-", "4", "6", "1.25x", "-", "31.8"])

        for template, header in (("AllCrazySlotsWeapons", unknown_header), ("AllLightWeapons", crazy_slots_header)):
            with self.subTest(template=template):
                text = "{|\n" + header + "\n|-\n" + row + "\n|}"
                with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
                    parse_template(payload(template, "Crazy Slots", text))

    def test_standard_template_rejects_renamed_header(self):
        fixture = (FIXTURES / "light_weapons.wiki").read_text("utf-8")
        renamed = fixture.replace("Base Damage", "Weapon Damage", 1)

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllLightWeapons", "Light", renamed))

    def test_crazy_slots_rejects_eleven_cell_data_row(self):
        fixture = (FIXTURES / "crazy_slots_weapons.wiki").read_text("utf-8")
        malformed = fixture.replace("||31.8\n|}", "||31.8||unexpected\n|}")

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllCrazySlotsWeapons", "Crazy Slots", malformed))

    def test_weapon_like_row_with_wrong_column_count_is_rejected(self):
        ten_cells = "|-\n| " + " || ".join(str(index) for index in range(10))
        twelve_cells = "|-\n| " + " || ".join(str(index) for index in range(12))

        for text in (ten_cells, twelve_cells):
            with self.subTest(cell_count=text.count("||") + 1):
                with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
                    parse_template(payload("AllLightWeapons", "Light", text))

    def test_standard_template_rejects_every_wrong_data_row_width(self):
        header, valid_table = self._table_with_header_category_and_valid_row()

        for cell_count in (9, 10, 12, 13):
            with self.subTest(cell_count=cell_count):
                malformed = "| " + " || ".join(
                    ["[[Malformed Weapon]]", *[str(index) for index in range(cell_count - 1)]]
                )
                text = valid_table.replace(
                    "\n|-\n| [[Valid Dagger]]",
                    "\n|-\n" + malformed + "\n|-\n| [[Valid Dagger]]",
                )
                with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
                    parse_template(payload("AllLightWeapons", "Light", text))

    @staticmethod
    def _table_with_header_category_and_valid_row() -> tuple[str, str]:
        header = "!Name!!Requirements!!Base Damage!!Scaling!!Armor Penetration!!Chip Damage!!Posture Damage!!Range!!Swing Speed!!Endlag!!Scaled Damage"
        category = '|colspan="11" style="background:#FE7316"|[[Daggers|<span>Daggers</span>]]'
        valid = "| " + " || ".join(["[[Valid Dagger]]", "10 LHT", "12", "LHT: 5", "-", "-", "2", "6", "1.2x", "-", "18"])
        return header, "{|\n|-\n" + header + "\n|-\n" + category + "\n|-\n" + valid + "\n|}"

    def test_expected_header_and_colspan_category_are_controls(self):
        _header, text = self._table_with_header_category_and_valid_row()

        rows = parse_template(payload("AllLightWeapons", "Light", text))

        self.assertEqual(rows[0].weapon_type, "Dagger")
        self.assertEqual(rows[0].cells[0], "Valid Dagger")

    def test_bang_prefixed_ten_cell_data_row_is_fatal_before_valid_row(self):
        header, text = self._table_with_header_category_and_valid_row()
        malformed = "! " + " || ".join(["[[Weapon]]", *[str(index) for index in range(9)]])
        text = text.replace("\n|-\n| [[Valid Dagger]]", "\n|-\n" + malformed + "\n|-\n| [[Valid Dagger]]")

        with self.assertRaisesRegex(WeaponSourceError, r"^Malformed weapon source\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))

    def test_required_template_without_weapon_rows_is_rejected(self):
        header, _table = self._table_with_header_category_and_valid_row()
        text = "{|\n|-\n" + header + "\n|-\n! colspan=\"11\" | Daggers\n|}"

        with self.assertRaisesRegex(WeaponSourceError, r"^Required weapon source has no valid rows\.$"):
            parse_template(payload("AllLightWeapons", "Light", text))


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
