"""Provider-free protocol/transport tests. No credentials, sockets or recording."""
import asyncio
import base64
import json
import sys
import types
import unittest
from unittest.mock import patch

import realtime as rt


class Socket:
    def __init__(self, initial=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.failure = False
        for event in initial or []:
            self.feed(event)

    def feed(self, event):
        self.incoming.put_nowait(types.SimpleNamespace(data=json.dumps(event)))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def send_str(self, text):
        if self.failure:
            raise RuntimeError("SECRET upstream body/header/token")
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class Response:
    status = 201
    headers = {"Location": "/v1/live/rtc_fake"}

    def __init__(self, body=b"v=0\r\n"):
        self.body = body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, size):
        for pos in range(0, len(self.body), size):
            yield self.body[pos:pos + size]


class HTTP:
    def __init__(self, socket=None, response=None):
        self.socket = socket or Socket([{"type": "session.started"}])
        self.response = response or Response()
        self.calls = []
        self.closed = False

    async def ws_connect(self, url, **kwargs):
        self.calls.append(("ws", url, kwargs))
        return self.socket

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    async def close(self):
        self.closed = True


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn
        return register


class Peer(Emitter):
    last = None

    def __init__(self, configuration):
        super().__init__()
        Peer.last = self
        self.configuration = configuration
        self.channel = Emitter()
        self.channel.label = None
        self.localDescription = types.SimpleNamespace(sdp="v=0\r\no=fake-offer")
        self.connectionState = "new"
        self.closed = False

    def addTrack(self, track):
        self.track = track

    def createDataChannel(self, label, **kwargs):
        self.channel.label, self.channel.options = label, kwargs
        return self.channel

    async def createOffer(self):
        return self.localDescription

    async def setLocalDescription(self, offer):
        pass

    async def setRemoteDescription(self, answer):
        self.answer = answer
        self.channel.handlers["open"]()

    async def close(self):
        self.closed = True


class Track:
    def stop(self):
        pass


class Frame:
    def __init__(self, format, layout, samples):
        self.format, self.layout, self.samples = format, layout, samples
        self.data = b""
        self.planes = [types.SimpleNamespace(update=self.update)]

    def update(self, pcm):
        self.data = pcm


