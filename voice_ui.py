"""Quiet composer-adjacent voice strip, using the host's cached remote UI lane.

No terminal ownership, render RPCs, audio data, transcript writes or focus capture.
A separate bounded-rate publisher keeps UI backpressure off the media event loop.
"""
from collections import deque
import os
import threading
import unicodedata


BARS = "▁▂▃▄▅▆▇█"


def clip(text, width):
    result, used = [], 0
    for char in " ".join(text.split()):
        if ord(char) < 32 or 127 <= ord(char) < 160:
            continue
        cells = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + cells > width:
            break
        result.append(char)
        used += cells
    return "".join(result)


def render(snapshot, columns, microphone, speaker, color=True):
    columns = max(1, columns)
    def styled(text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if color else text
    muted = snapshot["muted"]
    state = snapshot["state"]
    if state in ("listening", "speaking", "muted"):
        state = "muted" if muted else ("speaking" if snapshot["speaker_level"] > 0.01 else "listening")
    elapsed = snapshot["elapsed_seconds"]
    timer = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
    title = f"● VOICE {state}"
    heading = clip(title, columns)
    if columns >= len(title) + len(timer) + 2:
        heading += " " * (columns - len(title) - len(timer)) + timer
    lines = [styled(heading, "2" if muted else "36")]
    count = min(24, max(1, (columns - 18) // 2))
    def wave(history, silent=False):
        values = ([0.0] * count if silent else ([0.0] * count + list(history))[-count:])
        return "".join(BARS[min(7, int(max(0, min(1, value)) * 7))] for value in values)
    if columns >= 26:
        lines.append(styled("  MIC " + wave(microphone, muted), "2" if muted else "36")
                     + "   " + styled("OUT " + wave(speaker), "35"))
    else:
        lines.append(styled(clip("MIC muted" if muted else "MIC live", columns), "2" if muted else "36"))
    caption = snapshot.get("caption", "")
    if caption:
        lines.append(styled(clip(caption, columns), "2"))
    hints = "^F9 unmute" if muted else "^F9 mute"
    lines.append(styled(clip(f"{hints} · ^F8 toggle · ^F10 stop · headphones recommended", columns), "2"))
    return lines


class VoiceStrip:
    def __init__(self, extension, service):
        self.ext, self.service = extension, service
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._mount = None
        self._serial = 0
        self._mic, self._speaker = deque(maxlen=24), deque(maxlen=24)
        self._color = "NO_COLOR" not in os.environ

    def open(self, owner, parent):
        with self._lock:
            self._close_locked()
            self._serial += 1
            surface = f"voice-{self._serial}"
            geometry = self.ext.request("ui/open", {"surface_id": surface, "title": "Voice",
                "placement": "above_editor", "resource_owner": owner}, parent_request_id=parent, timeout=3)
            self._mount = dict(surface_id=surface, resource_owner=dict(owner), parent_request_id=parent,
                               columns=geometry["columns"], rows=geometry["rows"], revision=0)
            self._mic.clear()
            self._speaker.clear()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="octet-voice-ui", daemon=True)
                self._thread.start()

    def event(self, event):
        with self._lock:
            if self._mount is None or event.get("surface_id") != self._mount["surface_id"]:
                return
            if "columns" in event:
                self._mount.update(columns=event["columns"], rows=event["rows"])
            else:
                self._mount = None
                # Owner replacement/rescue must also release the microphone.
                self.service.call(self.service.stop(), timeout=6)

    def _close_locked(self):
        mount, self._mount = self._mount, None
        if mount is not None:
            try:
                self.ext.request("ui/close", {key: mount[key] for key in (
                    "surface_id", "resource_owner", "parent_request_id")}, timeout=2)
            except Exception:
                # Owner retirement already removes the surface; never retry it.
                pass

    def _run(self):
        while not self._stop.wait(0.1):
            try:
                with self._lock:
                    if self._mount is None:
                        continue
                    snapshot = self.service.snapshot()
                    if snapshot["state"] in ("stopped", "failed"):
                        self._close_locked()
                        continue
                    self._mic.append(snapshot["microphone_level"])
                    self._speaker.append(snapshot["speaker_level"])
                    mount = self._mount
                    mount["revision"] += 1
                    frame = {key: mount[key] for key in (
                        "surface_id", "resource_owner", "columns", "rows", "revision")}
                    frame["lines"] = render(snapshot, mount["columns"], self._mic, self._speaker, self._color)
                    self.ext.publish_ui_frame(frame)
            except Exception:
                # Never leave a live microphone behind a failed display. UI
                # backpressure cannot block media, and no effect is retried.
                with self._lock:
                    self._mount = None
                try:
                    self.service.call(self.service.stop(), timeout=6)
                    self.ext.notify("Voice display failed; voice stopped. Check /voice status.", level="error")
                except Exception:
                    pass  # Transport shutdown still releases audio through supervision.

    def close(self):
        self._stop.set()
        with self._lock:
            self._close_locked()
        if self._thread is not None:
            self._thread.join(timeout=1)
