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
from tacet import dm7, osc, reaper, web
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
        payload = await (await self.client.post("/api/arm")).json()
        self.assertEqual(payload["state"], "idle")
        self.assertIn("armed", self.entries())

    async def test_trigger_opens_the_fader(self):
        await self.client.post("/api/arm")
        payload = await (await self.client.post("/api/trigger")).json()
        self.assertEqual(payload["state"], "open")
        self.assertTrue(self.console_sender.packets)

    async def test_release_starts_a_fade(self):
        await self.client.post("/api/arm")
        await self.client.post("/api/trigger")
        payload = await (await self.client.post("/api/release")).json()
        self.assertEqual(payload["state"], "releasing")
        await self.tacet.wait_for_fade()

    async def test_stand_down(self):
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


class TestAnnotation(WebTestCase):
    async def test_annotating(self):
        response = await self.client.post("/api/annotate", json={"key": "touchdown-sequence"})
        self.assertEqual(response.status, 200)
        self.assertIn("touchdown-sequence", self.entries())

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
        await self.client.post("/api/arm")
        response = await self.client.post("/api/annotate", json={"key": "up-drums", "data": "hello"})
        self.assertEqual(response.status, 400)
        state = await (await self.client.get("/api/state")).json()
        self.assertEqual(state["state"], "idle")
        self.assertEqual(self.console_sender.packets, [])

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
        self.assertIn({"span_id": span_id, "event": "q1"}, opened["state"]["open_spans"])
        closed = await (await self.client.post("/api/span/end", json={"span_id": span_id})).json()
        self.assertEqual(closed["state"]["open_spans"], [])

    async def test_ending_an_unknown_span_is_a_client_error(self):
        response = await self.client.post("/api/span/end", json={"span_id": "q1-999"})
        self.assertEqual(response.status, 400)


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

    def test_the_two_openers_share_the_colour_that_means_up(self):
        # Direction, not gesture: both send the fader up, and the button says in
        # words which of them rides in.
        self.assertIn(
            '.grid button[data-action="open"],\n.grid button[data-action="open-slow"]{border-color:var(--open)',
            web.PAGE,
        )
