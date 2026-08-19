# -*- coding: utf-8 -*-
"""
Subtitle Glue for DaVinci Resolve.

Reads generated subtitle clips on the active timeline and removes empty
gaps by extending each caption until the next one starts.

Resolve cannot trim native subtitle clips, and AppendToTimeline ignores
recordFrame for SRT — the file always lands after the last existing caption.
The working fix (same as the spelling script) is Resolve's own .drt format:
export the timeline, clone the subtitle track with new durations, re-import.
That creates a new timeline with captions on the original timecode.

Run from: Workspace > Scripts > Utility > SubtitleGlue
"""

from __future__ import print_function

import copy
import os
import re
import shutil
import sys
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET

VERSION = "1.7.0"
SCRIPT_ID = "SubtitleGlueWin"
MEDIA_FOLDER_NAME = "Subtitle Glue"
DRT_BREAK = "<br>"
_DRT_ESC = re.compile(r"(</?)([A-Za-z_][\w.\-]*)::")
_DRT_UNESC = re.compile(r"(</?)([A-Za-z_][\w.\-]*)__CC__")
_BAD_ZIP = getattr(zipfile, "BadZipFile", None) or getattr(zipfile, "BadZipfile")


# ---------------------------------------------------------------------------
# Timing helpers (pure, testable without Resolve)
# ---------------------------------------------------------------------------

class Cue(object):
    def __init__(self, start, end, text):
        self.start = int(start)
        self.end = int(end)
        self.text = text if text is not None else ""

    def __eq__(self, other):
        return (
            isinstance(other, Cue)
            and self.start == other.start
            and self.end == other.end
            and self.text == other.text
        )

    def __repr__(self):
        return "Cue(start=%r, end=%r, text=%r)" % (self.start, self.end, self.text)


def fill_gaps(cues, gap_frames=0, max_extend_frames=0):
    """Extend each cue's end toward the next cue to close empty gaps.

    Frames use Resolve's exclusive-end convention: a clip occupying
    frames [start, end) can be followed immediately by a clip at `end`.

    gap_frames: frames to leave between cues (0 = back-to-back).
    max_extend_frames: 0 means unlimited; otherwise cap how far a cue
        may grow into a pause.

    Returns (new_cues, filled_count, extra_frames).
    """
    if not cues:
        return [], 0, 0

    gap_frames = max(0, int(gap_frames))
    max_extend_frames = max(0, int(max_extend_frames or 0))
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    result = []
    filled = 0
    extra_frames = 0

    for index, cue in enumerate(ordered):
        new_end = cue.end
        if index + 1 < len(ordered):
            limit = ordered[index + 1].start - gap_frames
            if limit > new_end:
                extension = limit - new_end
                if max_extend_frames > 0:
                    extension = min(extension, max_extend_frames)
                if extension > 0:
                    new_end = new_end + extension
                    filled += 1
                    extra_frames += extension
            new_end = max(cue.end, min(new_end, ordered[index + 1].start - gap_frames))
            if new_end < cue.end:
                new_end = cue.end
        result.append(Cue(cue.start, new_end, cue.text))

    return result, filled, extra_frames


def frames_to_srt_timestamp(frame, fps):
    """Convert a timeline-relative frame count to SRT time (HH:MM:SS,mmm)."""
    fps = float(fps)
    if fps <= 0:
        raise ValueError("fps must be positive")
    total_ms = int(round((float(frame) / fps) * 1000.0))
    if total_ms < 0:
        total_ms = 0
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, milliseconds = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, seconds, milliseconds)


def cues_to_srt(cues, fps):
    """Serialize cues to SRT text. Cue times are timeline-relative frames."""
    blocks = []
    number = 1
    for cue in cues:
        if cue.end <= cue.start:
            continue
        text = cue.text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text:
            text = " "
        blocks.append(
            "%d\n%s --> %s\n%s" % (
                number,
                frames_to_srt_timestamp(cue.start, fps),
                frames_to_srt_timestamp(cue.end, fps),
                text,
            )
        )
        number += 1
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def write_srt_file(path, cues, fps):
    payload = cues_to_srt(cues, fps)
    with open(path, "wb") as handle:
        handle.write(b"\xef\xbb\xbf")
        handle.write(payload.encode("utf-8"))
    return path


