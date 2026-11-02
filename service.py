"""Session-fenced realtime conversation, independent of the native agent loop.

Voice is a spoken surface of the active conversation (including typed turns),
not a second executor. Injection ACKs mean admission, never task completion.
Only the host's successful final-response hook is shared as an agent answer;
reasoning and raw tool output are never forwarded.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
import time

from audio import AudioBackpressure

MAX_TEXT_BYTES = 16_384
SPEECH_BYTES = 3960  # Codex's ~990-token spoken-final budget, conservatively UTF-8.
MAX_HANDOFFS = 4096
MAX_SESSION_SECONDS = 3600


def audio_backend():
    """Select native audio lazily; selection never imports sounddevice or opens devices."""
    if sys.platform in ("darwin", "win32"):
        import portable_audio
        return portable_audio.PortableAudio, portable_audio
    import audio
    return audio.PipeWireAudio, audio


def plain(text, limit=MAX_TEXT_BYTES):
    if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
        raise ValueError("Voice text exceeds its byte budget")
    # Provider transcripts and responses cannot inject terminal escapes/controls.
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))


class VoiceService:
    def __init__(self, inject, notify, client_factory=None, audio_factory=None):
        self.inject = inject
        self.notify = notify
        self.client_factory = client_factory
        self.audio_factory = audio_factory
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name="octet-voice", daemon=True)
        self._thread.start()
        self._epoch = 0
        self._owner = None
        self._host_session = None
        self._parent = None
        self._task = None
        self._client = None
        self._audio = None
        self._outbox = None
        self._handoffs = None
        self._seen = set()
        self._state = "stopped"
        self._error = None
        self._muted = False
        self._started = None
        self._backend = None
        self._closed = False
        self._cleanup_failed = False
        self._caption = ""
        self._injection_cancel = threading.Event()

    def call(self, coroutine, timeout=3):
        if self._closed:
            coroutine.close()
            raise RuntimeError("Voice service is closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("Voice control timed out") from None

    def snapshot(self):
        audio = self._audio
        mic, speaker = audio.levels() if audio is not None else (0.0, 0.0)
        return {"state": self._state, "muted": self._muted,
                "microphone_level": mic, "speaker_level": speaker, "caption": self._caption,
                "backend": self._backend, "error": self._error,
                "elapsed_seconds": int(time.monotonic() - self._started) if self._started else 0}

    def owns(self, context):
        return self._owner is not None and context.get("resource_owner") == self._owner

    async def reserve(self, owner, host_session, parent):
        if not isinstance(owner, dict) or not host_session or not isinstance(parent, int):
            raise ValueError("Voice requires a foreground session-owned command")
        if (self._cleanup_failed or self._state not in ("stopped", "failed")
                or (self._task is not None and not self._task.done())):
            raise ValueError("Voice is already active; stop it before restarting")
        self._epoch += 1
        self._injection_cancel = threading.Event()
        self._owner = dict(owner)
        self._host_session = host_session
        self._parent = parent
        self._state, self._error = "connecting", None
        self._caption = ""
        return self._epoch

    async def activate(self, ticket, settings, credential):
        if ticket != self._epoch or self._state != "connecting":
            return False
        self._backend = settings["backend"]
        self._task = asyncio.create_task(self._session(ticket, settings, credential))
        return True

    async def failed_start(self, ticket):
        if ticket == self._epoch:
            await self.stop()
            self._state, self._error = "failed", "Credential resolution failed; check native provider login"

    async def _session(self, epoch, settings, credential):
        client = audio = None
        tasks = []
        try:
            if self.client_factory is None:
                from realtime import RealtimeClient
                factory = RealtimeClient
            else:
                factory = self.client_factory
            client = factory(settings, credential)
            self._client = client
            await asyncio.wait_for(client.connect(), 35)
            if epoch != self._epoch:
                return
            audio_factory = self.audio_factory or audio_backend()[0]
            audio = audio_factory(input_target=settings["input_target"],
                                  output_target=settings["output_target"])
            self._audio = audio
            audio.set_muted(self._muted)
            await client.set_muted(self._muted)
            await asyncio.wait_for(audio.start(), 8)
            if epoch != self._epoch:
                return
            self._outbox = asyncio.Queue(maxsize=16)
            self._handoffs = asyncio.Queue(maxsize=8)
            self._seen.clear()
            self._started = time.monotonic()
            self._state = "muted" if self._muted else "listening"
            tasks = [asyncio.create_task(self._capture(audio, client)),
                     asyncio.create_task(self._receive(audio, client, epoch)),
                     asyncio.create_task(self._send_updates(client)),
                     asyncio.create_task(self._delegate(client, epoch)),
                     asyncio.create_task(asyncio.sleep(MAX_SESSION_SECONDS))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            if epoch == self._epoch:
                # Do not expose network exception bodies/headers/SDP or credentials.
                if isinstance(error, AudioBackpressure):
                    self._error = "Voice stopped (AudioBackpressure): playback fell behind. Restart voice; no audio was replayed."
                else:
                    self._error = f"Voice failed ({type(error).__name__}); check connection, provider access, and audio devices"
                self._state = "failed"
                self.notify(self._error, level="error")
        finally:
            self._injection_cancel.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Close microphone first, before waiting on any remote teardown.
            if audio:
                try:
                    await asyncio.wait_for(audio.stop(), 2)
                except Exception:
                    self._cleanup_failed = True
                    self.notify("Voice audio cleanup failed; close octet to force child cleanup", level="error")
            if client:
                try:
                    await asyncio.wait_for(client.close(), 2)
                except Exception:
                    pass
            if epoch == self._epoch:
                self._client = self._audio = self._outbox = self._handoffs = None
                self._owner = self._host_session = self._parent = None
                self._started = None
                if self._state != "failed":
                    self._state = "stopped"

    async def _capture(self, audio, client):
        while True:
            chunk = await audio.read()
            await client.send_audio(chunk)

    async def _receive(self, audio, client, epoch):
        while epoch == self._epoch:
            event = await client.receive()
            kind = event.get("type")
            if kind == "audio":
                self._state = "muted" if self._muted else "speaking"
                await audio.play(event["audio"])
            elif kind == "interrupted":
                await audio.interrupt()
                self._state = "muted" if self._muted else "listening"
            elif kind in ("input_transcript", "output_transcript"):
                if event.get("final"):
                    text = plain(event.get("text", ""))
                    label = "You" if kind == "input_transcript" else "Voice"
                    if text.strip():
                        self._caption = f"{label}: {text}"
                    self._state = "muted" if self._muted else "listening"
            elif kind == "handoff":
                identifier = event.get("handoff_id")
                text = plain(event.get("text", ""))
                if not isinstance(identifier, str) or not identifier or len(identifier) > 256 or not text.strip():
                    raise ValueError("Invalid voice handoff")
                if identifier in self._seen:
                    continue  # Never execute a duplicate delegation.
                if len(self._seen) >= MAX_HANDOFFS:
                    raise ValueError("Voice handoff budget exhausted; restart voice")
                self._seen.add(identifier)
                # Admission can take seconds; it must not block playback, VAD or
                # barge-in. Bound pending input separately from the media queue.
                self._handoffs.put_nowait((identifier, text))
            elif kind == "error":
                raise RuntimeError("Realtime provider reported an error")
            elif kind == "closed":
                return

    async def _delegate(self, client, epoch):
        while epoch == self._epoch:
            identifier, text = await self._handoffs.get()
            # A lost ACK is ambiguous: never replay potentially admitted input.
            await asyncio.to_thread(self.inject, text, dict(self._owner), self._parent,
                                    cancellation_event=self._injection_cancel)
            if epoch != self._epoch:
                return
            await client.send_result(identifier,
                "Request admitted to the coding-agent input. Execution is not complete. "
                "Wait for a shared agent update; do not claim the task succeeded.", final=False)

    async def _send_updates(self, client):
        while True:
            action, text = await self._outbox.get()
            if action == "prompt":
                if self._audio:
                    await self._audio.interrupt()
                await client.send_text("[USER already submitted this to the coding agent; context only, "
                                       "do not delegate again] " + text)
            else:
                await client.send_result(None, text, final=True)

    async def update(self, action, text, owner):
        if owner != self._owner or self._outbox is None:
            return
        try:
            text = plain(text)
        except ValueError:
            self.notify("Voice update exceeds its budget; the full text remains in octet.", level="warning")
            return
        if action == "answer" and len(text.encode("utf-8")) > SPEECH_BYTES:
            text = "The agent finished. Its answer is too long to read aloud; see the full answer in octet."
        try:
            self._outbox.put_nowait((action, text))
        except asyncio.QueueFull:
            # Stale speech is worse than ending the explicitly started session.
            await self.stop()
            self.notify("Voice stopped: its bounded update queue filled.", level="warning")

    async def mute(self, toggle=True):
        if self._state in ("stopped", "failed"):
            return "Voice is stopped"
        self._muted = not self._muted if toggle else True
        if self._audio:
            self._audio.set_muted(self._muted)
        if self._client:
            await self._client.set_muted(self._muted)
        self._state = ("muted" if self._muted else "listening") if self._audio else "connecting"
        return "Microphone muted" if self._muted else "Microphone live"

    async def session_settled(self, session_id):
        if session_id == self._host_session:
            await self.stop()

    async def stop(self):
        # Cancelling to_thread does not stop its running reverse RPC. Revoke it
        # explicitly as well; cancellation cannot roll back already admitted input.
        self._injection_cancel.set()
        self._epoch += 1  # Revoke pending credential callbacks and future handoffs first.
        task = self._task
        already_stopping = self._state == "stopping"
        self._owner = self._host_session = self._parent = None
        self._outbox = self._handoffs = None
        self._state = "stopping"
        self._started = None
        self._muted = False
        if task:
            if not already_stopping:
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Host process-tree supervision is the final cleanup fence.
                self._error = "Voice cleanup is still pending; close octet to force cleanup"
        if self._cleanup_failed or (task and not task.done()):
            return "Voice stop requested; cleanup is still pending. Close octet to force process-tree cleanup."
        self._task = None
        self._client = self._audio = None
        self._state = "stopped"
        return "Voice stopped. Microphone closed; already admitted coding work is not cancelled."

    def close(self):
        if self._closed:
            return
        try:
            self.call(self.stop(), timeout=6)
        finally:
            self._closed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1)
            if not self._thread.is_alive():
                self._loop.close()
