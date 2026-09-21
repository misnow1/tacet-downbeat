import asyncio
import io
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from aiohttp import WSMsgType
from aiohttp.test_utils import AioHTTPTestCase

from tacet import annotations as ann
from tacet import app as tacet_app
from tacet import dm7, osc, prompts, reaper, state, web
from tests.disk import Disk


class FakeSender:
    def __init__(self):
        self.packets = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)


class WebTestCase(AioHTTPTestCase):
    async def get_application(self):
        return web.create_app(self.build_app())

    def build_app(self, monotonic=time.monotonic):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.console_sender = FakeSender()
        self.log = ann.AnnotationLog(self.root / "game.jsonl")
        self.log.open()
        self.addCleanup(self.log.close)
        self.tacet = tacet_app.App(
            console=dm7.Dm7Client("192.0.2.1", dca=3, sender=self.console_sender, tick_hz=200.0),
            log=self.log,
            recorder=reaper.ReaperClient(sender=FakeSender(), monotonic=monotonic),
            fade_seconds=0.05,
            monotonic=monotonic,
        )
        return self.tacet

    def entries(self):
        self.log.flush()
        return [e.event for e in ann.read_entries(self.root / "game.jsonl")]


class TestPage(WebTestCase):
    async def test_serves_a_page(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    async def test_the_page_does_not_load_anything_off_the_network(self):
        # The control VLAN has no route to the internet, and a game is not the
        # time to discover a CDN was the single point of failure.
        body = await (await self.client.get("/")).text()
        for scheme in ("http://", "https://", "//cdn"):
            self.assertNotIn(scheme, body)

    async def test_the_page_carries_the_handoff_controls(self):
        # #12: the "StageMix has it" control and its confirm prompt. Where the
        # prompt sits is pinned by the tests below (#108).
        body = await (await self.client.get("/")).text()
        for element_id in ("btn-handoff", "handoff-confirm", "btn-handoff-yes", "btn-handoff-no", "level-tag"):
            self.assertIn(f'id="{element_id}"', body)

    async def test_the_handoff_confirmation_sits_in_more_after_the_control_grid(self):
        # #108: the confirmation renders where the operator just tapped, in
        # MORE and below the Control row whose last button opens it, not in
        # the top slot.
        body = await (await self.client.get("/")).text()
        more = body.split('<div id="tab-more" ', 1)[1].split('<div id="wake">', 1)[0]
        self.assertIn('id="handoff-confirm"', more)
        self.assertLess(more.index('id="btn-handoff"'), more.index('id="handoff-confirm"'))
        # After the grid closes, not inside it: inside, its buttons would
        # inherit `.grid button` and shrink.
        grid_closes = more.index("</div>", more.index('id="btn-handoff"'))
        self.assertLess(grid_closes, more.index('id="handoff-confirm"'))

    async def test_the_handoff_confirmation_has_left_the_top_slot(self):
        # #108: the #prompt slot is above the tabs, so anything before the
        # tabs is in the top slot. It now holds #19's question alone.
        body = await (await self.client.get("/")).text()
        self.assertLess(body.index('class="tabs"'), body.index('id="handoff-confirm"'))
        slot = body.split('<div id="prompt">', 1)[1].split('id="recording-status"', 1)[0]
        self.assertNotIn("handoff-confirm", slot)
        self.assertIn('id="prompt-panel"', slot)

    async def test_the_handoff_confirmation_can_never_move_the_fader_column(self):
        # #108: it lives in .left, before the fader column, which is a flex
        # sibling of the whole left panel (#5), so opening it cannot move it.
        body = await (await self.client.get("/")).text()
        self.assertLess(body.index('id="handoff-confirm"'), body.index('id="fader-column"'))

    async def test_the_handoff_confirmation_ships_hidden(self):
        body = await (await self.client.get("/")).text()
        self.assertIn('<div class="panel" id="handoff-confirm" style="display:none">', body)

    async def test_the_fader_column_still_fits_a_short_landscape_screen(self):
        # The column is six buttons (112 + 112 + 80 + 80 + 72 + 136 = 592), six
        # 12px gaps between its seven children (72) and the readout gap. At
        # 96px that is 592 + 72 + 96 = 760px, which fits a 768px-tall landscape
        # iPad. Raising it to fit the belief row (148px, 812px in all) makes
        # the page scroll, the exact failure #5 was built to remove, so the
        # belief row has to fit inside the 96px instead (#107).
        body = await (await self.client.get("/")).text()
        self.assertIn("#readout{flex:1 1 96px;min-height:96px;", body)
        self.assertNotIn("148px", body)

    async def test_the_readout_can_never_spill_onto_the_buttons_around_it(self):
        # Worst case is more than 96px of content: overflow:hidden is only the
        # backstop, and the two secondary lines are one line each with an
        # ellipsis so they cannot grow past their 15px.
        body = await (await self.client.get("/")).text()
        readout = body.split("#readout{", 1)[1].split("}", 1)[0]
        self.assertIn("overflow:hidden", readout)
        for line in ("#col-refusal", "#readout #fader-error"):
            rule = body.split(line + "{", 1)[1].split("}", 1)[0]
            self.assertIn("text-overflow:ellipsis", rule, line)
            self.assertIn("white-space:nowrap", rule, line)

    async def test_only_the_refusal_copy_yields_and_the_console_fault_never_does(self):
        # "Console unreachable" is shown nowhere else on the page, so it is not
        # a duplicate and must not be the line that shrinks. The refusal is
        # also in the header chip, so it is. The level line and the belief row
        # never shrink either.
        body = await (await self.client.get("/")).text()

        def rule(selector: str) -> str:
            return body.split(selector + "{", 1)[1].split("}", 1)[0]

        self.assertIn("flex:0 0 auto", rule("#readout #fader-error"))
        self.assertIn("flex:0 1 auto", rule("#col-refusal"))
        self.assertIn("flex:none", rule("#readout .value"))
        self.assertIn("flex:none", rule("#belief"))
        self.assertIn("text-overflow:ellipsis", rule("#belief button"))

    async def test_the_belief_row_is_last_in_the_readout_so_the_secondary_lines_yield_first(self):
        body = await (await self.client.get("/")).text()
        readout = body.split('<div id="readout">', 1)[1]
        order = [readout.index(f'id="{name}"') for name in ("level", "fader-error", "col-refusal", "belief")]
        self.assertEqual(order, sorted(order))

    async def test_the_page_carries_the_column_note_and_ships_it_hidden(self):
        # #107: the one line that says why the ramping buttons are grey. Hidden
        # until the script has a snapshot to decide from, so a page that renders
        # before the first push shows nothing rather than a stale reason.
        body = await (await self.client.get("/")).text()
        self.assertIn('<div id="col-unknown"></div>', body)
        self.assertIn("display:none", body.split("#col-unknown{", 1)[1].split("}", 1)[0])

    async def test_the_note_cannot_grow_the_readout(self):
        # The readout has no spare height: level 22.5 + console error 15 +
        # refusal 15 + belief row 43.6 = 96.1 against 96, and that 0.1 is
        # absorbed by the one line allowed to shrink. The note is a third line
        # in that yielding class, and the script never shows it alongside the
        # refusal (tests/test_app_js.mjs), so the worst case is unchanged. Were
        # it to hold its height, or show with the refusal, the belief row at
        # the bottom would be the thing clipped.
        body = await (await self.client.get("/")).text()
        rule = body.split("#col-unknown{", 1)[1].split("}", 1)[0]
        for wanted in (
            "flex:0 1 auto",
            "min-height:0",
            "overflow:hidden",
            "white-space:nowrap",
            "text-overflow:ellipsis",
            "line-height:15px",
        ):
            self.assertIn(wanted, rule)
        readout = body.split('<div id="readout">', 1)[1]
        order = [readout.index(f'id="{name}"') for name in ("fader-error", "col-refusal", "col-unknown", "belief")]
        self.assertEqual(order, sorted(order))

    async def test_the_page_carries_the_belief_controls(self):
        # #107: the two answers to "where is the fader", always on the page.
        body = await (await self.client.get("/")).text()
        for element_id in ("belief", "btn-close-now", "btn-report-ready"):
            self.assertIn(f'id="{element_id}"', body)
        # The labels are the page's own, never rewritten by the script.
        self.assertIn(">Close now<", body)
        self.assertIn(">It's at ready level<", body)

    async def test_the_belief_controls_are_never_hidden_by_the_stylesheet(self):
        # Nothing may appear, disappear or move on a belief change (#107): the
        # row is in the readout gap for good, and the script only ever
        # disables a button in place.
        body = await (await self.client.get("/")).text()
        self.assertNotIn("#belief{display:none", body)
        self.assertNotIn('id="belief" style="display:none', body)

    async def test_the_take_back_prompt_is_gone_from_the_page(self):
        body = await (await self.client.get("/")).text()
        for gone in ("takeback", "btn-take-back-up", "btn-take-back-down", "Take back control"):
            self.assertNotIn(gone, body)

    async def test_the_page_carries_the_prompt_controls(self):
        # #19: the arm / stand-down question's panel, alone in #prompt since
        # #12's hand-off confirmation moved into MORE (#108).
        body = await (await self.client.get("/")).text()
        for element_id in (
            "prompt-panel",
            "prompt-question",
            "prompt-answers",
            "btn-prompt-dismiss",
            "btn-prompt-accept",
            "duty",
        ):
            self.assertIn(f'id="{element_id}"', body)

    async def test_the_prompt_panel_ships_hidden(self):
        body = await (await self.client.get("/")).text()
        self.assertIn('<div class="panel" id="prompt-panel" style="display:none">', body)

    async def test_the_prompt_panel_ships_its_dismiss_label_and_an_empty_accept_button(self):
        # "Not yet" is the page's own markup; the accept label is filled in by
        # script per the question's kind, so it ships empty.
        body = await (await self.client.get("/")).text()
        self.assertIn(">Not yet<", body)
        self.assertIn('<button id="btn-prompt-accept"></button>', body)

    async def test_the_prompt_panel_never_covers_the_fader_column(self):
        # #prompt lives in .left, well before the fader column, which is a
        # flex sibling of the whole left panel (#5) - nothing in #prompt can
        # ever move or cover it.
        body = await (await self.client.get("/")).text()
        left = body.index('id="left"')
        prompt = body.index('id="prompt"')
        fader_column = body.index('id="fader-column"')
        self.assertLess(left, prompt)
        self.assertLess(prompt, fader_column)

    async def test_the_new_prompt_rules_use_no_fixed_or_absolute_positioning(self):
        body = await (await self.client.get("/")).text()
        for selector in ("#prompt-panel", "#prompt-question", "#prompt-answers", "#duty"):
            rule = body.split(selector + "{", 1)[1].split("}", 1)[0]
            self.assertNotIn("position:fixed", rule, selector)
            self.assertNotIn("position:absolute", rule, selector)

    async def test_the_prompt_panel_css_budget_is_documented(self):
        # Mirrors test_the_note_cannot_grow_the_readout: pins the actual CSS
        # declarations the 88px sum is built from, not just the comment's own
        # arithmetic text - a line-height, a max-height, a gap or a button's
        # padding could each drift without failing a test that only checks
        # the prose.
        body = await (await self.client.get("/")).text()

        def rule(selector: str) -> str:
            return body.split(selector + "{", 1)[1].split("}", 1)[0]

        self.assertIn("padding:12px 14px", rule(".panel"))  # the 26px panel overhead
        self.assertIn("gap:6px", rule("#prompt-panel"))
        self.assertIn("line-height:14px", rule("#prompt-question"))
        self.assertIn("max-height:28px", rule("#prompt-question"))
        self.assertIn("padding:6px 10px", rule("#prompt-answers button"))
        self.assertIn("line-height:14px", rule("#prompt-answers button"))
        # Secondary: the comment's own arithmetic, kept honest against the
        # declarations above rather than pinned on its own.
        self.assertIn("26 + 28 + 6 + 28 = 88", body)

    async def test_the_duty_chip_is_first_in_the_strip_and_never_hidden(self):
        body = await (await self.client.get("/")).text()
        self.assertIn('<div id="strip">\n  <div id="duty"></div>', body)
        self.assertNotIn("#duty{display:none", body)
        self.assertNotIn('id="duty" style="display:none', body)


class TestThePageCarriesTheTargetControl(WebTestCase):
    """#9: the standing target's segmented control on MORE and its chip in the
    strip. Both ship empty - the script fills them from the snapshot - and where
    each sits is pinned, since #108 put the hand-off confirmation directly under
    its button and nothing may come between them."""

    async def body(self):
        return await (await self.client.get("/")).text()

    def rule(self, body, selector):
        return body.split(selector + "{", 1)[1].split("}", 1)[0]

    async def test_the_control_and_the_chip_ship_empty(self):
        body = await self.body()
        self.assertIn('<div id="target-control"></div>', body)
        self.assertIn('<div id="target-level"></div>', body)

    async def test_the_control_is_in_more_after_the_handoff_confirmation(self):
        body = await self.body()
        more = body.split('<div id="tab-more" ', 1)[1].split('<div id="wake">', 1)[0]
        self.assertIn('id="target-control"', more)
        self.assertLess(more.index('id="handoff-confirm"'), more.index('id="target-control"'))
        # The confirmation stays directly under its button: the heading comes
        # after the panel closes, not between the two.
        self.assertLess(more.index('id="btn-handoff-no"'), more.index("<h2>Target level</h2>"))
        self.assertLess(more.index("<h2>Target level</h2>"), more.index('id="target-control"'))

    async def test_nothing_sits_between_the_button_and_its_confirmation(self):
        body = await self.body()
        between = body.split('id="btn-handoff"', 1)[1].split('id="handoff-confirm"', 1)[0]
        self.assertNotIn("target", between)

    async def test_both_are_before_the_fader_column(self):
        body = await self.body()
        column = body.index('id="fader-column"')
        self.assertLess(body.index('id="target-control"'), column)
        self.assertLess(body.index('id="target-level"'), column)

    async def test_the_chip_is_in_the_strip_after_the_duty_chip(self):
        body = await self.body()
        strip = body.split('<div id="strip">', 1)[1].split('<div id="prompt">', 1)[0]
        self.assertIn('id="target-level"', strip)
        self.assertLess(strip.index('id="duty"'), strip.index('id="target-level"'))

    async def test_the_chip_rules_are_scoped_to_the_strip_so_they_win(self):
        # `#strip > div` is an id plus a type: an id alone would lose to it.
        body = await self.body()
        self.assertIn("#strip > #target-level{", body)
        self.assertIn("cursor:default", self.rule(body, "#strip > #target-level"))
        amber = self.rule(body, "#strip > #target-level.off-default")
        self.assertIn("var(--fade)", amber)
        self.assertIn("var(--fade-text)", amber)

    async def test_the_segments_are_96_by_72(self):
        body = await self.body()
        rule = self.rule(body, "#target-control button")
        self.assertIn("96px", rule)
        self.assertIn("72px", rule)

    async def test_the_selected_segment_is_neutral_because_this_control_moves_nothing(self):
        # Green and amber are the fader's direction (#5); a segment that
        # changes no level must not borrow either.
        body = await self.body()
        rule = self.rule(body, "#target-control button.selected")
        self.assertNotIn("var(--open)", rule)
        self.assertNotIn("var(--fade)", rule)
        self.assertIn("var(--text)", rule)

    async def test_the_new_rules_use_no_fixed_or_absolute_positioning(self):
        body = await self.body()
        for selector in ("#strip > #target-level", "#target-control", "#target-control button"):
            rule = self.rule(body, selector)
            self.assertNotIn("position:fixed", rule, selector)
            self.assertNotIn("position:absolute", rule, selector)

    async def test_the_script_and_the_page_agree_on_the_ids(self):
        body = await self.body()
        for element_id in ("target-control", "target-level"):
            self.assertIn(f'$("{element_id}")', body)


class TestThePageHasWhereTapsReport(WebTestCase):
    async def test_the_tap_line_is_on_the_page(self):
        # The script writes a failed tap here (#11); a page without it throws
        # on the first tap that fails, which is exactly when it must not.
        text = await (await self.client.get("/")).text()
        self.assertIn('id="tap"', text)
        self.assertIn("button.sending", text)


class TestStateEndpoint(WebTestCase):
    async def test_returns_the_snapshot(self):
        payload = await (await self.client.get("/api/state")).json()
        self.assertEqual(payload["state"], "standing-down")
        self.assertIn("why", payload)
        self.assertIn("buttons", payload)

    async def test_the_fader_is_never_reported_confirmed(self):
        payload = await (await self.client.get("/api/state")).json()
        self.assertFalse(payload["fader"]["confirmed"])


class TestCommands(WebTestCase):
    async def test_arm(self):
        await self.client.post("/api/close-now")
        payload = await (await self.client.post("/api/arm")).json()
        self.assertEqual(payload["state"], "idle")
        self.assertIn("armed", self.entries())

    async def test_arm_before_the_level_is_known_is_refused(self):
        # The production default at cold boot (#107), through the real route.
        payload = await (await self.client.post("/api/arm")).json()
        self.assertEqual(payload["state"], "standing-down")
        self.assertEqual(payload["refusal"], state.UNKNOWN_LEVEL_ARM)
        self.assertNotIn("armed", self.entries())
        self.assertEqual(self.console_sender.packets, [])

    async def test_trigger_opens_the_fader(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        payload = await (await self.client.post("/api/trigger")).json()
        self.assertEqual(payload["state"], "open")
        self.assertTrue(self.console_sender.packets)

    async def test_release_starts_a_fade(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        await self.client.post("/api/trigger")
        payload = await (await self.client.post("/api/release")).json()
        self.assertEqual(payload["state"], "releasing")
        await self.tacet.wait_for_fade()

    async def test_stand_down(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        payload = await (await self.client.post("/api/stand-down")).json()
        self.assertEqual(payload["state"], "standing-down")

    async def test_record(self):
        response = await self.client.post("/api/record")
        self.assertEqual(response.status, 200)
        self.assertIn("recording-started", self.entries())

    async def test_there_is_no_stop_route(self):
        # design.md 5.9. Asserted against the router so it cannot creep back in.
        paths = {getattr(route.resource, "canonical", "") for route in self.app_under_test.router.routes()}
        for forbidden in ("/api/stop", "/api/stop-recording", "/api/abort"):
            self.assertNotIn(forbidden, paths)

    @property
    def app_under_test(self):
        return self.server.app


class TestTheLevelIsKnownOrNot(WebTestCase):
    """#107 (generalising #12): the routes the page's belief controls drive.
    The state machine's own rules are `tests/test_state.py` and
    `tests/test_app.py`'s job; this is only whether each is wired to a route
    at all."""

    async def test_close_now(self):
        payload = await (await self.client.post("/api/close-now")).json()
        self.assertTrue(payload["fader"]["level_known"])
        self.assertEqual(payload["fader"]["commanded"], dm7.MINUS_INF)
        self.assertEqual(len(self.console_sender.packets), 1)
        self.assertIn("took-back", self.entries())

    async def test_report_ready(self):
        payload = await (await self.client.post("/api/report-ready")).json()
        self.assertEqual(payload["state"], "ready")
        self.assertTrue(payload["fader"]["level_known"])
        # A belief, never a packet.
        self.assertEqual(self.console_sender.packets, [])

    async def test_report_ready_once_the_level_is_known_is_refused(self):
        await self.client.post("/api/close-now")
        payload = await (await self.client.post("/api/report-ready")).json()
        self.assertEqual(payload["refusal"], state.LEVEL_ALREADY_KNOWN)
        self.assertEqual(payload["state"], "standing-down")

    async def test_handoff(self):
        await self.client.post("/api/close-now")
        payload = await (await self.client.post("/api/handoff")).json()
        self.assertFalse(payload["fader"]["level_known"])
        self.assertIn("handed-off", self.entries())

    async def test_still_mine(self):
        payload = await (await self.client.post("/api/still-mine")).json()
        self.assertEqual(payload["state"], "standing-down")
        self.assertIn("still-mine", self.entries())

    async def test_a_fade_that_needs_a_known_level_is_refused_on_the_snapshot(self):
        payload = await (await self.client.post("/api/annotate", json={"key": "out"})).json()
        self.assertEqual(payload["state"]["refusal"], state.UNKNOWN_LEVEL_MOVE)
        self.assertFalse(payload["state"]["fader"]["level_known"])
        self.assertNotIn("queued", payload["state"])

    async def test_the_take_back_routes_are_gone(self):
        # Mirrors `test_there_is_no_stop_route`: asserted against the router so
        # they cannot creep back in. #107 replaced them with close-now and
        # report-ready, and nothing may 404 that the page still calls.
        paths = {getattr(route.resource, "canonical", "") for route in self.server.app.router.routes()}
        for gone in ("/api/take-back-up", "/api/take-back-down"):
            self.assertNotIn(gone, paths)
        for present in ("/api/close-now", "/api/report-ready", "/api/handoff", "/api/still-mine", "/api/target"):
            self.assertIn(present, paths)


class TestTheTargetRoute(WebTestCase):
    """#9: `POST /api/target {"db": -3.0}`. It stores a value and moves nothing,
    so what it is held to is the shape of the request and the presets."""

    def build_app(self, monotonic=time.monotonic):
        self.clock = [5000.0]
        return super().build_app(monotonic=lambda: self.clock[0])

    async def post(self, body):
        return await self.client.post("/api/target", json=body)

    async def test_a_good_post_changes_the_target_and_the_response_shows_it(self):
        response = await self.post({"db": -3.0})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["target"]["db"], -3.0)
        self.assertEqual(payload["target"]["level"], -300)
        self.assertIsNone(payload["refusal"])
        self.assertIn("target-set", self.entries())

    async def test_a_whole_number_is_a_number(self):
        payload = await (await self.post({"db": -6})).json()
        self.assertEqual(payload["target"]["db"], -6.0)

    async def test_a_good_post_sends_nothing_to_the_console(self):
        await self.post({"db": -3.0})
        self.assertEqual(self.console_sender.packets, [])

    async def test_the_fader_block_is_untouched_by_it(self):
        before = (await (await self.client.get("/api/state")).json())["fader"]
        payload = await (await self.post({"db": -3.0})).json()
        for key in ("commanded", "level_known", "target", "moving"):
            self.assertEqual(payload["fader"][key], before[key])

    async def test_a_body_without_db_is_a_400_naming_the_field(self):
        response = await self.post({})
        self.assertEqual(response.status, 400)
        self.assertIn("'db' is required", (await response.json())["error"])

    async def test_a_db_that_is_not_a_number_is_a_400_naming_the_field(self):
        bad: object
        for bad in (True, False, "loud", "-3", None, [], {}):
            with self.subTest(bad=bad):
                response = await self.post({"db": bad})
                self.assertEqual(response.status, 400)
                self.assertIn("'db' must be a number", (await response.json())["error"])
        self.assertNotIn("target-set", self.entries())

    async def test_a_non_finite_db_is_a_400(self):
        # `json` accepts NaN and Infinity, which are not levels.
        for text in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(text=text):
                response = await self.client.post(
                    "/api/target", data='{"db": ' + text + "}", headers={"Content-Type": "application/json"}
                )
                self.assertEqual(response.status, 400)
                self.assertIn("'db' must be a number", (await response.json())["error"])

    async def test_malformed_json_is_a_400(self):
        response = await self.client.post("/api/target", data="{", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)

    async def test_a_well_formed_non_preset_is_a_200_with_the_refusal_and_nothing_changed(self):
        response = await self.post({"db": -2.5})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIn("-2.5", payload["refusal"])
        self.assertEqual(payload["target"]["db"], 0.0)
        self.assertEqual(self.console_sender.packets, [])
        self.assertNotIn("target-set", self.entries())

    async def test_it_works_while_the_level_is_unknown_and_standing_down(self):
        # The production default at cold boot: nothing about the target waits
        # on the box knowing where the fader is (#89, #107).
        state_before = await (await self.client.get("/api/state")).json()
        self.assertFalse(state_before["fader"]["level_known"])
        payload = await (await self.post({"db": -6.0})).json()
        self.assertEqual(payload["target"]["db"], -6.0)
        self.assertEqual(payload["state"], "standing-down")

    async def test_the_next_open_goes_to_it(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        await self.post({"db": -3.0})
        payload = await (await self.client.post("/api/trigger")).json()
        self.assertEqual(payload["fader"]["commanded"], -300)

    async def test_the_tap_stamp_reaches_the_log_entry(self):
        body = {"db": -3.0, "tap": {"at": 5998.0, "offset": 1000.0, "uncertainty": 0.05}}
        await self.post(body)
        self.log.flush()
        entry = [e for e in ann.read_entries(self.root / "game.jsonl") if e.event == "target-set"][-1]
        want = {"tapped": 4998.0, "received": 5000.0, "delay": 2.0, "uncertainty": 0.05}
        self.assertEqual(entry.data["tap"], want)

    async def test_a_late_tap_is_still_applied(self):
        # Not stale-checked: it moves nothing.
        body = {"db": -3.0, "tap": {"at": 5000.0 + 1000.0 - 30.0, "offset": 1000.0, "uncertainty": 0.05}}
        payload = await (await self.post(body)).json()
        self.assertEqual(payload["target"]["db"], -3.0)
        self.assertIsNone(payload["stale_tap"])

    async def test_a_malformed_stamp_is_a_client_error_and_changes_nothing(self):
        response = await self.post({"db": -3.0, "tap": {"at": "soon"}})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.tacet.snapshot()["target"]["db"], 0.0)

    async def test_the_page_is_pushed_the_new_target(self):
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            await socket.receive()  # the initial snapshot
            await self.post({"db": -3.0})
            frame = json.loads((await socket.receive()).data)
        self.assertEqual(frame["target"]["db"], -3.0)


class TestAnnotation(WebTestCase):
    async def test_annotating(self):
        response = await self.client.post("/api/annotate", json={"key": "touchdown"})
        self.assertEqual(response.status, 200)
        self.assertIn("touchdown", self.entries())

    async def test_a_note_carries_text(self):
        await self.client.post("/api/annotate", json={"key": "note", "data": {"text": "thin"}})
        self.log.flush()
        entries = list(ann.read_entries(self.root / "game.jsonl"))
        self.assertEqual(entries[-1].data["text"], "thin")

    async def test_an_unknown_event_is_a_client_error_not_a_crash(self):
        response = await self.client.post("/api/annotate", json={"key": "nope"})
        self.assertEqual(response.status, 400)
        self.assertIn("nope", (await response.json())["error"])

    async def test_a_missing_key_is_a_client_error(self):
        response = await self.client.post("/api/annotate", json={})
        self.assertEqual(response.status, 400)

    async def test_malformed_json_is_a_client_error(self):
        response = await self.client.post(
            "/api/annotate", data="{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)

    async def test_a_box_only_event_is_a_client_error(self):
        response = await self.client.post("/api/annotate", json={"key": "recording-started"})
        self.assertEqual(response.status, 400)
        self.assertNotIn("recording-started", self.entries())

    async def test_data_that_is_not_an_object_is_refused_before_the_fader_moves(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        sent = len(self.console_sender.packets)
        response = await self.client.post("/api/annotate", json={"key": "up-drums", "data": "hello"})
        self.assertEqual(response.status, 400)
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(len(self.console_sender.packets), sent)

    async def test_nan_in_the_body_is_refused(self):
        # Python's JSON reader takes a bare NaN; the log must never hold one.
        response = await self.client.post(
            "/api/annotate",
            data='{"key": "note", "data": {"x": NaN}}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 400)
        self.assertNotIn("note", self.entries())

    async def test_an_oversized_body_is_refused_unread(self):
        text = "x" * web.MAX_REQUEST_BYTES
        response = await self.client.post("/api/annotate", json={"key": "note", "data": {"text": text}})
        self.assertEqual(response.status, 413)
        self.assertNotIn("note", self.entries())

    async def test_the_longest_note_fits_in_a_request(self):
        # Every character outside the BMP, which JSON escapes as a surrogate
        # pair - twelve bytes, the most it makes of one character. The body cap
        # must never be what refuses a note the log would accept.
        text = "\U0001f941" * ann.MAX_DATA_TEXT
        response = await self.client.post("/api/annotate", json={"key": "note", "data": {"text": text}})
        self.assertEqual(response.status, 200)

    async def test_spans_open_and_close(self):
        opened = await (await self.client.post("/api/span/start", json={"key": "q1"})).json()
        span_id = opened["span_id"]
        self.assertIn({"span_id": span_id, "event": "q1", "label": "Q1"}, opened["state"]["open_spans"])
        closed = await (await self.client.post("/api/span/end", json={"span_id": span_id})).json()
        self.assertEqual(closed["state"]["open_spans"], [])

    async def test_ending_an_unknown_span_is_a_client_error(self):
        response = await self.client.post("/api/span/end", json={"span_id": "q1-999"})
        self.assertEqual(response.status, 400)


class TestPromptRoutes(WebTestCase):
    """#19: the answers to the arm / stand-down question. Whether a prompt is
    raised, and what an answer does, is `tests/test_app.py`'s job; this is only
    whether each is wired to a route, and what a bad request looks like."""

    async def stand_down_question(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        payload = await (await self.client.post("/api/annotate", json={"key": ann.BAND_EXITS_STANDS})).json()
        self.assertEqual(payload["state"]["prompt"]["kind"], "stand-down")
        return payload["state"]["prompt"]["seq"]

    async def test_accepting_a_stand_down_prompt_stands_the_box_down(self):
        seq = await self.stand_down_question()
        response = await self.client.post("/api/prompt/accept", json={"seq": seq})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["state"], "standing-down")
        self.assertIsNone(payload["prompt"])
        self.assertIn(prompts.PROMPT_ACCEPTED, self.entries())

    async def test_dismissing_through_the_route_logs_it_and_changes_nothing(self):
        seq = await self.stand_down_question()
        sent = len(self.console_sender.packets)
        payload = await (await self.client.post("/api/prompt/dismiss", json={"seq": seq})).json()
        self.assertEqual(payload["state"], "idle")
        self.assertIsNone(payload["prompt"])
        self.assertIn(prompts.PROMPT_DISMISSED, self.entries())
        self.assertNotIn(prompts.PROMPT_ACCEPTED, self.entries())
        self.assertEqual(len(self.console_sender.packets), sent)

    async def test_an_answer_without_a_seq_is_a_bad_request(self):
        await self.stand_down_question()
        for path in ("/api/prompt/accept", "/api/prompt/dismiss"):
            with self.subTest(path=path):
                response = await self.client.post(path, json={})
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "'seq' is required")
        self.assertEqual(self.tacet.machine.state, state.State.IDLE)

    async def test_an_answer_with_a_seq_that_is_not_an_integer_is_a_bad_request(self):
        await self.stand_down_question()
        # A bool is an int to Python and not to the page: rejected explicitly.
        for bad in ("1", 1.5, True, None, [1]):
            for path in ("/api/prompt/accept", "/api/prompt/dismiss"):
                with self.subTest(path=path, seq=bad):
                    response = await self.client.post(path, json={"seq": bad})
                    self.assertEqual(response.status, 400)
                    # Present, so not "required": it is the wrong kind of thing.
                    self.assertEqual((await response.json())["error"], f"'seq' must be a whole number, got {bad!r}")
        self.assertEqual(self.tacet.machine.state, state.State.IDLE)
        self.assertEqual(self.tacet.snapshot()["prompt"]["seq"], 1)

    async def test_an_answer_that_is_not_json_is_a_bad_request(self):
        response = await self.client.post(
            "/api/prompt/accept", data="{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)

    async def test_an_answer_for_a_prompt_that_moved_on_changes_nothing_and_is_not_an_error(self):
        seq = await self.stand_down_question()
        before = self.entries()
        for path in ("/api/prompt/accept", "/api/prompt/dismiss"):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"seq": seq + 1})
                self.assertEqual(response.status, 200)
                payload = await response.json()
                self.assertEqual(payload["state"], "idle")
                self.assertEqual(payload["prompt"]["seq"], seq)
        self.assertEqual(self.entries(), before)

    async def test_an_answer_with_no_prompt_open_is_not_an_error(self):
        response = await self.client.post("/api/prompt/accept", json={"seq": 1})
        self.assertEqual(response.status, 200)
        self.assertIsNone((await response.json())["prompt"])

    async def test_the_snapshot_carries_the_prompt_and_the_duty_clock(self):
        payload = await (await self.client.get("/api/state")).json()
        self.assertIsNone(payload["prompt"])
        self.assertEqual(payload["duty"], {"armed": False, "since": None})
        await self.stand_down_question()
        payload = await (await self.client.get("/api/state")).json()
        self.assertEqual(payload["prompt"], {"seq": 1, "kind": "stand-down", "source": ann.BAND_EXITS_STANDS})
        self.assertTrue(payload["duty"]["armed"])
        self.assertIsInstance(payload["duty"]["since"], float)

    async def test_both_answer_routes_are_registered(self):
        paths = {getattr(route.resource, "canonical", "") for route in self.server.app.router.routes()}
        for present in ("/api/prompt/accept", "/api/prompt/dismiss"):
            self.assertIn(present, paths)

    async def test_an_answer_reads_the_tap_stamp_too(self):
        seq = await self.stand_down_question()
        body = {"seq": seq, "tap": {"at": 5.0, "offset": 0.0, "uncertainty": 0.05}}
        await self.client.post("/api/prompt/dismiss", json=body)
        self.log.flush()
        dismissed = [e for e in ann.read_entries(self.root / "game.jsonl") if e.event == prompts.PROMPT_DISMISSED]
        self.assertIn("tap", dismissed[0].data)


class RefusingSender:
    def send(self, packet: bytes) -> None:
        from tacet.net import TransportError

        raise TransportError("connection refused")


class TestFailures(WebTestCase):
    """A write or a send that fails is not a 500. The tap was acted on, and the
    state that comes back says what did not happen."""

    async def get_application(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.disk = Disk()
        self.log = ann.AnnotationLog(self.root / "game.jsonl", opener=self.disk.open)
        self.log.open()
        self.addCleanup(self.log.close)
        self.tacet = tacet_app.App(
            console=dm7.Dm7Client("192.0.2.1", dca=3, sender=FakeSender(), tick_hz=200.0),
            log=self.log,
            recorder=reaper.ReaperClient(sender=RefusingSender()),
            fade_seconds=0.05,
        )
        return web.create_app(self.tacet)

    async def test_an_annotation_that_was_not_saved_says_so(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm")
        self.disk.full = True
        response = await self.client.post("/api/annotate", json={"key": "up-drums"})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        # Accepted, and answered before the disk was tried (#41).
        self.assertIsNotNone(payload["entry"])
        self.assertEqual(payload["state"]["state"], "open")
        self.log.flush()
        state = await (await self.client.get("/api/state")).json()
        self.assertFalse(state["log"]["healthy"])

    async def test_a_span_that_was_not_saved_says_so(self):
        self.disk.full = True
        response = await self.client.post("/api/span/start", json={"key": "q3"})
        self.assertEqual(response.status, 200)
        self.log.flush()
        state = await (await self.client.get("/api/state")).json()
        self.assertFalse(state["log"]["healthy"])
        self.assertEqual(state["open_spans"], [])

    async def test_a_record_send_that_fails_is_a_refusal_not_a_500(self):
        response = await self.client.post("/api/record")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIn("connection refused", payload["refusal"])


class TestWebSocket(WebTestCase):
    async def test_a_snapshot_arrives_on_connect(self):
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            payload = json.loads((await socket.receive()).data)
            self.assertEqual(payload["state"], "standing-down")

    async def test_changes_are_pushed(self):
        await self.client.post("/api/close-now")
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            await socket.receive()  # the initial snapshot
            await self.client.post("/api/arm")
            payload = json.loads((await socket.receive()).data)
            self.assertEqual(payload["state"], "idle")


class TestKeepalive(WebTestCase):
    """An open socket proves nothing. Stadium wifi half-opens - delivery stops,
    `onclose` never fires, and the page goes on showing a snapshot from six
    minutes ago with complete confidence. aiohttp's ping frames are how the box
    notices a dead browser; a browser cannot see them, so it gets these.
    """

    async def test_a_keepalive_arrives_before_the_first_snapshot(self):
        # The page reports itself as connecting until it has been told how long
        # to wait, so being told has to come with the first round trip rather
        # than a keepalive interval later.
        async with self.client.ws_connect("/ws") as socket:
            first = json.loads((await socket.receive()).data)
            self.assertTrue(first["keepalive"])
            self.assertEqual(first["stale_after"], web.STALE_AFTER)
            second = json.loads((await socket.receive()).data)
            self.assertEqual(second["state"], "standing-down")

    async def test_a_keepalive_is_not_mistakable_for_a_snapshot(self):
        # The page renders whatever is not marked, so an unmarked keepalive
        # would blank the state, the fader and the recording line at once.
        frame = json.loads(web.KEEPALIVE_FRAME)
        self.assertTrue(frame["keepalive"])
        for key in ("state", "fader", "recording", "buttons"):
            self.assertNotIn(key, frame)

    async def test_a_snapshot_is_not_mistakable_for_a_keepalive(self):
        payload = await (await self.client.get("/api/state")).json()
        self.assertNotIn("keepalive", payload)


class TestKeepaliveRepeats(WebTestCase):
    """The interval turned right up, so the loop can be watched repeating
    without the test taking a minute to do it."""

    async def get_application(self):
        return web.create_app(self.build_app(), keepalive_interval=0.01)

    async def test_they_keep_coming_on_a_socket_with_nothing_to_report(self):
        # The whole point. Silence is what the page has to be able to trust,
        # and it can only trust it if something arrives during it.
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            await socket.receive()  # the initial snapshot
            for _ in range(3):
                frame = json.loads((await socket.receive()).data)
                self.assertTrue(frame["keepalive"])


class TestStateTick(WebTestCase):
    """Some faults are a silence, and a silence sends nothing to push on.

    Reaper crashing mid-recording stops its packets, so nothing called
    `broadcast` and the page went on showing ROLLING, tagged confirmed, for as
    long as anyone cared to look - while `/api/state` said LINK LOST. The
    keepalive kept the link pulse green the whole time, correctly: the link to
    the box was fine. So the box re-reads its own state on a tick.
    """

    TICK = 0.01
    #: Generous next to the tick, and still well short of a keepalive, so
    #: anything that arrives in the window is a snapshot.
    WAIT = 1.0

    async def get_application(self):
        self.clock = [1000.0]
        app = self.build_app(monotonic=lambda: self.clock[0])
        return web.create_app(app, tick_interval=self.TICK)

    async def snapshots(self, socket):
        """The next snapshot, skipping keepalives."""
        while True:
            frame = json.loads((await socket.receive(timeout=self.WAIT)).data)
            if not frame.get("keepalive"):
                return frame

    async def test_reaper_going_silent_is_pushed_to_the_page(self):
        async with self.client.ws_connect("/ws") as socket:
            await self.snapshots(socket)  # the initial snapshot
            self.tacet.handle_recorder_packet(osc.encode_message("/record", 1.0))
            rolling = (await self.snapshots(socket))["recording"]
            self.assertEqual((rolling["recording"], rolling["liveness"]), (True, "live"))

            self.clock[0] += 60.0  # the /time stream should have been arriving
            # Nothing is sent to the box. The page has to find out anyway.
            lost = (await self.snapshots(socket))["recording"]
            self.assertEqual(lost["liveness"], "lost")
            self.assertFalse(lost["confirmed"])

    async def test_a_tick_that_finds_nothing_new_sends_nothing(self):
        async with self.client.ws_connect("/ws") as socket:
            await self.snapshots(socket)  # the initial snapshot
            with self.assertRaises(asyncio.TimeoutError):
                await self.snapshots(socket)

    async def test_the_tick_outlives_a_snapshot_that_fails(self):
        # A tick that died quietly would put this bug straight back, with
        # nothing on any screen to say so.
        real = self.tacet.snapshot
        failures = [RuntimeError("boom")]

        def flaky():
            if failures:
                raise failures.pop()
            return real()

        async with self.client.ws_connect("/ws") as socket:
            await self.snapshots(socket)  # the initial snapshot
            self.tacet.handle_recorder_packet(osc.encode_message("/record", 1.0))
            await self.snapshots(socket)  # rolling
            with (
                mock.patch.object(self.tacet, "snapshot", side_effect=flaky),
                mock.patch("sys.stderr", new_callable=io.StringIO) as terminal,
            ):
                self.clock[0] += 60.0
                lost = (await self.snapshots(socket))["recording"]
            self.assertEqual(lost["liveness"], "lost")
            # Survived, but not swallowed: whoever is at the terminal sees it.
            self.assertIn("boom", terminal.getvalue())


class TestStaleThreshold(unittest.TestCase):
    def test_it_survives_a_lost_keepalive(self):
        # One frame lost on a bad link is not a fault. Were the threshold at or
        # under a single interval, every ordinary hiccup would paint the page
        # red and the banner would stop meaning anything.
        self.assertGreater(web.STALE_AFTER, web.KEEPALIVE_INTERVAL * 2)

    def test_it_is_derived_rather_than_written_down_twice(self):
        self.assertEqual(web.STALE_AFTER, web.KEEPALIVE_INTERVAL * web.KEEPALIVE_LOSSES_BEFORE_STALE)

    def test_the_page_is_told_it_rather_than_keeping_a_copy(self):
        # The one number, crossing into the other language exactly once.
        self.assertIn('"stale_after"', web.KEEPALIVE_FRAME)


class TestLinkIndicator(WebTestCase):
    """The banner only ever says something is wrong, and its absence is also
    what a page that has stopped executing looks like. The counter beside the
    state is the half that says things are fine.
    """

    async def test_the_page_carries_the_counter(self):
        body = await (await self.client.get("/")).text()
        self.assertIn('id="pulse"', body)

    async def test_it_starts_claiming_nothing(self):
        # Not "0s". Nothing has arrived yet, and a page that has never heard
        # from the box must not open looking healthy.
        body = await (await self.client.get("/")).text()
        self.assertIn('<div id="pulse">--</div>', body)

    async def test_it_shares_the_line_with_the_state(self):
        body = await (await self.client.get("/")).text()
        headline = body[body.index('class="headline"') : body.index('id="why"')]
        self.assertIn('id="state"', headline)
        self.assertIn('id="pulse"', headline)


class TestWakeAdvice(WebTestCase):
    """The Screen Wake Lock API needs a secure context and the page is served
    over plain HTTP on a VLAN with no route to a certificate authority. An iPad
    that locks its screen stops being an operator interface, so when the API is
    missing the page has to say the thing that does work.
    """

    async def test_the_page_carries_somewhere_to_say_it(self):
        body = await (await self.client.get("/")).text()
        self.assertIn('id="wake"', body)

    async def test_the_advice_names_the_setting_that_works(self):
        body = await (await self.client.get("/")).text()
        self.assertIn("Auto-Lock", body)


class TestShutdown(WebTestCase):
    async def test_a_browser_is_hung_up_on_rather_than_left_hanging(self):
        # Two reasons. The shutdown otherwise waits on handlers parked in
        # `async for message in socket`, which is what made Ctrl-C take two
        # presses and lose the rest of the cleanup; and the page wants the
        # close anyway, since a clean hangup puts it straight into its
        # reconnecting banner, where a silent disappearance would leave it
        # looking healthy and stale until the keepalive threshold expired.
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            await socket.receive()  # the initial snapshot
            await self.app.shutdown()
            self.assertEqual((await socket.receive()).type, WSMsgType.CLOSE)


class TestBroadcastThrottle(unittest.TestCase):
    """Reaper streams `/time` at about 11 Hz while rolling, and every packet
    used to push a whole snapshot at every browser - roughly 30 KB/s each, on
    an iPad on stadium wifi, for three hours. The playhead does not need that
    resolution. Anything that is not the playhead still goes out at once.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        log = ann.AnnotationLog(root / "game.jsonl")
        log.open()
        self.addCleanup(log.close)
        self.app = tacet_app.App(
            console=dm7.Dm7Client("192.0.2.1", dca=3, sender=FakeSender()),
            log=log,
            recorder=reaper.ReaperClient(sender=FakeSender()),
        )

    def moved(self, snapshot, position):
        recording = dict(snapshot["recording"], position=position)
        return dict(snapshot, recording=recording)

    def test_the_first_snapshot_always_goes_out(self):
        self.assertTrue(web.should_broadcast(None, self.app.snapshot(), elapsed=0.0))

    def test_a_playhead_that_has_only_moved_waits(self):
        before = self.app.snapshot()
        self.assertFalse(web.should_broadcast(before, self.moved(before, 12.5), elapsed=0.1, interval=1.0))

    def test_a_playhead_that_has_only_moved_goes_out_once_the_interval_passes(self):
        before = self.app.snapshot()
        self.assertTrue(web.should_broadcast(before, self.moved(before, 12.5), elapsed=1.5, interval=1.0))

    def test_anything_that_is_not_the_playhead_is_never_delayed(self):
        """The fader must not wait on a coalescing timer. A missed downbeat is
        unrecoverable; see CLAUDE.md on fast open."""
        before = self.app.snapshot()
        after = self.app.snapshot()
        after["fader"] = dict(after["fader"], commanded=0)
        self.assertTrue(web.should_broadcast(before, after, elapsed=0.0, interval=1.0))

    def test_a_transport_change_is_never_delayed(self):
        before = self.app.snapshot()
        self.app.handle_recorder_packet(osc.encode_message("/record", 1.0))
        after = self.app.snapshot()
        self.assertNotEqual(before["recording"]["recording"], after["recording"]["recording"])
        self.assertTrue(web.should_broadcast(before, after, elapsed=0.0, interval=1.0))

    def test_an_identical_snapshot_still_waits(self):
        snapshot = self.app.snapshot()
        self.assertFalse(web.should_broadcast(snapshot, snapshot, elapsed=0.1, interval=1.0))

    def test_an_identical_snapshot_never_goes_out(self):
        # The box re-reads its state on a tick so a silence can be noticed, and
        # most ticks find nothing new. Resending those would push the whole
        # snapshot at every browser every second for three hours to say nothing.
        snapshot = self.app.snapshot()
        self.assertFalse(web.should_broadcast(snapshot, snapshot, elapsed=3600.0, interval=1.0))


class TestEveryFaderActionIsColoured(unittest.TestCase):
    """The grid is tapped without looking, so colour carries the meaning.

    This exists because it did not: adding `open-slow` to the vocabulary left
    `up-slow` matching no colour rule, so the one button that moves the fader
    most gently rendered as an ordinary annotation button. Nothing failed - the
    page was simply wrong, which is the kind of thing a stadium is bad at
    forgiving.
    """

    def test_every_action_has_a_rule_in_the_page(self):
        for action in ann.Action:
            with self.subTest(action=action):
                self.assertIn(f'[data-action="{action.value}"]', web.PAGE)

    def test_every_action_a_button_carries_is_in_that_set(self):
        # The vocabulary is the source: a button with an action the stylesheet
        # has never heard of is the failure above, one release later.
        for event in ann.BUTTONS:
            if event.action is not None:
                with self.subTest(event=event.key):
                    self.assertIn(f'[data-action="{event.action.value}"]', web.PAGE)

    def test_every_opener_shares_the_colour_that_means_up(self):
        # Direction, not gesture: open, open-slow and ready all send the fader
        # up, and the button says in words which gesture it is. Not scoped to
        # .grid (#5): the fader column's buttons live outside any grid and
        # must read the same way.
        self.assertIn(
            'button[data-action="open"],\n'
            'button[data-action="open-slow"],\n'
            'button[data-action="ready"]{border-color:var(--open)',
            web.PAGE,
        )


class TestTapsAreStampedOnTheWayIn(WebTestCase):
    """#11: every route the page taps carries the page's stamp, and the box
    logs when it was tapped as well as when it arrived."""

    def build_app(self, monotonic=time.monotonic):
        self.clock = [5000.0]
        return super().build_app(monotonic=lambda: self.clock[0])

    def stamp(self, *, at, offset=None, uncertainty=None):
        return {"tap": {"at": at, "offset": offset, "uncertainty": uncertainty}}

    def logged(self):
        self.log.flush()
        return list(ann.read_entries(self.root / "game.jsonl"))

    async def test_every_command_route_reads_the_stamp(self):
        # Page clock 1000 s ahead of the box's, tapped 2 s before it arrived.
        body = self.stamp(at=5998.0, offset=1000.0, uncertainty=0.05)
        paths = ("/api/close-now", "/api/arm", "/api/trigger", "/api/release", "/api/stand-down", "/api/record")
        for path in paths:
            with self.subTest(path):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status, 200)
        await self.tacet.wait_for_fade()
        want = {"tapped": 4998.0, "received": 5000.0, "delay": 2.0, "uncertainty": 0.05}
        for entry in self.logged():
            with self.subTest(entry.event):
                self.assertEqual(entry.data["tap"], want)

    async def test_annotations_and_spans_read_it_too(self):
        body = self.stamp(at=6000.0, offset=1000.5, uncertainty=0.01)
        await self.client.post("/api/annotate", json={"key": "note", "data": {"text": "x"}, **body})
        started = await (await self.client.post("/api/span/start", json={"key": "q1", **body})).json()
        await self.client.post("/api/span/end", json={"span_id": started["span_id"], **body})
        for entry in self.logged():
            with self.subTest(entry.event, phase=entry.phase):
                self.assertEqual(entry.data["tap"]["delay"], 0.5)

    async def test_a_tap_with_no_estimate_logs_when_it_arrived(self):
        await self.client.post("/api/close-now")
        await self.client.post("/api/arm", json=self.stamp(at=123.0))
        tap = self.logged()[-1].data["tap"]
        self.assertEqual(tap, {"tapped": None, "received": 5000.0, "delay": None, "uncertainty": None})

    async def test_a_command_with_no_body_still_works(self):
        # curl, or a page cached from before the stamp.
        await self.client.post("/api/close-now")
        response = await self.client.post("/api/arm")
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.logged()[-1].data["tap"]["delay"])

    async def test_report_ready_reads_the_stamp_too(self):
        # It is the one route that cannot follow close-now in the loop above:
        # once the level is known it is refused.
        body = self.stamp(at=5998.0, offset=1000.0, uncertainty=0.05)
        await self.client.post("/api/report-ready", json=body)
        self.assertEqual(self.logged()[-1].data["tap"]["delay"], 2.0)

    async def test_a_malformed_stamp_is_a_client_error_and_does_nothing(self):
        response = await self.client.post("/api/arm", json={"tap": {"at": "soon"}})
        self.assertEqual(response.status, 400)
        self.assertIn("tap", (await response.json())["error"])
        self.assertEqual(self.tacet.machine.state.value, "standing-down")


class TestTheClockCanBeAskedOverTheSocket(WebTestCase):
    """#11: the page estimates its offset from the box by a round trip on the
    socket it already has open, triggered by each keepalive."""

    def build_app(self, monotonic=time.monotonic):
        return super().build_app(monotonic=lambda: 777.25)

    async def test_a_ping_is_answered_with_the_box_clock(self):
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the opening keepalive
            await socket.receive()  # the initial snapshot
            await socket.send_str(json.dumps({"ping": 1234.5}))
            reply = json.loads((await socket.receive()).data)
        self.assertEqual(reply, {"pong": 1234.5, "box": 777.25})

    def test_only_a_well_formed_ping_is_answered(self):
        self.assertEqual(json.loads(web.pong_for('{"ping": 1.5}', 9.0) or ""), {"pong": 1.5, "box": 9.0})
        for text in ("not json", "[]", '{"ping": "1"}', '{"ping": true}', '{"other": 1}', '{"ping": NaN}'):
            with self.subTest(text):
                self.assertIsNone(web.pong_for(text, 9.0))

    def test_a_pong_is_neither_a_snapshot_nor_a_keepalive(self):
        frame = json.loads(web.pong_for('{"ping": 1.0}', 2.0) or "")
        self.assertNotIn("state", frame)
        self.assertNotIn("keepalive", frame)


class TestTheTimestampAloneIsNotNews(WebTestCase):
    def test_a_snapshot_that_differs_only_in_when_it_was_taken_is_not_sent(self):
        before = self.tacet.snapshot()
        after = {**before, "at": before["at"] + 1.0}
        self.assertFalse(web.should_broadcast(before, after, elapsed=5.0))