def to_record_frame(relative_start, offset):
    """Convert a timeline-relative cue start to AppendToTimeline recordFrame."""
    return int(relative_start) + int(offset or 0)


def count_gaps(cues):
    """Return positive empty gaps between consecutive cues in frames."""
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    gaps = []
    for current, nxt in zip(ordered, ordered[1:]):
        gap = nxt.start - current.end
        if gap > 0:
            gaps.append(int(gap))
    return gaps


def unique_frames(*values):
    frames = []
    for value in values:
        if value is None:
            continue
        frame = int(value)
        if frame not in frames:
            frames.append(frame)
    return frames


def record_frame_candidates(timeline_start, offset, first_cue_abs=None):
    return unique_frames(0, offset, timeline_start, first_cue_abs)


def is_placement_aligned(actual_start, expected_starts, tolerance=2):
    if actual_start is None:
        return False
    actual_start = int(actual_start)
    for expected in expected_starts:
        if expected is None:
            continue
        if abs(actual_start - int(expected)) <= int(tolerance):
            return True
    return False


def first_start_is_aligned(actual_start, filled_cues, origin, tolerance=2):
    """True if the first placed caption starts with the original first cue."""
    expected = []
    if filled_cues:
        expected.append(filled_cues[0].start)
    expected.extend([origin, 0])
    return is_placement_aligned(actual_start, expected, tolerance)


# ---------------------------------------------------------------------------
# DRT timeline rewrite (frame-accurate subtitle placement)
# ---------------------------------------------------------------------------

class DrtError(Exception):
    pass


def _xml_to_unicode(root):
    xml = ET.tostring(root, encoding="unicode")
    if not isinstance(xml, type(u"")):
        xml = xml.decode("utf-8")
    return xml


def _set_child_text(parent, tag, value):
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def generator_span(gen):
    start = int(gen.findtext("Start") or 0)
    duration_text = gen.findtext("Duration")
    end_text = gen.findtext("End")
    if duration_text not in (None, ""):
        duration = int(duration_text)
        end = start + duration
    elif end_text not in (None, ""):
        end = int(end_text)
        duration = max(1, end - start)
    else:
        duration = 1
        end = start + 1
    return start, end, duration


def set_span_on_element(element, start, end):
    duration = max(1, int(end) - int(start))
    for node in element.iter():
        if node.tag == "Start":
            node.text = str(int(start))
        elif node.tag == "Duration":
            node.text = str(duration)
        elif node.tag == "End":
            node.text = str(int(start) + duration)
    if element.find("Duration") is None and element.find(".//Duration") is None:
        _set_child_text(element, "Duration", duration)
    return duration


def retag_drt_ids(element):
    for node in element.iter():
        if "DbId" in node.attrib:
            node.set("DbId", str(uuid.uuid4()))
        if node.tag == "DbId" or node.tag.endswith("DbId"):
            if node.text:
                node.text = str(uuid.uuid4())


def generator_text(gen):
    return (gen.findtext("Name") or "").replace(DRT_BREAK, "\n")


def cues_from_generators(generators):
    cues = []
    for gen in generators:
        start, end, _duration = generator_span(gen)
        cues.append(Cue(start, end, generator_text(gen)))
    return cues


