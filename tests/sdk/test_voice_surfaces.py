"""Generic static shortcuts and explicitly selected reverse host services."""

import copy
import io
import os
import threading
import unittest
from unittest.mock import patch

from octet_extension import CancelledError, Extension, RpcError
from test_extension_v02 import RunningExtension, initialize_v02, rpc_request


REQUIRED = ["request_cancellation", "content_parts"]
SERVICES = ["message_injection", "provider_credentials"]
SHORTCUT = {"name": "voice-mute", "key": "ctrl+shift+m", "description": "Mute voice"}
CONTEXT = {"workspace": "/workspace", "host": {}, "resource_owner": {
    "session_id": "host-issued-owner", "extension_instance_id": "host-instance",
    "process_generation": 3,
}}


def initialize(version="0.4", *, shortcuts=None, optional=None):
    message = initialize_v02(
        shortcuts=[copy.deepcopy(SHORTCUT)] if shortcuts is None else shortcuts,
        optional=[] if optional is None else optional,
    )
    message["params"]["api_version"] = version
    if version == "0.1":
        del message["params"]["protocol"]
    else:
        message["params"]["protocol"]["version"] = version
    return message


class VoiceSurfacesTests(unittest.TestCase):
    def test_static_shortcut_echoes_manifest_and_uses_ordinary_request_context(self):
        for version in ("0.2", "0.4"):
            with self.subTest(version=version):
                extension = Extension(api_version=version, stderr=io.StringIO())
                observed = []
                outputs = iter([None, "Muted", {"text": "Ready", "notifications": [
                    {"level": "info", "message": "Muted"}], "context": []}])

                @extension.shortcut(name="voice-mute", description="Mute voice")
                def mute(context):
                    observed.append((context, extension.request_id, extension.cancellation.request_id))
                    extension.cancellation.raise_if_cancelled()
                    return next(outputs)

                host = RunningExtension(extension)
                response = host.start(initialize(version))
                try:
                    self.assertEqual(response["result"]["shortcuts"], [SHORTCUT])
                    self.assertEqual(response["result"]["commands"], [])
                    self.assertEqual(set(response["result"]["protocol"]["features"]), set(REQUIRED))
                    for request_id, text in enumerate(["", "Muted", "Ready"], start=2):
                        host.reader.feed(rpc_request(request_id, "shortcut/execute", {
                            "name": "voice-mute", "context": CONTEXT,
                        }))
                        result = host.writer.wait_for(lambda msg: msg.get("id") == request_id)["result"]
                        self.assertEqual(result["text"], text)
                        self.assertEqual(result["context"], [])
                        self.assertEqual(len(result["notifications"]), int(request_id == 4))
                    self.assertEqual(observed, [(CONTEXT, i, i) for i in range(2, 5)])
                finally:
                    host.shutdown()

    def test_exact_shortcut_declarations_and_malformed_definitions_fail_initialize(self):
        bad_definitions = [
            [], [{**SHORTCUT, "name": "other"}], [SHORTCUT, SHORTCUT],
            [SHORTCUT, {**SHORTCUT, "name": "other", "key": "CTRL+SHIFT+M"}],
            ["voice-mute"], [{"name": "voice-mute"}],
            [{**SHORTCUT, "extra": True}], [{**SHORTCUT, "name": 1}],
            [{**SHORTCUT, "name": "bad name"}], [{**SHORTCUT, "key": ""}],
            [{**SHORTCUT, "key": " ctrl+m"}], [{**SHORTCUT, "key": "k" * 129}],
            [{**SHORTCUT, "description": ""}], [{**SHORTCUT, "description": "bad\ntext"}],
            [{**SHORTCUT, "description": "x" * 4097}], {}, None,
        ]
        for definitions in bad_definitions:
            with self.subTest(definitions=definitions):
                extension = Extension(api_version="0.4", stderr=io.StringIO())
                extension.shortcut(name="voice-mute")(lambda context: None)
                request = initialize()
                request["params"]["contributes"]["shortcuts"] = definitions
                self.assert_initialize_error(extension, request)
        # Omission cannot silently admit a registered shortcut.
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        extension.shortcut(name="voice-mute")(lambda context: None)
        request = initialize()
        del request["params"]["contributes"]["shortcuts"]
        self.assert_initialize_error(extension, request)
        # Neither an unhandled declaration nor a different description is admitted.
        self.assert_initialize_error(Extension(api_version="0.4", stderr=io.StringIO()), initialize())
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        extension.shortcut(name="voice-mute", description="Different")(lambda context: None)
        self.assert_initialize_error(extension, initialize())

    def assert_initialize_error(self, extension, request):
        host = RunningExtension(extension)
        try:
            response = host.start(request)
            self.assertEqual(response["error"]["code"], -32602)
            self.assertFalse(extension.initialized)
        finally:
            host.reader.close()
            host.thread.join(2)
            self.assertFalse(host.thread.is_alive())

    def test_registration_validation_and_frozen_api_0_1(self):
        extension = Extension(api_version="0.4")
        for name in (None, "", "bad name", "1bad", "é", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                extension.shortcut(name=name)
        for description in ("", 3):
            with self.assertRaises(ValueError):
                extension.shortcut(name="voice-mute", description=description)
        extension.shortcut(name="voice-mute")(lambda context: None)
        with self.assertRaises(ValueError):
            extension.shortcut(name="voice-mute")(lambda context: None)
        legacy = Extension(api_version="0.1", stderr=io.StringIO())
        legacy.shortcut(name="voice-mute")(lambda context: None)
        self.assert_initialize_error(legacy, initialize("0.1", shortcuts=[]))
        host = RunningExtension(Extension(api_version="0.1", stderr=io.StringIO()))
        try:
            response = host.start(initialize("0.1", shortcuts=[]))
            self.assertEqual(response["result"], {"api_version": "0.1", "tools": [], "commands": []})
        finally:
            host.shutdown()

    def test_bad_dispatch_never_invokes_handler(self):
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        invoked = []
        extension.shortcut(name="voice-mute")(lambda context: invoked.append(context))
        host = RunningExtension(extension)
        host.start(initialize())
        try:
            for request_id, (params, code) in enumerate([
                ([], -32602), ({}, -32602), ({"name": None}, -32602),
                ({"name": []}, -32602), ({"name": "bad name"}, -32602),
                ({"name": "unknown"}, -32601),
                ({"name": "voice-mute", "context": []}, -32602),
                ({"name": "voice-mute", "arguments": []}, -32602),
            ], start=2):
                host.reader.feed(rpc_request(request_id, "shortcut/execute", params))
                result = host.writer.wait_for(lambda msg: msg.get("id") == request_id)
                self.assertEqual(result["error"]["code"], code)
            self.assertEqual(invoked, [])
        finally:
            host.shutdown()

    def test_features_are_explicit_and_only_selected_when_offered(self):
        for supported, offered, expected in [
            (None, SERVICES, []), (REQUIRED + SERVICES, [], []),
            (REQUIRED + SERVICES, ["message_injection"], ["message_injection"]),
            (REQUIRED + SERVICES, SERVICES + ["shortcuts", "lifecycle_events_v2"], SERVICES),
        ]:
            for version in ("0.2", "0.4"):
                with self.subTest(supported=supported, offered=offered, version=version):
                    extension = Extension(api_version=version, supported_features=supported, stderr=io.StringIO())
                    host = RunningExtension(extension)
                    try:
                        response = host.start(initialize(version, shortcuts=[], optional=offered))
                        self.assertEqual(set(response["result"]["protocol"]["features"]), set(REQUIRED + expected))
                    finally:
                        host.shutdown()

    def test_raw_requests_carry_parent_and_exact_host_issued_owner(self):
        extension = Extension(api_version="0.4", supported_features=REQUIRED + SERVICES, stderr=io.StringIO())
        captured = []
        retained = []

        def responder(message):
            if message.get("method") in ("session/send_user_message", "provider/credentials"):
                captured.append(message)
                result = {} if message["method"].startswith("session/") else {"credential": "private-test-value"}
                return {"jsonrpc": "2.0", "id": message["id"], "result": result}

        @extension.shortcut(name="voice-mute")
        def mute(context):
            retained.append((extension.request_id, context["resource_owner"]))
            self.assertEqual(extension.request("session/send_user_message", {
                "text": "Transcribed speech", "resource_owner": context["resource_owner"],
            }, operation_scoped=True), {})
            result = extension.request("provider/credentials", {
                "provider": "openai", "model": "whisper-1", "resource_owner": context["resource_owner"],
            }, operation_scoped=True)
            self.assertEqual(result, {"credential": "private-test-value"})
            return "Ready"

        host = RunningExtension(extension, responder=responder)
        host.start(initialize(optional=SERVICES))
        try:
            host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
            response = host.writer.wait_for(lambda msg: msg.get("id") == 2)
            self.assertEqual(response["result"]["text"], "Ready")
            self.assertEqual(len(captured), 2)
            for message in captured:
                self.assertEqual(message["params"]["parent_request_id"], 2)
                self.assertEqual(message["params"]["resource_owner"], CONTEXT["resource_owner"])
            # Deferred callbacks explicitly return the original parent and owner.
            parent, owner = retained[0]
            outcomes = []
            thread = threading.Thread(target=lambda: outcomes.append(extension.request(
                "session/send_user_message", {"text": "Later speech", "resource_owner": owner},
                parent_request_id=parent, operation_scoped=True,
            )))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, [{}])
            self.assertEqual(captured[-1]["params"], {
                "parent_request_id": 2, "resource_owner": owner, "text": "Later speech",
            })
            ids = [message["id"] for message in captured]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertNotIn("private-test-value", str(response))
        finally:
            host.shutdown()

    def test_request_timeout_cancels_once_without_retry_and_releases_worker(self):
        extension = Extension(api_version="0.4", max_concurrent_requests=1, stderr=io.StringIO())
        outcomes = []

        @extension.shortcut(name="voice-mute")
        def mute(context):
            try:
                extension.request("session/send_user_message", {"text": "Speech"},
                                  operation_scoped=True, timeout=0.03)
            except TimeoutError:
                outcomes.append("timeout")
            return "Worker released"

        host = RunningExtension(extension)
        host.start(initialize(optional=["message_injection"]))
        try:
            host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
            response = host.writer.wait_for(lambda msg: msg.get("id") == 2)
            self.assertEqual(response["result"]["text"], "Worker released")
            self.assertEqual(outcomes, ["timeout"])
            sent = host.writer.matching(lambda msg: msg.get("method") == "session/send_user_message")
            self.assertEqual(len(sent), 1)
            child_id = sent[0]["id"]
            cancellations = host.writer.matching(lambda msg: msg.get("method") == "$/cancelRequest")
            self.assertEqual(cancellations, [{"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {
                "id": child_id, "reason": "timeout",
            }}])
            self.assertEqual(extension._pending, {})
            # An ambiguous late success is not replayed and cannot revive the waiter.
            host.reader.feed({"jsonrpc": "2.0", "id": child_id, "result": {}})
            self.assertEqual(host.shutdown()["result"], {})
        finally:
            if host.thread.is_alive():
                host.shutdown(request_id=901)

    def test_request_timeout_validation_and_response_before_deadline(self):
        extension = Extension(api_version="0.4", stderr=io.StringIO())

        def responder(message):
            if message.get("method") == "provider/credentials":
                return {"jsonrpc": "2.0", "id": message["id"], "result": {"value": "test"}}

        host = RunningExtension(extension, responder=responder)
        host.start(initialize(shortcuts=[]))
        try:
            for timeout in (True, False, 0, -1, float("inf"), float("nan"), "1"):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    extension.request("provider/credentials", timeout=timeout)
            for timeout in (None, 1, 0.5):
                self.assertEqual(extension.request("provider/credentials", {
                    "provider": "test", "model": "test", "resource_owner": CONTEXT["resource_owner"],
                }, parent_request_id=2, timeout=timeout), {"value": "test"})
            self.assertEqual(host.writer.matching(lambda msg: msg.get("method") == "$/cancelRequest"), [])
        finally:
            host.shutdown()

    def test_explicit_cancellation_event_validation_and_before_send(self):
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        host = RunningExtension(extension)
        host.start(initialize(shortcuts=[]))
        try:
            for event in (True, False, 1, object(), threading.Lock()):
                with self.subTest(event=event), self.assertRaises(TypeError):
                    extension.request("provider/credentials", cancellation_event=event)
            event = threading.Event()
            event.set()
            with self.assertRaises(CancelledError):
                extension.request("provider/credentials", cancellation_event=event)
            self.assertEqual(extension._pending, {})
            self.assertEqual(host.writer.matching(lambda msg: msg.get("method") in (
                "provider/credentials", "$/cancelRequest")), [])
        finally:
            host.shutdown()

    def test_explicit_cancellation_in_flight_cancels_once_and_ignores_late_reply(self):
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        host = RunningExtension(extension)
        host.start(initialize(shortcuts=[]))
        event = threading.Event()
        outcomes = []

        def request():
            try:
                extension.request("provider/credentials", cancellation_event=event, timeout=1)
            except CancelledError as error:
                outcomes.append(error.data)

        thread = threading.Thread(target=request)
        try:
            thread.start()
            sent = host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
            event.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, [{"reason": "cancelled"}])
            cancellation = host.writer.wait_for(lambda msg: msg.get("method") == "$/cancelRequest")
            self.assertEqual(cancellation["params"], {"id": sent["id"], "reason": "cancelled"})
            self.assertEqual(len(host.writer.matching(lambda msg: msg.get("method") == "$/cancelRequest")), 1)
            self.assertEqual(extension._pending, {})
            host.reader.feed({"jsonrpc": "2.0", "id": sent["id"], "result": {"late": True}})
        finally:
            event.set()
            thread.join(1)
            host.shutdown()

    def test_unset_explicit_cancellation_preserves_response_timeout_and_parent_cancellation(self):
        for outcome in ("response", "timeout", "parent"):
            with self.subTest(outcome=outcome):
                extension = Extension(api_version="0.4", stderr=io.StringIO())
                event = threading.Event()
                observed = []

                @extension.shortcut(name="voice-mute")
                def mute(context):
                    try:
                        observed.append(extension.request("provider/credentials", cancellation_event=event,
                                                          timeout=0.03 if outcome == "timeout" else 1))
                    except (TimeoutError, CancelledError) as error:
                        observed.append(type(error).__name__)
                    return "done"

                host = RunningExtension(extension)
                host.start(initialize())
                try:
                    host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
                    sent = host.writer.wait_for(lambda msg: msg.get("method") == "provider/credentials")
                    if outcome == "response":
                        host.reader.feed({"jsonrpc": "2.0", "id": sent["id"], "result": {"ok": True}})
                    elif outcome == "parent":
                        host.reader.feed({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {
                            "id": 2, "reason": "owner_retired",
                        }})
                    host.writer.wait_for(lambda msg: msg.get("id") == 2)
                    expected = {"response": {"ok": True}, "timeout": "TimeoutError", "parent": "CancelledError"}
                    self.assertEqual(observed, [expected[outcome]])
                    if outcome != "response":
                        cancellation = host.writer.wait_for(lambda msg: msg.get("method") == "$/cancelRequest")
                        self.assertEqual(cancellation["params"], {"id": sent["id"], "reason":
                            "timeout" if outcome == "timeout" else "owner_retired"})
                finally:
                    host.shutdown()

    def test_teardown_notifications_bypass_saturated_requests_and_ui_observations(self):
        for saturation in ("request", "ui"):
            with self.subTest(saturation=saturation):
                extension = Extension(api_version="0.4", max_concurrent_requests=1, max_pending_requests=1,
                    supported_features=REQUIRED + ["remote_ui", "lifecycle_events"], stderr=io.StringIO())
                started, release = threading.Event(), threading.Event()
                observed = []
                completed = {method: threading.Event() for method in ("session/settled", "ui/closed")}

                def block(params):
                    started.set()
                    release.wait(2)
                    return "done"

                extension.shortcut(name="voice-mute")(block)
                extension.on_ui_event("ui/resize")(block)

                def teardown(params):
                    # A reverse request from this lane must still receive its reply
                    # while the ordinary worker is blocked and its admission is full.
                    observed.append((params, extension.request("test/reply", timeout=1), extension.request_id))
                    completed[params["event"]].set()

                extension.on_lifecycle("session/settled")(teardown)
                extension.on_ui_event("ui/closed")(teardown)

                def responder(message):
                    if message.get("method") == "test/reply":
                        return {"jsonrpc": "2.0", "id": message["id"], "result": "routed"}

                host = RunningExtension(extension, responder=responder)
                host.start(initialize(optional=["remote_ui", "lifecycle_events"]))
                try:
                    if saturation == "request":
                        host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
                    else:
                        host.reader.feed({"jsonrpc": "2.0", "method": "ui/resize", "params": {}})
                    self.assertTrue(started.wait(1))
                    # Serialize critical observations with a reply barrier, since this
                    # test deliberately gives the teardown lane only one admission slot.
                    for method in ("session/settled", "ui/closed"):
                        host.reader.feed({"jsonrpc": "2.0", "method": method, "params": {"event": method}})
                        self.assertTrue(completed[method].wait(1))
                        self.assertFalse(extension._wait_for_futures(0))  # Ordinary work is still blocked.
                        # Wait for admission release, not just handler return.
                        self.assertTrue(extension._teardown_admission.acquire(timeout=1))
                        extension._teardown_admission.release()
                    self.assertEqual(observed, [({"event": method}, "routed", None)
                        for method in ("session/settled", "ui/closed")])
                    self.assertNotIn("teardown notification queue is full", extension.logger.stream.getvalue())
                finally:
                    release.set()
                    host.shutdown()

    def test_teardown_overflow_fails_closed_runs_cleanup_and_releases_pending_requests(self):
        extension = Extension(api_version="0.4", max_pending_requests=1, shutdown_timeout=0.1,
            cancellation_grace=0.05, supported_features=REQUIRED + ["remote_ui", "lifecycle_events"],
            stderr=io.StringIO())
        started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        ordinary_started = threading.Event()
        outcomes = []

        @extension.shortcut(name="voice-mute")
        def mute(context):
            ordinary_started.set()
            release.wait(2)
            return "done"

        @extension.on_lifecycle("session/settled")
        def settled(params):
            started.set()
            release.wait(2)

        extension.on_ui_event("ui/closed")(lambda params: self.fail("overflow must stop dispatch"))

        @extension.on_shutdown
        def shutdown(params):
            outcomes.append(params)
            cleaned.set()  # Stand-in for releasing the microphone.
            release.set()

        host = RunningExtension(extension)
        host.start(initialize(optional=["remote_ui", "lifecycle_events"]))
        pending = []

        def request():
            try:
                extension.request("test/no-reply")
            except RpcError as error:
                pending.append(error.message)

        thread = threading.Thread(target=request)
        try:
            thread.start()
            host.writer.wait_for(lambda msg: msg.get("method") == "test/no-reply")
            host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
            self.assertTrue(ordinary_started.wait(1))
            host.reader.feed({"jsonrpc": "2.0", "method": "session/settled", "params": {}})
            self.assertTrue(started.wait(1))
            host.reader.feed({"jsonrpc": "2.0", "method": "ui/closed", "params": {}})
            self.assertTrue(cleaned.wait(1))
            thread.join(1)
            host.thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertFalse(host.thread.is_alive())
            self.assertEqual(outcomes, [{"reason": "teardown_overflow"}])
            self.assertEqual(len(pending), 1)
            self.assertIn(pending[0], ("teardown notification queue is full", "extension stopped"))
            self.assertFalse(extension.running)
            self.assertEqual(extension._pending, {})
        finally:
            release.set()
            host.reader.close()
            thread.join(1)
            host.thread.join(1)

    def test_teardown_overflow_cannot_hang_run_on_stuck_shutdown_hook(self):
        extension = Extension(api_version="0.4", max_pending_requests=1, shutdown_timeout=0.05,
                              cancellation_grace=0.05, stderr=io.StringIO())
        started, hook_started, release = threading.Event(), threading.Event(), threading.Event()

        @extension.on_lifecycle("session/settled")
        def settled(params):
            started.set()
            release.wait(2)

        @extension.on_shutdown
        def shutdown(params):
            hook_started.set()
            release.wait(2)

        host = RunningExtension(extension)
        host.start(initialize(shortcuts=[], optional=["lifecycle_events"]))
        try:
            host.reader.feed({"jsonrpc": "2.0", "method": "session/settled", "params": {}})
            self.assertTrue(started.wait(1))
            host.reader.feed({"jsonrpc": "2.0", "method": "session/settled", "params": {}})
            self.assertTrue(hook_started.wait(1))
            host.thread.join(1)
            self.assertFalse(host.thread.is_alive())
        finally:
            release.set()
            host.reader.close()
            host.thread.join(1)

    def test_remote_ui_uses_resolved_environment_api_version(self):
        with patch.dict(os.environ, {"OCTET_EXTENSION_API_VERSION": "0.4"}):
            extension = Extension(supported_features=REQUIRED + ["remote_ui"])
        self.assertEqual(extension.api_version, "0.4")

    def test_remote_ui_is_explicit_and_api_0_4_only(self):
        for version in ("0.1", "0.2", "0.3"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                Extension(api_version=version, supported_features=REQUIRED + ["remote_ui"])
        for supported, offered, expected in [(None, ["remote_ui"], False),
                (REQUIRED + ["remote_ui"], [], False),
                (REQUIRED + ["remote_ui"], ["remote_ui"], True)]:
            extension = Extension(api_version="0.4", supported_features=supported, stderr=io.StringIO())
            host = RunningExtension(extension)
            try:
                response = host.start(initialize(shortcuts=[], optional=offered))
                self.assertEqual("remote_ui" in response["result"]["protocol"]["features"], expected)
                frame = {"surface_id": "voice", "resource_owner": CONTEXT["resource_owner"],
                         "columns": 80, "rows": 24, "revision": 1, "lines": ["Voice"]}
                if expected:
                    extension.publish_ui_frame(frame)
                    self.assertEqual(host.writer.wait_for(lambda msg: msg.get("method") == "ui/frame"),
                        {"jsonrpc": "2.0", "method": "ui/frame", "params": frame})
                else:
                    with self.assertRaises(ValueError):
                        extension.publish_ui_frame(frame)
            finally:
                host.shutdown()

    def test_remote_ui_notifications_dispatch_only_when_negotiated(self):
        methods = ("ui/key", "ui/mouse", "ui/resize", "ui/closed", "ui/editor-text", "context/updated")
        for offered in ([], ["remote_ui"]):
            extension = Extension(api_version="0.4", supported_features=REQUIRED + ["remote_ui"],
                                  stderr=io.StringIO())
            observed = []
            ready = threading.Event()
            for method in methods:
                def handle(params, method=method):
                    observed.append((method, params, threading.current_thread().name))
                    if len(observed) == len(methods):
                        ready.set()
                extension.on_ui_event(method)(handle)
            with self.assertRaises(ValueError):
                extension.on_ui_event("ui/focus")
            with self.assertRaises(ValueError):
                extension.on_ui_event("ui/key")(lambda params: None)
            host = RunningExtension(extension)
            host.start(initialize(shortcuts=[], optional=offered))
            try:
                for method in methods:
                    host.reader.feed({"jsonrpc": "2.0", "method": method,
                                      "params": {"surface_id": "voice", "event": method}})
                if offered:
                    self.assertTrue(ready.wait(2))
                    self.assertEqual({(method, params["event"]) for method, params, _ in observed},
                                     {(method, method) for method in methods})
                    self.assertTrue(all(name != host.thread.name for _, _, name in observed))
                else:
                    self.assertFalse(ready.wait(0.05))
                    self.assertEqual(observed, [])
            finally:
                host.shutdown()
        for version in ("0.1", "0.2"):
            extension = Extension(api_version=version)
            with self.assertRaises(ValueError):
                extension.on_ui_event("ui/resize")
            with self.assertRaises(ValueError):
                extension.publish_ui_frame({})

    def test_remote_ui_shares_bounded_dispatch_without_blocking_reader(self):
        extension = Extension(api_version="0.4", supported_features=REQUIRED + ["remote_ui"],
                              max_concurrent_requests=1, max_pending_requests=1, stderr=io.StringIO())
        started, release = threading.Event(), threading.Event()
        events = []

        @extension.on_ui_event("ui/resize")
        def resize(params):
            events.append(params)
            started.set()
            release.wait(2)

        extension.shortcut(name="voice-mute")(lambda context: "done")
        host = RunningExtension(extension)
        host.start(initialize(optional=["remote_ui"]))
        try:
            host.reader.feed({"jsonrpc": "2.0", "method": "ui/resize", "params": {"revision": 1}})
            self.assertTrue(started.wait(1))
            host.reader.feed({"jsonrpc": "2.0", "method": "ui/resize", "params": {"revision": 2}})
            host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
            response = host.writer.wait_for(lambda msg: msg.get("id") == 2)
            self.assertEqual(response["error"]["message"], "extension request queue is full")
            self.assertEqual(events, [{"revision": 1}])
            self.assertIn("dropped lifecycle event because request queue is full", extension.logger.stream.getvalue())
        finally:
            release.set()
            host.shutdown()

    def test_shortcut_uses_cooperative_cancellation(self):
        extension = Extension(api_version="0.4", stderr=io.StringIO())
        started = threading.Event()

        @extension.shortcut(name="voice-mute")
        def mute(context):
            started.set()
            extension.cancellation.wait(2)
            extension.cancellation.raise_if_cancelled()

        host = RunningExtension(extension)
        host.start(initialize())
        try:
            host.reader.feed(rpc_request(2, "shortcut/execute", {"name": "voice-mute", "context": CONTEXT}))
            self.assertTrue(started.wait(1))
            host.reader.feed({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 2}})
            response = host.writer.wait_for(lambda msg: msg.get("id") == 2)
            self.assertEqual(response["error"]["code"], -32800)
        finally:
            host.shutdown()


if __name__ == "__main__":
    unittest.main()
