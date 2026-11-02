"""Full JSON-RPC fixtures with fake provider/media; never record or spend."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "tests/sdk"))
from test_extension_v02 import RunningExtension, initialize_v02, rpc_request
from test_service import FakeClient, FakeAudio, eventually, OWNER

spec = importlib.util.spec_from_file_location("voice_extension", ROOT / "extension.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)
MANIFEST = tomllib.loads((ROOT / "extension.toml").read_text())
CONTEXT = {"workspace": "/fixture", "resource_owner": OWNER,
           "host": {"session_id": "host-session", "has_ui": True,
                    "model_view": {"provider": "codex", "id": "coding-model"}}}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.ext, self.service = voice.build_extension()
        self.ext.logger.stream = io.StringIO()
        self.clients, self.audios = [], []

        def client(*args):
            value = FakeClient(*args)
            self.clients.append(value)
            return value

        def audio(**kwargs):
            value = FakeAudio(**kwargs)
            self.audios.append(value)
            return value

        self.service.client_factory, self.service.audio_factory = client, audio
        def responder(message):
            if message.get("method") == "ui/close":
                return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

        self.host = RunningExtension(self.ext, responder=responder)
        self.addCleanup(self.service.close)
        self.addCleanup(self.host.shutdown)
        self.config_patch = patch.object(voice.settings, "load", return_value=dict(voice.settings.DEFAULTS))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.probe_patch = patch.object(voice, "probe", return_value={"available": True, "error": None})
        self.probe_patch.start()
        self.addCleanup(self.probe_patch.stop)
        self.offer = initialize_v02(**MANIFEST["contributes"],
            optional=["lifecycle_events", "message_injection", "provider_credentials", "remote_ui"])
        self.offer["params"]["api_version"] = "0.4"
        self.offer["params"]["protocol"]["version"] = "0.4"

    def initialize(self):
        response = self.host.start(self.offer)
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["shortcuts"], MANIFEST["contributes"]["shortcuts"])
        self.assertEqual(self.audios, [])
        self.assertEqual(self.clients, [])
        return response

    def command(self, identifier, arguments, context=CONTEXT):
        self.host.reader.feed(rpc_request(identifier, "command/execute", {
            "name": "voice", "arguments": arguments, "context": context}))

    def result(self, identifier):
        message = self.host.writer.wait_for(lambda msg: msg.get("id") == identifier)
        self.assertNotIn("error", message)
        return message["result"]

    def open_strip(self):
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "ui/open")
        self.assertEqual(request["params"], {"surface_id": "voice-1", "title": "Voice",
            "placement": "above_editor", "resource_owner": OWNER, "parent_request_id": 2})
        self.assertEqual(self.host.writer.matching(lambda msg: msg.get("method") == "provider/credentials"), [])
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"columns": 80, "rows": 24}})
        return request["params"]["surface_id"]

    def start(self):
        self.command(2, ["start"])
        self.open_strip()
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
        self.assertEqual(request["params"], {"provider": "codex", "model": "coding-model", "parent_request_id": 2})
        self.assertEqual(self.audios, [])
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {
            "ok": True, "apiKey": "fixture-token", "headers": {"ChatGPT-Account-Id": "fixture-account"}}})
        self.assertEqual(self.result(2)["text"], "")
        eventually(lambda: self.service.snapshot()["state"] == "listening")

    def test_inert_initialization_status_and_menu(self):
        self.initialize()
        self.command(2, ["status"])
        self.assertIn('"state": "stopped"', self.result(2)["text"])
        self.host.reader.feed(rpc_request(3, "menu/collect", {"context": CONTEXT}))
        menu = self.result(3)
        self.assertEqual(menu["status"]["state"], "stopped")
        self.assertTrue(all(item["command"] == "voice" for item in menu["items"]))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.audios, [])

    def test_portable_status_and_devices_do_not_open_streams_or_run_pipewire(self):
        import portable_audio

        sd = Mock()
        sd.query_devices.return_value = [{"name": "Built-in Microphone", "max_input_channels": 1,
            "max_output_channels": 0, "default_samplerate": 48000}]
        self.probe_patch.stop()
        with patch.object(portable_audio, "_sounddevice", return_value=sd) as native, \
                patch.object(voice.subprocess, "run") as run:
            self.initialize()
            native.assert_not_called()
            for index, platform in enumerate(("darwin", "win32")):
                with self.subTest(platform=platform), patch("sys.platform", platform):
                    status_id, devices_id = 10 + index * 2, 11 + index * 2
                    self.command(status_id, ["status"])
                    status = json.loads(self.result(status_id)["text"])
                    self.assertEqual(status["audio"]["backend"], "portaudio")
                    self.assertTrue(status["audio"]["available"])
                    self.command(devices_id, ["devices"])
                    devices = json.loads(self.result(devices_id)["text"])
                    self.assertEqual(devices[0]["target"], "Built-in Microphone")
                    self.assertEqual(devices[0]["input_channels"], 1)
                    self.assertEqual(self.audios, [])
                    self.assertEqual(self.clients, [])
            sd.RawInputStream.assert_not_called()
            sd.RawOutputStream.assert_not_called()
            run.assert_not_called()

    def test_portable_probe_failure_prevents_credentials_and_capture(self):
        import portable_audio

        self.probe_patch.stop()
        with patch("sys.platform", "win32"), patch.object(portable_audio, "_sounddevice",
                side_effect=voice.AudioError("Install sounddevice and its PortAudio runtime for native audio")):
            self.initialize()
            self.command(2, ["start"])
            self.open_strip()
            self.assertIn("Install sounddevice", self.result(2)["text"])
            self.assertEqual(self.audios, [])
            self.assertEqual(self.clients, [])
            self.assertEqual(self.host.writer.matching(
                lambda msg: msg.get("method") == "provider/credentials"), [])

    def test_portable_devices_report_missing_dependency(self):
        import portable_audio

        self.initialize()
        with patch("sys.platform", "darwin"), patch.object(portable_audio, "_sounddevice",
                side_effect=voice.AudioError("Install sounddevice and its PortAudio runtime for native audio")), \
                patch.object(voice.subprocess, "run") as run:
            self.command(2, ["devices"])
            self.assertIn("Install sounddevice", self.result(2)["text"])
            run.assert_not_called()
            self.assertEqual(self.audios, [])

    def test_linux_status_preserves_pipewire_probe(self):
        import audio
        import portable_audio

        self.probe_patch.stop()
        self.initialize()
        with patch("sys.platform", "linux"), \
                patch.object(audio, "probe", return_value={"available": True, "pw_record": "/pw-record"}) as probe, \
                patch.object(portable_audio, "_sounddevice", side_effect=AssertionError("native import")):
            self.command(2, ["status"])
            self.assertEqual(json.loads(self.result(2)["text"])["audio"]["pw_record"], "/pw-record")
            probe.assert_called_once_with()
            self.assertEqual(self.audios, [])

    def test_linux_devices_preserve_wpctl_listing(self):
        self.initialize()
        result = voice.subprocess.CompletedProcess([], 0, b"Audio\nnode.name\x1b[31m\n", b"")
        with patch("sys.platform", "linux"), \
                patch.object(voice, "_child_environment", return_value={}) as environment, \
                patch.object(voice.subprocess, "run", return_value=result) as run:
            self.command(2, ["devices"])
            self.assertEqual(self.result(2)["text"], "Audio\nnode.name[31m\n")
            environment.assert_called_once_with()
            run.assert_called_once_with(["wpctl", "status", "--name"], env={},
                capture_output=True, timeout=3, check=False)

    def test_full_duplex_handoff_shared_response_mute_stop_and_session_cleanup(self):
        self.initialize()
        self.start()
        client, audio = self.clients[0], self.audios[0]
        self.service._loop.call_soon_threadsafe(client.events.put_nowait,
            {"type": "handoff", "handoff_id": "h-1", "text": "Inspect tests"})
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "session/send_user_message")
        self.assertEqual(request["params"], {"text": voice.VOICE_INPUT_PREFIX + "Inspect tests", "deliver_as": "steer",
                         "resource_owner": OWNER, "parent_request_id": 2})
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}})
        eventually(lambda: bool(client.results))
        self.assertEqual(client.results[0][0], "h-1")
        self.assertFalse(client.results[0][2])
        self.host.reader.feed(rpc_request(20, "hook/run", {"hook": "before_prompt",
            "payload": {"prompt": voice.VOICE_INPUT_PREFIX + "Inspect tests"}, "context": CONTEXT}))
        self.result(20)
        self.assertEqual(client.texts, [])  # no reflection/redelegation loop
        self.host.reader.feed(rpc_request(3, "hook/run", {"hook": "after_response",
            "payload": {"response": "The agent answer"}, "context": CONTEXT}))
        self.result(3)
        eventually(lambda: len(client.results) == 2)
        self.assertEqual(client.results[1], (None, "The agent answer", True))
        self.host.reader.feed(rpc_request(4, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
        self.assertEqual(self.result(4)["text"], "")
        self.assertTrue(audio.muted)
        self.assertTrue(client.muted)
        self.host.reader.feed({"jsonrpc": "2.0", "method": "session/settled",
                               "params": {"session_id": "host-session", "outcome": "completed"}})
        eventually(lambda: audio.stopped)
        self.assertTrue(client.closed)

    def test_stop_cancels_pending_handoff_without_replay_or_completion_claim(self):
        self.initialize()
        self.start()
        client, audio = self.clients[0], self.audios[0]
        self.service._loop.call_soon_threadsafe(client.events.put_nowait,
            {"type": "handoff", "handoff_id": "pending", "text": "Inspect tests"})
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "session/send_user_message")
        self.command(3, ["stop"])
        self.assertEqual(self.result(3)["text"], "")
        cancellation = self.host.writer.wait_for(lambda msg: msg.get("method") == "$/cancelRequest"
            and msg.get("params", {}).get("id") == request["id"])
        self.assertTrue(cancellation["params"]["reason"])
        eventually(lambda: not self.ext._pending)
        self.assertTrue(audio.stopped)
        self.assertTrue(client.closed)
        self.assertEqual(client.results, [])
        # An ambiguous late ACK must neither restart media nor fabricate completion.
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}})
        self.assertEqual(self.service.snapshot()["state"], "stopped")
        self.assertEqual(len(self.host.writer.matching(
            lambda msg: msg.get("method") == "session/send_user_message")), 1)

    def saturated_teardown(self, method, params):
        self.initialize()
        self.start()
        acquired = 0
        while self.ext._admission.acquire(blocking=False):
            acquired += 1
        self.assertGreater(acquired, 0)
        try:
            self.host.reader.feed({"jsonrpc": "2.0", "method": method, "params": params})
            eventually(lambda: self.audios[0].stopped)
            eventually(lambda: self.clients[0].closed)
        finally:
            for _ in range(acquired):
                self.ext._admission.release()

    def test_session_retirement_closes_microphone_under_request_saturation(self):
        self.saturated_teardown("session/settled", {"session_id": "host-session", "outcome": "completed"})

    def test_surface_retirement_closes_microphone_under_request_saturation(self):
        self.saturated_teardown("ui/closed", {"surface_id": "voice-1", "reason": "owner_retired"})

    def test_stop_during_credential_resolution_revokes_start(self):
        self.initialize()
        self.command(2, ["start"])
        self.open_strip()
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
        self.host.reader.feed(rpc_request(3, "shortcut/execute", {"name": "voice-stop", "context": CONTEXT}))
        self.assertEqual(self.result(3)["text"], "")
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {
            "ok": True, "apiKey": "fixture-token", "headers": {"ChatGPT-Account-Id": "fixture-account"}}})
        self.assertEqual(self.result(2)["text"], "")
        self.assertEqual(self.clients, [])
        self.assertEqual(self.audios, [])

    def test_mute_while_credentials_pending_preserves_activation(self):
        self.initialize()
        self.command(2, ["start"])
        self.open_strip()
        request = self.host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
        self.host.reader.feed(rpc_request(3, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
        self.assertEqual(self.result(3)["text"], "")
        self.assertEqual(self.service.snapshot()["state"], "connecting")
        self.assertTrue(self.service.snapshot()["muted"])
        self.assertEqual(self.audios, [])
        self.host.reader.feed({"jsonrpc": "2.0", "id": request["id"], "result": {
            "ok": True, "apiKey": "fixture-token", "headers": {"ChatGPT-Account-Id": "fixture-account"}}})
        self.assertEqual(self.result(2)["text"], "")
        eventually(lambda: self.service.snapshot()["state"] == "muted")
        self.assertTrue(self.audios[0].muted)
        self.assertTrue(self.clients[0].muted)

    def test_cancel_during_credential_resolution_opens_nothing(self):
        self.initialize()
        self.command(2, ["start"])
        self.open_strip()
        self.host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
        self.host.reader.feed({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 2}})
        reply = self.host.writer.wait_for(lambda msg: msg.get("id") == 2)
        self.assertEqual(reply["error"]["code"], -32800)
        self.assertEqual(self.clients, [])
        self.assertEqual(self.audios, [])
        self.assertEqual(self.service.snapshot()["state"], "failed")

    def test_strip_frames_resize_owner_retirement_and_quiet_controls(self):
        self.initialize()
        self.start()
        frame = self.host.writer.wait_for(lambda msg: msg.get("method") == "ui/frame")
        params = frame["params"]
        self.assertEqual(params["surface_id"], "voice-1")
        self.assertEqual(params["resource_owner"], OWNER)
        self.assertEqual((params["columns"], params["rows"]), (80, 24))
        self.assertTrue(any("VOICE" in line for line in params["lines"]))
        self.host.reader.feed({"jsonrpc": "2.0", "method": "ui/resize", "params": {
            "surface_id": "voice-1", "columns": 31, "rows": 12}})
        resized = self.host.writer.wait_for(lambda msg: msg.get("method") == "ui/frame"
            and msg["params"]["columns"] == 31)
        self.assertGreater(resized["params"]["revision"], params["revision"])
        self.assertEqual(resized["params"]["rows"], 12)
        self.host.reader.feed({"jsonrpc": "2.0", "method": "ui/closed", "params": {
            "surface_id": "voice-1", "reason": "owner_retired"}})
        eventually(lambda: self.audios[0].stopped)
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.host.writer.matching(lambda msg: msg.get("method") == "notification"), [])

    def test_no_required_services_or_foreground_means_no_start(self):
        self.offer["params"]["protocol"]["optional_features"] = []
        self.initialize()
        self.command(2, ["start"])
        self.assertIn("did not offer", self.result(2)["text"])
        self.assertEqual(self.clients, [])

    def test_headless_is_refused_without_credentials_or_devices(self):
        self.initialize()
        context = copy.deepcopy(CONTEXT)
        context["host"]["has_ui"] = False
        self.command(2, ["start"], context)
        self.assertIn("interactive", self.result(2)["text"])
        self.assertEqual(self.clients, [])
        self.assertEqual(self.audios, [])


if __name__ == "__main__":
    unittest.main()
