"""Provider-free cached strip rendering and owner/surface fences."""
import re
import threading
import time
import unicodedata
import unittest

from voice_ui import BARS, VoiceStrip, clip, render
from test_service import OWNER, eventually

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cells(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in ANSI.sub("", text))


def snapshot(**changes):
    return dict(state="listening", muted=False, elapsed_seconds=65,
                microphone_level=0.5, speaker_level=0.0, caption="", **changes)


class RenderTests(unittest.TestCase):
    def test_all_widths_color_and_plain_are_cell_bounded(self):
        state = snapshot()
        state["caption"] = "A caption 界e\u0301 " * 30
        for width in (1, 5, 25, 26, 27, 31, 40, 80, 160):
            with self.subTest(width=width):
                plain = render(state, width, [0, 0.5, 1], [1, 0], color=False)
                colored = render(state, width, [0, 0.5, 1], [1, 0], color=True)
                self.assertEqual([ANSI.sub("", line) for line in colored], plain)
                self.assertTrue(all(cells(line) <= width for line in plain), plain)
                self.assertTrue(all("\x1b" not in line for line in plain))
        self.assertIn("01:05", render(state, 80, [], [], False)[0])

    def test_compact_muted_history_and_clipping(self):
        state = snapshot()
        state["muted"] = True
        lines = render(state, 80, [1] * 100, [1] * 100, False)
        self.assertIn("muted", lines[0])
        self.assertIn("MIC " + BARS[0] * 24, lines[1])
        self.assertIn("OUT " + BARS[-1] * 24, lines[1])
        self.assertIn("unmute", lines[-1])
        self.assertEqual(render(state, 20, [1], [1], False)[1], "MIC muted")
        state["muted"] = False
        lines = render(state, 80, [1] * 100 + [0] * 24, [], False)
        self.assertNotIn(BARS[-1], lines[1])
        self.assertEqual(clip("界e\u0301x", 3), "界e\u0301")
        self.assertNotIn("\x1b", clip("a\x1bb\x00c", 20))


class FakeExtension:
    def __init__(self):
        self.requests, self.frames = [], []
        self.lock = threading.Lock()

    def request(self, method, params, **kwargs):
        with self.lock:
            self.requests.append((method, params, kwargs))
        return {"columns": 80, "rows": 24} if method == "ui/open" else {}

    def publish_ui_frame(self, frame):
        with self.lock:
            self.frames.append(frame)


class FakeService:
    def __init__(self):
        self.stops = 0

    def snapshot(self):
        return snapshot()

    def stop(self):
        self.stops += 1

    def call(self, result, **kwargs):
        return result


class StripTests(unittest.TestCase):
    def setUp(self):
        self.ext, self.service = FakeExtension(), FakeService()
        self.strip = VoiceStrip(self.ext, self.service)
        self.addCleanup(self.strip.close)

    def test_mount_has_no_focus_and_frames_resize(self):
        self.strip.open(OWNER, 7)
        method, params, kwargs = self.ext.requests[0]
        self.assertEqual(method, "ui/open")
        self.assertEqual(params, {"surface_id": "voice-1", "title": "Voice",
            "placement": "above_editor", "resource_owner": OWNER})
        self.assertEqual(kwargs["parent_request_id"], 7)
        eventually(lambda: bool(self.ext.frames))
        self.strip.event({"surface_id": "voice-1", "columns": 33, "rows": 12})
        eventually(lambda: self.ext.frames[-1]["columns"] == 33)
        self.assertEqual(self.ext.frames[-1]["rows"], 12)
        self.assertEqual(self.ext.frames[-1]["resource_owner"], OWNER)
        self.assertGreater(self.ext.frames[-1]["revision"], self.ext.frames[0]["revision"])
        self.assertTrue(all(method in ("ui/open", "ui/close") for method, _, _ in self.ext.requests))

    def test_failed_frame_publication_stops_microphone(self):
        notices = []
        def failed(frame):
            raise RuntimeError("transport gone")
        self.ext.publish_ui_frame = failed
        self.ext.notify = lambda text, **kwargs: notices.append((text, kwargs))
        self.strip.open(OWNER, 7)
        eventually(lambda: self.service.stops == 1 and bool(notices))
        self.assertEqual(notices[0][1], {"level": "error"})
        time.sleep(0.15)
        self.assertEqual(self.service.stops, 1)
        self.assertEqual(len(notices), 1)

    def test_replaced_surface_ignores_stale_resize_and_close(self):
        self.strip.open(OWNER, 7)
        self.strip.open(OWNER, 8)
        self.strip.event({"surface_id": "voice-1", "columns": 2, "rows": 2})
        self.strip.event({"surface_id": "voice-1", "reason": "owner_retired"})
        eventually(lambda: bool(self.ext.frames))
        self.assertEqual(self.ext.frames[-1]["surface_id"], "voice-2")
        self.assertEqual(self.ext.frames[-1]["columns"], 80)
        self.assertEqual(self.service.stops, 0)
        self.strip.event({"surface_id": "voice-2", "reason": "owner_retired"})
        self.assertEqual(self.service.stops, 1)
        count = len(self.ext.frames)
        time.sleep(0.15)
        self.assertEqual(len(self.ext.frames), count)
        self.strip.event({"surface_id": "voice-2", "reason": "owner_retired"})
        self.assertEqual(self.service.stops, 1)


if __name__ == "__main__":
    unittest.main()