class DrtTimeline(object):
    """Open a .drt, clone a subtitle track with new cue durations, save it back.

    A .drt is a zip. SeqContainer/<uuid>.xml holds SubtitleTrackVec. AppendToTimeline
    cannot place SRT on the original timecode; rewriting this XML can.
    """

    def __init__(self, path):
        self.path = path
        self._tmp = tempfile.mkdtemp(prefix="subtitle_glue_drt_")
        try:
            archive = zipfile.ZipFile(path)
            try:
                self._names = archive.namelist()
                archive.extractall(self._tmp)
            finally:
                archive.close()
        except _BAD_ZIP:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise DrtError("%s is not a readable .drt archive" % path)

        seqs = [name for name in self._names if name.startswith("SeqContainer/") and name.endswith(".xml")]
        if not seqs:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise DrtError("%s contains no SeqContainer XML" % path)
        self._seq_path = os.path.join(self._tmp, seqs[0])
        self._root = self._load(self._seq_path)
        self._vec = self._root.find(".//SubtitleTrackVec")
        if self._vec is None:
            self.close()
            raise DrtError("timeline has no SubtitleTrackVec — it has never had a subtitle track")

    def close(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @staticmethod
    def _load(path):
        handle = open(path, "rb")
        try:
            raw = handle.read()
        finally:
            handle.close()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        text = raw.decode("utf-8")
        return ET.fromstring(_DRT_ESC.sub(r"\1\2__CC__", text))

    @staticmethod
    def _dump(root, path):
        xml = _DRT_UNESC.sub(r"\1\2::", _xml_to_unicode(root))
        handle = open(path, "wb")
        try:
            handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            handle.write(xml.encode("utf-8"))
        finally:
            handle.close()

    def track_elements(self):
        return list(self._vec.findall("Element"))

    def track_cues(self, track_index):
        elements = self.track_elements()
        if track_index < 1 or track_index > len(elements):
            raise DrtError("no subtitle track %d (found %d)" % (track_index, len(elements)))
        return cues_from_generators(list(elements[track_index - 1].iter("Sm2TiGenerator")))

    def track_name(self, track_index):
        elements = self.track_elements()
        if track_index < 1 or track_index > len(elements):
            return "Subtitle %d" % track_index
        name_el = elements[track_index - 1].find(".//UserDefinedName")
        if name_el is None or not (name_el.text or "").strip():
            return "Subtitle %d" % track_index
        return name_el.text.strip()

    def add_filled_track(self, source_index, filled_cues, name):
        """Clone subtitle track `source_index` and set each cue's duration from filled_cues."""
        elements = self.track_elements()
        if source_index < 1 or source_index > len(elements):
            raise DrtError("no subtitle track %d (found %d)" % (source_index, len(elements)))
        source = elements[source_index - 1]
        generators = list(source.iter("Sm2TiGenerator"))
        if not generators:
            raise DrtError("subtitle track %d is empty" % source_index)
        filled_ordered = sorted(filled_cues, key=lambda cue: (cue.start, cue.end))
        if len(filled_ordered) != len(generators):
            raise DrtError(
                "track %d has %d cues but fill produced %d"
                % (source_index, len(generators), len(filled_ordered))
            )

        clone = copy.deepcopy(source)
        retag_drt_ids(clone)
        item_wrappers = []
        items_el = clone.find(".//Items")
        if items_el is not None:
            item_wrappers = list(items_el.findall("Element"))
        if len(item_wrappers) == len(filled_ordered):
            paired = []
            for wrapper in item_wrappers:
                gen = next(iter(wrapper.iter("Sm2TiGenerator")), None)
                start = int(gen.findtext("Start") or 0) if gen is not None else 0
                paired.append((start, wrapper, gen))
            paired.sort(key=lambda row: row[0])
            for (_start, wrapper, gen), cue in zip(paired, filled_ordered):
                target = wrapper if wrapper is not None else gen
                set_span_on_element(target, cue.start, cue.end)
        else:
            clone_gens = sorted(
                list(clone.iter("Sm2TiGenerator")),
                key=lambda gen: int(gen.findtext("Start") or 0),
            )
            for gen, cue in zip(clone_gens, filled_ordered):
                set_span_on_element(gen, cue.start, cue.end)

        udn = clone.find(".//UserDefinedName")
        if udn is None:
            track_el = clone.find("Sm2TiTrack")
            udn = ET.SubElement(track_el if track_el is not None else clone, "UserDefinedName")
        udn.text = name
        self._vec.append(clone)
        return len(filled_ordered)

    def save(self, out_path):
        self._dump(self._root, self._seq_path)
        if os.path.exists(out_path):
            os.remove(out_path)
        archive = zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED)
        try:
            for name in self._names:
                disk_path = os.path.join(self._tmp, name)
                if os.path.isfile(disk_path):
                    archive.write(disk_path, name)
        finally:
            archive.close()
        return out_path


