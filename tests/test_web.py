import json
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp.test_utils import AioHTTPTestCase

from tacet import annotations as ann
from tacet import app as tacet_app
from tacet import dm7, reaper, web


class FakeSender:
    def __init__(self):
        self.packets = []

    def send(self, packet: bytes) -> None:
        self.packets.append(packet)


class WebTestCase(AioHTTPTestCase):
    async def get_application(self):
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
            recorder=reaper.ReaperClient(sender=FakeSender()),
            fade_seconds=0.05,
        )
        return web.create_app(self.tacet)

    def entries(self):
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

    async def test_spans_open_and_close(self):
        opened = await (await self.client.post("/api/span/start", json={"key": "q1"})).json()
        span_id = opened["span_id"]
        self.assertIn(span_id, opened["state"]["open_spans"])
        closed = await (await self.client.post("/api/span/end", json={"span_id": span_id})).json()
        self.assertEqual(closed["state"]["open_spans"], [])

    async def test_ending_an_unknown_span_is_a_client_error(self):
        response = await self.client.post("/api/span/end", json={"span_id": "q1-999"})
        self.assertEqual(response.status, 400)


class TestWebSocket(WebTestCase):
    async def test_a_snapshot_arrives_on_connect(self):
        async with self.client.ws_connect("/ws") as socket:
            payload = json.loads((await socket.receive()).data)
            self.assertEqual(payload["state"], "standing-down")

    async def test_changes_are_pushed(self):
        async with self.client.ws_connect("/ws") as socket:
            await socket.receive()  # the initial snapshot
            await self.client.post("/api/arm")
            payload = json.loads((await socket.receive()).data)
            self.assertEqual(payload["state"], "idle")
