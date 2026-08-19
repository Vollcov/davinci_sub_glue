# -*- coding: utf-8 -*-
import importlib.util
import os
import tempfile
import unittest
import zipfile


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


class PlacementAlignTests(unittest.TestCase):
    def test_record_frame_tries_zero_before_timeline_start(self):
        self.assertEqual(
            sg.record_frame_candidates(86400, 86400, 86410),
            [0, 86400, 86410],
        )

    def test_accepts_timeline_origin_and_rejects_append_at_end(self):
        expected = sg.record_frame_candidates(86400, 86400, 86410)
        self.assertTrue(sg.is_placement_aligned(0, expected))
        self.assertTrue(sg.is_placement_aligned(86400, expected))
        self.assertTrue(sg.is_placement_aligned(86410, expected))
        self.assertFalse(sg.is_placement_aligned(90000, expected))
        self.assertFalse(sg.is_placement_aligned(None, expected))

    def test_alignment_tolerance(self):
        self.assertTrue(sg.is_placement_aligned(86402, [86400], tolerance=2))
        self.assertFalse(sg.is_placement_aligned(86403, [86400], tolerance=2))

    def test_record_frame_adds_timeline_offset(self):
        self.assertEqual(sg.to_record_frame(10, 86400), 86410)
        self.assertEqual(sg.to_record_frame(10, 0), 10)

    def test_count_gaps(self):
        cues = [sg.Cue(0, 10, "a"), sg.Cue(20, 30, "b"), sg.Cue(30, 40, "c")]
        self.assertEqual(sg.count_gaps(cues), [10])
        filled, _, _ = sg.fill_gaps(cues)
        self.assertEqual(sg.count_gaps(filled), [])

    def test_first_start_rejects_append_after_old_captions(self):
        filled = [sg.Cue(86400, 86500, "a"), sg.Cue(86500, 86600, "b")]
        self.assertTrue(sg.first_start_is_aligned(86400, filled, 86400))
        self.assertFalse(sg.first_start_is_aligned(90000, filled, 86400))


MINIMAL_DRT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Sequence>
  <ListMgt__CC__LmVersionTable></ListMgt__CC__LmVersionTable>
  <SubtitleTrackVec>
    <Element>
      <Sm2TiTrack DbId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa">
        <UserDefinedName>Subtitle 1</UserDefinedName>
        <Items>
          <Element>
            <Sm2TiGenerator DbId="11111111-1111-1111-1111-111111111111">
              <PrettyType>Subtitle</PrettyType>
              <Name>hello&lt;br&gt;there</Name>
              <Start>86400</Start>
              <Duration>10</Duration>
            </Sm2TiGenerator>
          </Element>
          <Element>
            <Sm2TiGenerator DbId="22222222-2222-2222-2222-222222222222">
              <PrettyType>Subtitle</PrettyType>
              <Name>world</Name>
              <Start>86420</Start>
              <Duration>10</Duration>
            </Sm2TiGenerator>
          </Element>
        </Items>
      </Sm2TiTrack>
    </Element>
  </SubtitleTrackVec>
</Sequence>
"""


def write_minimal_drt(path):
    xml = MINIMAL_DRT_XML.replace("ListMgt__CC__LmVersionTable", "ListMgt::LmVersionTable")
    seq_name = "SeqContainer/timeline.xml"
    tmp_xml = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    try:
        tmp_xml.write(xml.encode("utf-8"))
        tmp_xml.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(tmp_xml.name, seq_name)
    finally:
        os.remove(tmp_xml.name)


class DrtRewriteTests(unittest.TestCase):
    def test_clone_extends_duration_and_keeps_start(self):
        path = tempfile.mkstemp(suffix=".drt")[1]
        out = tempfile.mkstemp(suffix=".drt")[1]
        try:
            write_minimal_drt(path)
            drt = sg.DrtTimeline(path)
            try:
                cues = drt.track_cues(1)
                self.assertEqual(cues[0], sg.Cue(86400, 86410, "hello\nthere"))
                self.assertEqual(cues[1], sg.Cue(86420, 86430, "world"))
                filled, grown, extra = sg.fill_gaps(cues)
                self.assertEqual(grown, 1)
                self.assertEqual(extra, 10)
                self.assertEqual(filled[0].end, 86420)
                placed = drt.add_filled_track(1, filled, "Subtitle 1 (без пауз)")
                self.assertEqual(placed, 2)
                drt.save(out)
            finally:
                drt.close()

            rewritten = sg.DrtTimeline(out)
            try:
                self.assertEqual(len(rewritten.track_elements()), 2)
                self.assertEqual(rewritten.track_name(2), "Subtitle 1 (без пауз)")
                cloned = rewritten.track_cues(2)
                self.assertEqual(cloned[0].start, 86400)
                self.assertEqual(cloned[0].end, 86420)
                self.assertEqual(cloned[1].start, 86420)
                self.assertEqual(cloned[1].end, 86430)
                original = rewritten.track_cues(1)
                self.assertEqual(original[0].end, 86410)
                ids = [
                    gen.attrib.get("DbId")
                    for gen in rewritten.track_elements()[1].iter("Sm2TiGenerator")
                ]
                self.assertNotIn("11111111-1111-1111-1111-111111111111", ids)
            finally:
                rewritten.close()
        finally:
            os.remove(path)
            os.remove(out)

    def test_roundtrip_preserves_colon_colon_tags(self):
        path = tempfile.mkstemp(suffix=".drt")[1]
        out = tempfile.mkstemp(suffix=".drt")[1]
        try:
            write_minimal_drt(path)
            drt = sg.DrtTimeline(path)
            try:
                filled, _, _ = sg.fill_gaps(drt.track_cues(1))
                drt.add_filled_track(1, filled, "filled")
                drt.save(out)
            finally:
                drt.close()
            with zipfile.ZipFile(out) as archive:
                xml = archive.read("SeqContainer/timeline.xml").decode("utf-8")
            self.assertIn("ListMgt::LmVersionTable", xml)
            self.assertNotIn("__CC__", xml)
        finally:
            os.remove(path)
            os.remove(out)

    def test_safe_filename_strips_illegal_chars(self):
        self.assertEqual(sg.safe_filename('a/b:c*d'), "a_b_c_d")


if __name__ == "__main__":
    unittest.main()