RTC_MODULES = {
    "aiortc": types.SimpleNamespace(RTCPeerConnection=Peer,
        RTCConfiguration=lambda **kw: kw, RTCSessionDescription=lambda **kw: types.SimpleNamespace(**kw),
        MediaStreamTrack=Track),
    "av": types.SimpleNamespace(AudioFrame=Frame, AudioResampler=object),
}


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await asyncio.sleep(0)

    def client(self, config=None, credential=None):
        client = rt.RealtimeClient(config or {}, credential or {"api_key": "fake-key"})
        self.clients.append(client)
        return client

    async def connect_ws(self, config=None):
        client = self.client(config)
        http = HTTP(Socket([{"type": "session.updated"}]))
        with patch.object(rt, "_http_session", return_value=http):
            await client.connect()
        self.assertEqual((await client.receive())["type"], "ready")
        return client, http

    async def handle(self, client, event):
        await client._handle(json.dumps(event))

    async def test_public_bidi_session_audio_text_and_handoff(self):
        c, http = await self.connect_ws()
        url, options = http.calls[0][1:]
        self.assertEqual(url, rt.WS_OPENAI + "?model=gpt-realtime-1.5")
        self.assertEqual(options["headers"], {"Authorization": "Bearer fake-key"})
        session = http.socket.sent[0]["session"]
        self.assertEqual(session["output_modalities"], ["audio"])
        self.assertTrue(session["audio"]["input"]["turn_detection"]["interrupt_response"])
        self.assertEqual(session["audio"]["output"]["format"]["rate"], 24000)
        self.assertEqual([t["name"] for t in session["tools"]], ["background_agent", "remain_silent"])
        await c.send_audio(b"\x01\0" * 240)
        self.assertEqual(http.socket.sent[-1]["type"], "input_audio_buffer.append")
        audio = base64.b64encode(b"\x02\0" * 240).decode()
        await self.handle(c, {"type": "response.output_audio.delta", "delta": audio})
        self.assertEqual((await c.receive())["audio"], b"\x02\0" * 240)
        await c.send_text("Hello")
        self.assertEqual(http.socket.sent[-1], {"type": "response.create"})
        event = {"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "background_agent", "call_id": "call1",
            "arguments": json.dumps({"prompt": "fix the tests"})}}
        await self.handle(c, event)
        self.assertEqual(await c.receive(), {"type": "handoff", "handoff_id": "call1", "text": "fix the tests"})
        await self.handle(c, event)
        self.assertTrue(c._events.empty())
        await self.handle(c, {"type": "response.done"})
        await c.send_result("call1", "admitted, not completed", final=False)
        self.assertEqual(http.socket.sent[-2]["item"], {
            "type": "function_call_output", "call_id": "call1", "output": "admitted, not completed"})
        await c.send_result(None, "shared update")
        self.assertEqual(http.socket.sent[-1]["item"]["role"], "developer")
        await self.handle(c, {"type": "response.done"})
        self.assertEqual(http.socket.sent[-1], {"type": "response.create"})
        with self.assertRaises(rt.RealtimeError):
            await c.send_result("call1", "no replay")

    async def test_silence_is_completed_once_without_response(self):
        c, http = await self.connect_ws()
        event = {"type": "conversation.item.done", "item": {
            "name": "remain_silent", "call_id": "silent", "arguments": "{}"}}
        before = len(http.socket.sent)
        await self.handle(c, event)
        await self.handle(c, event)
        self.assertEqual(len(http.socket.sent), before + 1)
        self.assertEqual(http.socket.sent[-1]["item"]["output"], "")
        self.assertTrue(c._events.empty())

    async def test_transcripts_and_barge_in_drop_queued_audio_not_handoff(self):
        c, _ = await self.connect_ws()
        c._emit({"type": "audio", "pcm": b"\0\0"})
        c._emit({"type": "handoff", "handoff_id": "keep", "prompt": "work"})
        await self.handle(c, {"type": "input_audio_buffer.speech_started"})
        self.assertEqual((await c.receive())["type"], "handoff")
        self.assertEqual((await c.receive())["type"], "interrupted")
        for kind, field, normalized in [
            ("conversation.item.input_audio_transcription.completed", "transcript", "input_transcript"),
            ("response.output_audio_transcript.delta", "delta", "output_transcript")]:
            await self.handle(c, {"type": kind, field: "hello"})
            event = await c.receive()
            self.assertEqual((event["type"], event["text"]), (normalized, "hello"))
            self.assertEqual(event["final"], kind.endswith("completed"))

    async def test_codex_api_key_uses_verified_live_path(self):
        c, http = await self.connect_ws({"backend": "codex"})
        self.assertEqual(http.calls[0][1], rt.WS_CODEX + "?model=gpt-live-1-codex")
        self.assertEqual(http.calls[0][2]["headers"]["openai-alpha"], "quicksilver=v2")
        self.assertEqual(http.socket.sent[0]["session"]["delegation"], {"type": "client"})
        await c.send_audio(b"\0\0")
        self.assertEqual(http.socket.sent[-1]["type"], "input_audio.append")
        await self.handle(c, {"type": "delegation.created", "item": {
            "id": "deleg1", "type": "delegation", "target": "client",
            "content": [{"type": "input_text", "text": "work"}]}})
        self.assertEqual((await c.receive())["text"], "work")
        text = "🙂" * 200
        await c.send_result("deleg1", text)
        parts = [e for e in http.socket.sent if e["type"] == "delegation.context.append"]
        self.assertEqual("".join(e["content"][0]["text"] for e in parts), text)
        self.assertTrue(all(len(e["content"][0]["text"].encode()) <= 500 for e in parts))
        self.assertTrue(all(e["channel"] == "speakable" for e in parts))
        await c.send_text("context")
        self.assertEqual(http.socket.sent[-1]["type"], "session.context.append")
        await c.send_result(None, "shared final update")
        self.assertEqual(http.socket.sent[-1]["channel"], "speakable")
        self.assertNotIn("delegation_item_id", http.socket.sent[-1])

    async def test_oauth_webrtc_exact_post_sideband_and_pcm_track(self):
        c = self.client({"backend": "codex"}, {"access_token": "fake-oauth", "account_id": "fake-account"})
        http = HTTP()
        with patch.dict(sys.modules, RTC_MODULES), patch.object(rt, "_http_session", return_value=http):
            await c.connect()
            self.assertEqual((await c.receive())["type"], "ready")
            self.assertEqual(http.calls[0][0:2], ("post", rt.CALL_CODEX))
            options = http.calls[0][2]
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["headers"]["ChatGPT-Account-Id"], "fake-account")
            self.assertEqual(options["json"]["session"]["delegation"], {"type": "client"})
            self.assertEqual(http.calls[1][1], "wss://api.openai.com/v1/live/rtc_fake")
            self.assertEqual(http.calls[1][2]["headers"]["Authorization"], "Bearer fake-oauth")
            self.assertEqual(Peer.last.channel.label, "oai-events")
            self.assertTrue(Peer.last.channel.options["ordered"])
            self.assertEqual(http.socket.sent, [])  # no duplicate session.update
            Peer.last.channel.handlers["message"](json.dumps({"type": "delegation.created"}))
            self.assertTrue(c._events.empty())  # sideband is the single event owner
            await c.send_audio(b"\x01\0" * 480)
            frame = await Peer.last.track.recv()
            self.assertEqual((frame.sample_rate, frame.samples, frame.pts), (24000, 480, 0))
            self.assertEqual(frame.data, b"\x01\0" * 480)
            # Invalidate buffered old speech and suppress new PCM while muted.
            await c.send_audio(b"\x02\0" * 480)
            await c.set_muted(True)
            self.assertEqual(Peer.last.track.buffer, b"")
            await c.send_audio(b"\x03\0" * 480)
            # Muted RTP continues with paced silence, never captured speech.
            silent = await asyncio.wait_for(Peer.last.track.recv(), 0.2)
            self.assertEqual((silent.pts, silent.data), (480, bytes(960)))
            Peer.last.track.buffer.extend(b"\x04\0" * 480)
            await c.set_muted(False)
            self.assertEqual(Peer.last.track.buffer, b"")
            await c.send_audio(b"\x05\0" * 480)
            resumed = await asyncio.wait_for(Peer.last.track.recv(), 0.2)
            self.assertEqual(resumed.data, b"\x05\0" * 480)
            await self.handle(c, {"type": "input_transcript.added", "item": {"text": "hello"}})
            self.assertEqual((await c.receive())["type"], "interrupted")
            self.assertEqual((await c.receive())["text"], "hello")
            await c.close()
            self.assertTrue(Peer.last.closed)
            self.assertTrue(http.closed)

    async def test_remote_audio_resamples_and_trims_plane_padding(self):
        c = self.client({"backend": "codex"})
        transport = rt._RTCTransport(c)
        converted = types.SimpleNamespace(samples=3, planes=[b"\x01\0\x02\0\x03\0padding"])
        parameters = []

        class Resampler:
            def __init__(self, **kwargs):
                parameters.append(kwargs)

            def resample(self, frame):
                return [converted]

        class Remote:
            async def recv(self):
                await asyncio.sleep(0)
                return object()

        task = asyncio.create_task(transport._remote_audio(Remote(), Resampler))
        try:
            event = await asyncio.wait_for(c.receive(), 1)
            self.assertEqual(event["audio"], b"\x01\0\x02\0\x03\0")
            self.assertEqual(parameters[0], {"format": "s16", "layout": "mono", "rate": 24000})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_oauth_rejection_is_explicit_and_redacted(self):
        c = self.client({"backend": "codex"}, {"access_token": "fake-oauth", "account_id": "fake-account"})
        response = Response(b"SECRET body not read")
        response.status = 403
        http = HTTP(response=response)
        with patch.dict(sys.modules, RTC_MODULES), patch.object(rt, "_http_session", return_value=http):
            with self.assertRaises(rt.RealtimeError) as caught:
                await c.connect()
        self.assertEqual(caught.exception.code, "oauth_unavailable")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(Peer.last.closed)

    async def test_oauth_never_used_as_standalone_api_key(self):
        c = self.client({"backend": "codex", "transport": "websocket"}, {"access_token": "fake-oauth", "account_id": "fake-account"})
        with self.assertRaisesRegex(rt.RealtimeError, "requires an OpenAI API key"):
            await c.connect()

    async def test_failure_redacts_and_never_retries(self):
        c, http = await self.connect_ws()
        http.socket.failure = True
        with self.assertRaises(rt.RealtimeError) as caught:
            await c.send_text("work")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual((await c.receive())["code"], "transport")
        self.assertEqual(len(http.calls), 1)
        with self.assertRaises(rt.RealtimeError):
            await c.connect()

    async def test_bounds_malformed_payloads_and_close_wakeup(self):
        c = self.client()
        for pcm in (b"x", b"x" * (rt.MAX_AUDIO + 2), "not bytes"):
            with self.assertRaises(rt.RealtimeError):
                await c.send_audio(pcm)
        await c._handle("not JSON SECRET")
        self.assertEqual((await c.receive())["code"], "protocol")
        c2 = self.client()
        for _ in range(rt.QUEUE_SIZE + 1):
            c2._emit({"type": "audio", "pcm": b"\0\0"})
        self.assertEqual((await c2.receive())["code"], "overflow")
        c3 = self.client()
        waiter = asyncio.create_task(c3.receive())
        await asyncio.sleep(0)
        await c3.close()
        with self.assertRaises(rt.RealtimeError):
            await asyncio.wait_for(waiter, 1)

    async def test_websocket_mute_suppresses_audio_and_validates_state(self):
        c, http = await self.connect_ws()
        count = len(http.socket.sent)
        await c.set_muted(True)
        await c.send_audio(b"\x01\0" * 480)
        self.assertEqual(len(http.socket.sent), count)
        await c.set_muted(False)
        await c.send_audio(b"\x02\0" * 480)
        self.assertEqual(len(http.socket.sent), count + 1)
        with self.assertRaises(rt.RealtimeError):
            await c.set_muted("false")

    async def test_close_is_globally_bounded_with_stalled_resources(self):
        class Stalled:
            async def close(self):
                await asyncio.sleep(10)

        c = self.client()
        c._rtc, c._ws, c._http = Stalled(), Stalled(), Stalled()
        await asyncio.wait_for(c.close(), 1.9)
        self.assertTrue(c._done.is_set())
        await asyncio.sleep(0)

    async def test_failed_response_and_wrong_audio_format_fail_closed(self):
        for payload in [
            {"type": "response.done", "response": {"status": "failed", "error": "SECRET"}},
            {"type": "response.output_audio.delta", "delta": "AAA=", "sample_rate": 48000},
        ]:
            c = self.client()
            await self.handle(c, payload)
            event = await c.receive()
            self.assertEqual(event["type"], "error")
            self.assertNotIn("SECRET", event["message"])

    async def test_http_redirect_hook_prevents_credential_forwarding(self):
        session = await rt._http_session()
        try:
            trace = session.trace_configs[0]
            with self.assertRaises(rt.RealtimeError):
                await trace.on_request_redirect.send(None, None, None)
        finally:
            await session.close()


if __name__ == "__main__":
    unittest.main()
