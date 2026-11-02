"""Provider-free duplex conversation and lifecycle fixtures; no real devices."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from service import VoiceService, plain, SPEECH_BYTES
from settings import DEFAULTS

OWNER = {"session_id": "owner", "extension_instance_id": "instance", "process_generation": 1}


def eventually(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("voice fixture did not settle")


class FakeAudio:
    def __init__(self, **kwargs):
        self.capture = asyncio.Queue()
        self.played = []
        self.started = False
        self.stopped = False
        self.muted = False
        self.interrupts = 0

    def levels(self):
        return (0.0, 0.0)

    async def start(self):
        self.started = True

    async def read(self):
        return await self.capture.get()

    async def play(self, data):
        self.played.append(data)

    async def interrupt(self):
        self.interrupts += 1

    def set_muted(self, value):
        self.muted = value

    async def stop(self):
        self.stopped = True


class FakeClient:
    def __init__(self, config, credential):
        self.events = asyncio.Queue()
        self.sent = []
        self.results = []
        self.texts = []
        self.closed = False

    async def connect(self):
        pass

    async def receive(self):
        return await self.events.get()

    async def set_muted(self, value):
        self.muted = value

    async def send_audio(self, data):
        self.sent.append(data)

    async def send_result(self, identifier, text, final=True):
        self.results.append((identifier, text, final))

    async def send_text(self, text):
        self.texts.append(text)

    async def close(self):
        self.closed = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clients = []
        self.audios = []
        self.injections = []
        self.notices = []
        self.inject_error = False

        def client(*args):
            value = FakeClient(*args)
            self.clients.append(value)
            return value

        def audio(**kwargs):
            value = FakeAudio(**kwargs)
            self.audios.append(value)
            return value

        def inject(*args, **kwargs):
            self.injections.append(args)
            if self.inject_error:
                raise TimeoutError("ambiguous receipt")

        self.service = VoiceService(inject, lambda text, **kw: self.notices.append((text, kw)),
                                    client_factory=client, audio_factory=audio)
        self.addCleanup(self.service.close)

    def start(self):
        ticket = self.service.call(self.service.reserve(OWNER, "host-session", 3))
        self.assertTrue(self.service.call(self.service.activate(ticket, DEFAULTS, {"api_key": "fixture"})))
        eventually(lambda: self.service.snapshot()["state"] == "listening")
        return self.clients[-1], self.audios[-1]

    def event(self, value):
        self.service._loop.call_soon_threadsafe(self.clients[-1].events.put_nowait, value)

    def test_explicit_start_only_and_cancelled_credential_callback(self):
        self.assertEqual(self.audios, [])
        ticket = self.service.call(self.service.reserve(OWNER, "host-session", 3))
        self.assertEqual(self.audios, [])
        self.service.call(self.service.stop())
        self.assertFalse(self.service.call(self.service.activate(ticket, DEFAULTS, {})))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.audios, [])

    def test_default_audio_factory_selects_native_platform_only_on_activation(self):
        import audio
        import portable_audio

        self.service.audio_factory = None
        config = dict(DEFAULTS, input_target="Built-in Microphone", output_target="Headphones")
        for platform, backend, class_name in (("linux", audio, "PipeWireAudio"),
                ("darwin", portable_audio, "PortableAudio"),
                ("win32", portable_audio, "PortableAudio")):
            with self.subTest(platform=platform), patch("sys.platform", platform), \
                    patch.object(backend, class_name, side_effect=FakeAudio) as factory, \
                    patch.object(portable_audio, "_sounddevice", side_effect=AssertionError("native import")):
                ticket = self.service.call(self.service.reserve(OWNER, "host-session", 3))
                factory.assert_not_called()
                self.service.call(self.service.activate(ticket, config, {}))
                eventually(lambda: self.service.snapshot()["state"] == "listening")
                factory.assert_called_once_with(input_target="Built-in Microphone", output_target="Headphones")
                selected = self.service._audio
                self.assertTrue(selected.started)
                self.service.call(self.service.stop())
                self.assertTrue(selected.stopped)

    def test_bidirectional_audio_and_live_mute(self):
        client, audio = self.start()
        self.service._loop.call_soon_threadsafe(audio.capture.put_nowait, b"\0\0" * 480)
        eventually(lambda: bool(client.sent))
        self.event({"type": "audio", "audio": b"\1\0" * 480})
        eventually(lambda: bool(audio.played))
        self.assertEqual(len(client.sent[0]), 960)
        self.assertEqual(len(audio.played[0]), 960)
        self.assertEqual(self.service.call(self.service.mute()), "Microphone muted")
        self.assertTrue(audio.muted)
        self.assertEqual(self.service.call(self.service.mute()), "Microphone live")
        self.assertFalse(audio.muted)
        self.event({"type": "interrupted"})
        eventually(lambda: audio.interrupts == 1)
        self.service.call(self.service.stop())
        self.assertTrue(audio.stopped)
        self.assertTrue(client.closed)

    def test_handoff_admission_dedup_and_shared_response_not_false_completion(self):
        client, _ = self.start()
        handoff = {"type": "handoff", "handoff_id": "request-1", "text": "Inspect tests"}
        self.event(handoff)
        eventually(lambda: bool(client.results))
        self.event(handoff)
        self.service.call(self.service.update("answer", "Tests passed.", OWNER))
        eventually(lambda: len(client.results) == 2)
        self.assertEqual(len(self.injections), 1)
        self.assertEqual(self.injections[0], ("Inspect tests", OWNER, 3))
        self.assertEqual(client.results[0][0], "request-1")
        self.assertFalse(client.results[0][2])
        self.assertIn("not complete", client.results[0][1])
        self.assertEqual(client.results[1], (None, "Tests passed.", True))

    def test_ambiguous_handoff_stops_and_never_replays(self):
        client, audio = self.start()
        self.inject_error = True
        self.event({"type": "handoff", "handoff_id": "request-1", "text": "Run tests"})
        eventually(lambda: audio.stopped)
        self.assertEqual(self.service.snapshot()["state"], "failed")
        self.assertEqual(len(self.injections), 1)
        self.assertEqual(client.results, [])
        self.assertTrue(client.closed)
        self.assertNotIn("ambiguous receipt", str(self.notices))

    def test_typed_prompt_interrupts_and_owner_fences_updates(self):
        client, audio = self.start()
        self.service.call(self.service.update("answer", "foreign secret", dict(OWNER, session_id="other")))
        self.service.call(self.service.update("prompt", "Explain instead", OWNER))
        eventually(lambda: bool(client.texts))
        self.assertEqual(client.results, [])
        self.assertNotIn("foreign secret", str(client.texts))
        self.assertEqual(audio.interrupts, 1)
        self.assertIn("Explain instead", client.texts[0])
        self.assertTrue(self.service.owns({"resource_owner": OWNER}))
        self.assertFalse(self.service.owns({"resource_owner": dict(OWNER, process_generation=2)}))

    def test_budget_keeps_full_answer_in_native_octet(self):
        client, _ = self.start()
        self.service.call(self.service.update("answer", "é" * SPEECH_BYTES, OWNER))
        eventually(lambda: bool(client.results))
        self.assertIn("too long", client.results[0][1])
        self.assertLess(len(client.results[0][1]), SPEECH_BYTES)

    def test_captions_sanitized_and_partials_not_spammed(self):
        self.start()
        self.event({"type": "input_transcript", "text": "partial", "final": False})
        self.event({"type": "input_transcript", "text": "hello\x1b[31m\x00", "final": True})
        eventually(lambda: self.service.snapshot()["caption"].startswith("You: hello"))
        self.assertNotIn("\x1b", self.service.snapshot()["caption"])
        self.assertNotIn("\x00", self.service.snapshot()["caption"])
        self.assertEqual(self.notices, [])
        self.event({"type": "output_transcript", "text": "A spoken answer", "final": True})
        eventually(lambda: self.service.snapshot()["caption"] == "Voice: A spoken answer")
        self.assertEqual(self.notices, [])
        self.service.call(self.service.stop())
        self.start()
        self.assertEqual(self.service.snapshot()["caption"], "")

    def test_snapshot_uses_observed_audio_levels_and_quiet_connect(self):
        _, audio = self.start()
        audio.levels = lambda: (0.25, 0.75)
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["microphone_level"], 0.25)
        self.assertEqual(snapshot["speaker_level"], 0.75)
        self.assertEqual(self.notices, [])
        self.service.call(self.service.stop())
        self.assertEqual(self.service.snapshot()["microphone_level"], 0.0)
        self.assertEqual(self.service.snapshot()["speaker_level"], 0.0)
        self.assertEqual(self.notices, [])

    def test_session_limit_closes_media_without_routine_notice(self):
        with patch("service.MAX_SESSION_SECONDS", 0.02):
            client, audio = self.start()
            eventually(lambda: audio.stopped)
            eventually(lambda: self.service.snapshot()["state"] == "stopped")
        self.assertTrue(client.closed)
        self.assertEqual(self.notices, [])

    def test_session_settlement_closes_only_own_session(self):
        _, audio = self.start()
        self.service.call(self.service.session_settled("foreign"))
        self.assertFalse(audio.stopped)
        self.service.call(self.service.session_settled("host-session"))
        self.assertTrue(audio.stopped)
        self.assertEqual(self.service.snapshot()["state"], "stopped")

    def test_slow_admission_does_not_block_audio_or_barge_in(self):
        client, audio = self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)

        self.service.inject = slow
        self.event({"type": "handoff", "handoff_id": "slow", "text": "Inspect"})
        self.assertTrue(entered.wait(1))
        self.event({"type": "audio", "audio": b"\0\0" * 480})
        self.event({"type": "interrupted"})
        eventually(lambda: audio.interrupts == 1)
        self.assertTrue(audio.played)
        self.assertEqual(client.results, [])
        self.service.call(self.service.stop())
        release.set()
        self.assertEqual(client.results, [])

    def test_invalid_handoff_fails_without_injection(self):
        _, audio = self.start()
        self.event({"type": "handoff", "handoff_id": "id", "text": "x" * 20000})
        eventually(lambda: audio.stopped)
        self.assertEqual(self.injections, [])

    def test_failure_does_not_open_audio_before_connect(self):
        async def fail(_):
            raise RuntimeError("fixture-secret")
        original = FakeClient.connect
        FakeClient.connect = fail
        try:
            ticket = self.service.call(self.service.reserve(OWNER, "host-session", 3))
            self.service.call(self.service.activate(ticket, DEFAULTS, {}))
            eventually(lambda: self.service.snapshot()["state"] == "failed")
            self.assertEqual(self.audios, [])
            self.assertNotIn("fixture-secret", str(self.notices))
        finally:
            FakeClient.connect = original

    def test_plain_budget_and_control_removal(self):
        self.assertEqual(plain("ok\x00\x1b\x85\n"), "ok\n")
        with self.assertRaises(ValueError):
            plain("é" * 100, 100)


if __name__ == "__main__":
    unittest.main()
