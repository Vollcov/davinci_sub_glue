# -*- coding: utf-8 -*-
import importlib.util
import os
import tempfile
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "SubtitleGlue.py")


def load_script():
    spec = importlib.util.spec_from_file_location("subtitle_glue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sg = load_script()


class FillGapsTests(unittest.TestCase):
    def test_extends_previous_cue_to_next_start(self):
        cues = [sg.Cue(0, 10, "a"), sg.Cue(20, 30, "b")]
        filled, grown, extra = sg.fill_gaps(cues, gap_frames=0)
        self.assertEqual(grown, 1)
        self.assertEqual(extra, 10)
        self.assertEqual(filled[0], sg.Cue(0, 20, "a"))
        self.assertEqual(filled[1], sg.Cue(20, 30, "b"))

    def test_keeps_requested_frame_gap(self):
        cues = [sg.Cue(0, 10, "a"), sg.Cue(20, 30, "b")]
        filled, grown, extra = sg.fill_gaps(cues, gap_frames=1)
        self.assertEqual(filled[0].end, 19)
        self.assertEqual(grown, 1)
        self.assertEqual(extra, 9)

    def test_caps_extension_when_max_set(self):
        cues = [sg.Cue(0, 10, "a"), sg.Cue(40, 50, "b")]
        filled, grown, extra = sg.fill_gaps(cues, gap_frames=0, max_extend_frames=5)
        self.assertEqual(filled[0].end, 15)
        self.assertEqual(grown, 1)
        self.assertEqual(extra, 5)

    def test_already_adjacent_cues_stay_unchanged(self):
        cues = [sg.Cue(0, 10, "a"), sg.Cue(10, 20, "b")]
        filled, grown, extra = sg.fill_gaps(cues)
        self.assertEqual(grown, 0)
        self.assertEqual(extra, 0)
        self.assertEqual(filled, cues)

    def test_does_not_shrink_overlapping_cues(self):
        cues = [sg.Cue(0, 25, "a"), sg.Cue(20, 30, "b")]
        filled, grown, extra = sg.fill_gaps(cues, gap_frames=1)
        self.assertEqual(filled[0].end, 25)
        self.assertEqual(grown, 0)
        self.assertEqual(extra, 0)

    def test_single_and_empty_lists(self):
        self.assertEqual(sg.fill_gaps([]), ([], 0, 0))
        single = [sg.Cue(5, 12, "only")]
        filled, grown, extra = sg.fill_gaps(single)
        self.assertEqual(filled, single)
        self.assertEqual(grown, 0)
        self.assertEqual(extra, 0)

    def test_fills_every_gap_in_a_chain(self):
        cues = [
            sg.Cue(0, 8, "a"),
            sg.Cue(12, 16, "b"),
            sg.Cue(30, 34, "c"),
        ]
        filled, grown, extra = sg.fill_gaps(cues)
        self.assertEqual(grown, 2)
        self.assertEqual(extra, 4 + 14)
        self.assertEqual(filled[0].end, 12)
        self.assertEqual(filled[1].end, 30)
        self.assertEqual(filled[2].end, 34)

    def test_sorts_by_start_time(self):
        cues = [sg.Cue(40, 50, "b"), sg.Cue(0, 10, "a")]
        filled, grown, _extra = sg.fill_gaps(cues)
        self.assertEqual(filled[0].text, "a")
        self.assertEqual(filled[0].end, 40)
        self.assertEqual(grown, 1)


class SrtTests(unittest.TestCase):
    def test_timestamp_conversion(self):
        self.assertEqual(sg.frames_to_srt_timestamp(0, 25), "00:00:00,000")
        self.assertEqual(sg.frames_to_srt_timestamp(25, 25), "00:00:01,000")
        self.assertEqual(sg.frames_to_srt_timestamp(25 * 60, 25), "00:01:00,000")
        self.assertEqual(sg.frames_to_srt_timestamp(1, 25), "00:00:00,040")

    def test_cues_to_srt_roundtrip_shape(self):
        cues = [sg.Cue(0, 25, "Привет"), sg.Cue(50, 75, "мир\nвторая строка")]
        text = sg.cues_to_srt(cues, 25)
        self.assertIn("1\n00:00:00,000 --> 00:00:01,000\nПривет", text)
        self.assertIn("2\n00:00:02,000 --> 00:00:03,000\nмир\nвторая строка", text)

    def test_skips_zero_length_cues(self):
        cues = [sg.Cue(10, 10, "gone"), sg.Cue(12, 20, "kept")]
        text = sg.cues_to_srt(cues, 25)
        self.assertNotIn("gone", text)
        self.assertIn("kept", text)
        self.assertTrue(text.startswith("1\n"))

    def test_write_srt_file_uses_utf8_bom(self):
        path = tempfile.mkstemp(suffix=".srt")[1]
        try:
            sg.write_srt_file(path, [sg.Cue(0, 10, "ёлка")], 25)
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn("ёлка".encode("utf-8"), raw)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