def safe_filename(name):
    cleaned = []
    for char in name or "":
        cleaned.append("_" if char in '\\/:*?"<>|' else char)
    text = "".join(cleaned).strip() or "timeline"
    return text[:120]


def unique_timeline_name(project, base):
    names = set()
    count = int(project.GetTimelineCount() or 0)
    for index in range(1, count + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline:
            names.add(timeline.GetName())
    if base not in names:
        return base
    suffix = 2
    while True:
        candidate = "%s %d" % (base, suffix)
        if candidate not in names:
            return candidate
        suffix += 1


# ---------------------------------------------------------------------------
# Resolve connection
# ---------------------------------------------------------------------------

def _append_resolve_module_path():
    if sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        candidate = os.path.join(
            program_data,
            "Blackmagic Design",
            "DaVinci Resolve",
            "Support",
            "Developer",
            "Scripting",
            "Modules",
        )
    elif sys.platform == "darwin":
        candidate = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules/"
    else:
        candidate = "/opt/resolve/Developer/Scripting/Modules/"
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.append(candidate)


def get_resolve():
    existing = globals().get("resolve")
    if existing is not None:
        return existing
    try:
        import DaVinciResolveScript as dvr_script
    except ImportError:
        _append_resolve_module_path()
        import DaVinciResolveScript as dvr_script
    return dvr_script.scriptapp("Resolve")


def get_fusion(resolve_app):
    existing = globals().get("fusion") or globals().get("fu")
    if existing is not None:
        return existing
    if resolve_app is None:
        return None
    try:
        return resolve_app.Fusion()
    except Exception:
        return None


def get_fps(timeline):
    raw = timeline.GetSetting("timelineFrameRate")
    try:
        fps = float(raw)
    except (TypeError, ValueError):
        fps = 24.0
    if fps <= 0:
        fps = 24.0
    return fps


def get_timeline_start(timeline):
    start = timeline.GetStartFrame()
    try:
        return int(start)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Timeline I/O
# ---------------------------------------------------------------------------

def grab_number(getter):
    try:
        value = getter(True)
    except TypeError:
        value = getter()
    except Exception:
        try:
            value = getter()
        except Exception:
            return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def item_span(item):
    """Return [start, end) in timeline frames. Prefer GetDuration() as length."""
    start = grab_number(item.GetStart)
    duration = grab_number(item.GetDuration)
    end = grab_number(item.GetEnd)
    if duration > 0:
        end = start + duration
    elif end > start:
        pass
    else:
        end = start + 1.0
    return int(round(start)), int(round(end))


def read_subtitle_track(timeline, track_index):
    items = timeline.GetItemListInTrack("subtitle", track_index) or []
    cues = []
    for item in items:
        start, end = item_span(item)
        cues.append(Cue(start, end, item.GetName() or ""))
    cues.sort(key=lambda cue: (cue.start, cue.end))
    timeline_start = get_timeline_start(timeline)
    origin = timeline_start
    if cues and cues[0].start < timeline_start:
        origin = 0
    return cues, items, origin


def cues_to_relative(cues, origin):
    return [Cue(cue.start - origin, cue.end - origin, cue.text) for cue in cues]


def list_subtitle_tracks(timeline):
    tracks = []
    count = int(timeline.GetTrackCount("subtitle") or 0)
    for index in range(1, count + 1):
        items = timeline.GetItemListInTrack("subtitle", index) or []
        name = timeline.GetTrackName("subtitle", index) or ("Subtitle %d" % index)
        tracks.append({"index": index, "name": name, "count": len(items)})
    return tracks


def hide_subtitle_track(timeline, track_index):
    """Turn off a subtitle track. Do not delete — that can wipe captions."""
    try:
        timeline.SetTrackLock("subtitle", track_index, False)
    except Exception:
        pass
    try:
        if timeline.SetTrackEnable("subtitle", track_index, False):
            return "disabled"
    except Exception:
        pass
    return "kept"


def export_timeline_drt(resolve_app, timeline, path):
    export_type = getattr(resolve_app, "EXPORT_DRT", None)
    if export_type is None:
        raise RuntimeError(
            "В этой версии Resolve нет EXPORT_DRT.\n"
            "Без экспорта .drt новые субтитры нельзя поставить на исходный таймкод."
        )
    ok = False
    try:
        ok = timeline.Export(path, export_type)
    except TypeError:
        ok = False
    if not ok:
        try:
            ok = timeline.Export(path, export_type, "")
        except Exception:
            ok = False
    if not ok:
        raise RuntimeError("Не удалось экспортировать таймлайн в .drt")
    return path


def import_timeline_drt(project, path):
    media_pool = project.GetMediaPool()
    imported = media_pool.ImportTimelineFromFile(path)
    if not imported:
        imported = media_pool.ImportTimelineFromFile(path, {"timelineName": os.path.splitext(os.path.basename(path))[0]})
    if not imported:
        raise RuntimeError("ImportTimelineFromFile не создал таймлайн из .drt")
    return imported


def glue_tracks(resolve_app, project, timeline, track_indices, gap_frames, max_extend_seconds, hide_original):
    """Fill gaps by cloning subtitle tracks inside a .drt export, then re-import."""
    fps = get_fps(timeline)
    origin = get_timeline_start(timeline)
    max_extend_frames = int(round(float(max_extend_seconds) * fps)) if max_extend_seconds else 0

    try:
        resolve_app.OpenPage("edit")
    except Exception:
        pass

    wanted_name = unique_timeline_name(project, "%s (без пауз)" % (timeline.GetName() or "Timeline"))
    tmp = tempfile.mkdtemp(prefix="subtitle_glue_")
    drt_in = os.path.join(tmp, "source.drt")
    drt_out = os.path.join(tmp, safe_filename(wanted_name) + ".drt")
    summaries = []
    source_indices = []

    try:
        export_timeline_drt(resolve_app, timeline, drt_in)
        drt = DrtTimeline(drt_in)
        try:
            for track_index in sorted(track_indices):
                try:
                    cues = drt.track_cues(track_index)
                except DrtError as exc:
                    summaries.append("S%d: %s" % (track_index, exc))
                    continue
                gaps = count_gaps(cues)
                if len(cues) < 2:
                    summaries.append(
                        "S%d: недостаточно субтитров (%d)." % (track_index, len(cues))
                    )
                    continue
                filled, grown, extra = fill_gaps(cues, gap_frames, max_extend_frames)
                if grown == 0:
                    sample = ", ".join("%d-%d" % (cue.start, cue.end) for cue in cues[:4])
                    summaries.append(
                        "S%d: пауз нет (%d клипов, гэпов %d). Кадры: %s"
                        % (track_index, len(cues), len(gaps), sample)
                    )
                    continue
                original_name = drt.track_name(track_index)
                placed = drt.add_filled_track(
                    track_index, filled, "%s (без пауз)" % original_name
                )
                source_indices.append(track_index)
                fd, srt_path = tempfile.mkstemp(prefix="subtitle_glue_", suffix=".srt")
                os.close(fd)
                write_srt_file(srt_path, cues_to_relative(filled, origin), fps)
                seconds = extra / fps
                summaries.append(
                    "S%d: удлинено %d из %d (+%.2f с, клипов: %d) на исходном таймкоде.\nSRT: %s"
                    % (track_index, grown, len(cues), seconds, placed, srt_path)
                )
            if not source_indices:
                return summaries or ["Нечего обрабатывать."]
            drt.save(drt_out)
        finally:
            drt.close()

        new_timeline = import_timeline_drt(project, drt_out)
        if new_timeline.GetName() != wanted_name:
            try:
                new_timeline.SetName(wanted_name)
            except Exception:
                pass

        subtitle_count = int(new_timeline.GetTrackCount("subtitle") or 0)
        hide_status = "kept"
        if hide_original:
            hide_status = "disabled"
            for index in source_indices:
                if hide_subtitle_track(new_timeline, index) != "disabled":
                    hide_status = "kept"
        for index in range(subtitle_count - len(source_indices) + 1, subtitle_count + 1):
            try:
                new_timeline.SetTrackEnable("subtitle", index, True)
                new_timeline.SetTrackLock("subtitle", index, False)
            except Exception:
                pass

        hide_note = {
            "disabled": "исходные subtitle-дорожки на новом таймлайне отключены",
            "kept": "исходные subtitle-дорожки оставлены включёнными",
        }.get(hide_status, hide_status)
        summaries.append(
            "Новый таймлайн: %s. Исходный не изменён. %s."
            % (new_timeline.GetName(), hide_note)
        )
        return summaries
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def show_message(fusion_app, title, text):
    fusion_app = fusion_app or get_fusion(globals().get("resolve"))
    bmd_mod = globals().get("bmd")
    if fusion_app is None or bmd_mod is None:
        print("%s: %s" % (title, text))
        return
    ui = fusion_app.UIManager
    dispatcher = bmd_mod.UIDispatcher(ui)
    win = dispatcher.AddWindow(
        {
            "ID": "SubtitleGlueMsg",
            "WindowTitle": title,
            "Geometry": [200, 200, 520, 240],
        },
        [
            ui.VGroup(
                {"Spacing": 10},
                [
                    ui.Label({"ID": "msg", "Text": text, "WordWrap": True, "Weight": 1}),
                    ui.Button({"ID": "ok", "Text": "OK", "Weight": 0}),
                ],
            )
        ],
    )
    items = win.GetItems()

    def _close(_ev):
        dispatcher.ExitLoop()

    win.On.SubtitleGlueMsg.Close = _close
    win.On.ok.Clicked = _close
    items["msg"].Text = text
    win.Show()
    dispatcher.RunLoop()
    win.Hide()


def show_ui_and_run(resolve_app, project, timeline):
    fusion_app = get_fusion(resolve_app)
    bmd_mod = globals().get("bmd")
    if fusion_app is None or bmd_mod is None:
        return None

    tracks = list_subtitle_tracks(timeline)
    if not tracks:
        show_message(fusion_app, "Subtitle Glue", "На активном таймлайне нет subtitle-дорожек.")
        return False

    ui = fusion_app.UIManager
    dispatcher = bmd_mod.UIDispatcher(ui)
    win = dispatcher.AddWindow(
        {
            "ID": SCRIPT_ID,
            "WindowTitle": "Subtitle Glue v%s" % VERSION,
            "Geometry": [120, 120, 560, 400],
        },
        [
            ui.VGroup(
                {"Spacing": 8, "Weight": 1},
                [
                    ui.Label(
                        {
                            "Text": (
                                "Закрывает паузы между субтитрами, удлиняя каждый клип "
                                "до начала следующего. Resolve не умеет ставить SRT в нужный "
                                "кадр, поэтому результат приходит на новый таймлайн через "
                                "экспорт .drt — как в скрипте орфографии. Исходный таймлайн "
                                "не меняется, новые субтитры остаются на исходном таймкоде."
                            ),
                            "WordWrap": True,
                            "Weight": 0,
                        }
                    ),
                    ui.Label({"Text": "Дорожка:", "Weight": 0}),
                    ui.ComboBox({"ID": "trackCombo", "Weight": 0}),
                    ui.HGroup(
                        {"Weight": 0},
                        [
                            ui.Label({"Text": "Оставить зазор, кадров:", "Weight": 1}),
                            ui.SpinBox(
                                {
                                    "ID": "gapSpin",
                                    "Value": 0,
                                    "Minimum": 0,
                                    "Maximum": 30,
                                    "Weight": 0,
                                }
                            ),
                        ],
                    ),
                    ui.HGroup(
                        {"Weight": 0},
                        [
                            ui.Label(
                                {
                                    "Text": "Макс. удлинение, сек (0 = без лимита):",
                                    "Weight": 1,
                                }
                            ),
                            ui.SpinBox(
                                {
                                    "ID": "maxSpin",
                                    "Value": 0,
                                    "Minimum": 0,
                                    "Maximum": 3600,
                                    "Weight": 0,
                                }
                            ),
                        ],
                    ),
                    ui.CheckBox(
                        {
                            "ID": "replaceCheck",
                            "Text": "Отключить исходную subtitle-дорожку на новом таймлайне",
                            "Checked": True,
                            "Weight": 0,
                        }
                    ),
                    ui.Label({"ID": "status", "Text": "", "WordWrap": True, "Weight": 1}),
                    ui.HGroup(
                        {"Weight": 0},
                        [
                            ui.Button({"ID": "runBtn", "Text": "Заполнить паузы"}),
                            ui.Button({"ID": "cancelBtn", "Text": "Закрыть"}),
                        ],
                    ),
                ],
            )
        ],
    )
    items = win.GetItems()
    items["trackCombo"].AddItem("Все subtitle-дорожки")
    for track in tracks:
        items["trackCombo"].AddItem(
            "S%d: %s (%d)" % (track["index"], track["name"], track["count"])
        )

    result = {"ran": False}

    def close_window(_ev):
        dispatcher.ExitLoop()

    def on_run(_ev):
        combo_index = int(items["trackCombo"].CurrentIndex)
        gap_frames = int(items["gapSpin"].Value)
        max_seconds = int(items["maxSpin"].Value)
        hide_original = bool(items["replaceCheck"].Checked)
        if combo_index <= 0:
            selected = [track["index"] for track in tracks if track["count"] > 0]
        else:
            selected = [tracks[combo_index - 1]["index"]]
        if not selected:
            items["status"].Text = "Нет дорожек с субтитрами."
            return
        items["status"].Text = "Обработка..."
        try:
            summaries = glue_tracks(
                resolve_app,
                project,
                timeline,
                selected,
                gap_frames,
                max_seconds,
                hide_original,
            )
        except Exception as exc:
            items["status"].Text = "Ошибка: %s" % exc
            print("Subtitle Glue error: %s" % exc)
            return
        message = "\n".join(summaries) if summaries else "Нечего обрабатывать."
        items["status"].Text = message
        result["ran"] = True
        print("Subtitle Glue:\n%s" % message)

    win.On.SubtitleGlueWin.Close = close_window
    win.On.cancelBtn.Clicked = close_window
    win.On.runBtn.Clicked = on_run
    win.Show()
    dispatcher.RunLoop()
    win.Hide()
    return result["ran"]


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    try:
        resolve_app = get_resolve()
    except Exception as exc:
        print("Не удалось подключиться к DaVinci Resolve: %s" % exc)
        return 1
    if resolve_app is None:
        print("DaVinci Resolve не запущен.")
        return 1

    project = resolve_app.GetProjectManager().GetCurrentProject()
    if project is None:
        msg = "Нет активного проекта."
        show_message(get_fusion(resolve_app), "Subtitle Glue", msg)
        print(msg)
        return 1

    timeline = project.GetCurrentTimeline()
    if timeline is None:
        msg = "Нет активного таймлайна."
        show_message(get_fusion(resolve_app), "Subtitle Glue", msg)
        print(msg)
        return 1

    ui_result = None
    try:
        ui_result = show_ui_and_run(resolve_app, project, timeline)
    except Exception as exc:
        print("UI недоступен (%s), запуск с параметрами по умолчанию." % exc)
        ui_result = None

    if ui_result is None:
        tracks = list_subtitle_tracks(timeline)
        selected = [track["index"] for track in tracks if track["count"] > 0]
        if not selected:
            print("На активном таймлайне нет субтитров.")
            return 1
        try:
            summaries = glue_tracks(
                resolve_app, project, timeline, selected, 0, 0, True
            )
        except Exception as exc:
            print("Ошибка: %s" % exc)
            return 1
        print("\n".join(summaries))
    return 0


def _should_autorun():
    if globals().get("resolve") is not None:
        return True
    return __name__ == "__main__"


if _should_autorun():
    main()
