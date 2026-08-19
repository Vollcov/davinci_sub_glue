# -*- coding: utf-8 -*-
"""
Subtitle Glue for DaVinci Resolve.

Reads generated subtitle clips on the active timeline and removes empty
gaps by extending each caption until the next one starts.

Resolve cannot trim native subtitle clips in place, and importing an SRT
ignores recordFrame: the new captions are appended after the last existing
subtitle. This script keeps that 1.0.0 behaviour because it actually fills
gaps. Original clips are only disabled, never deleted first.

Run from: Workspace > Scripts > Utility > SubtitleGlue
"""

from __future__ import print_function

import os
import sys
import tempfile

VERSION = "1.6.0"
SCRIPT_ID = "SubtitleGlueWin"
MEDIA_FOLDER_NAME = "Subtitle Glue"


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


def find_or_create_media_folder(media_pool, name):
    current = media_pool.GetCurrentFolder()
    root = media_pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == name:
            return folder, current
    created = media_pool.AddSubFolder(root, name)
    return created or root, current


def hide_subtitle_track(timeline, track_index):
    """Turn off the original captions. Do not delete — that can wipe the timeline."""
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


def set_playhead_start(timeline):
    getter = getattr(timeline, "GetStartTimecode", None)
    setter = getattr(timeline, "SetCurrentTimecode", None)
    if not getter or not setter:
        return
    try:
        timecode = getter()
        if timecode:
            setter(timecode)
    except Exception:
        pass


def place_srt_on_track(media_pool, timeline, clip, track_index, record_frames):
    """Try recordFrame values, then a plain append. Keep whatever Resolve accepts."""
    for record_frame in list(record_frames) + [None]:
        if record_frame is None:
            info = {"mediaPoolItem": clip, "trackIndex": int(track_index)}
        else:
            info = {
                "mediaPoolItem": clip,
                "recordFrame": int(record_frame),
                "trackIndex": int(track_index),
            }
        placed = media_pool.AppendToTimeline([info])
        if placed:
            return placed
    return media_pool.AppendToTimeline([clip])


def rebuild_track_from_srt(
    resolve_app, project, timeline, track_index, filled_cues, fps, origin, hide_original
):
    """Import a gap-filled SRT onto a new subtitle track. Never delete originals first."""
    media_pool = project.GetMediaPool()
    original_name = timeline.GetTrackName("subtitle", track_index) or ("Subtitle %d" % track_index)

    fd, srt_path = tempfile.mkstemp(prefix="subtitle_glue_", suffix=".srt")
    os.close(fd)
    write_srt_file(srt_path, cues_to_relative(filled_cues, origin), fps)

    try:
        resolve_app.OpenPage("edit")
    except Exception:
        pass

    folder, previous_folder = find_or_create_media_folder(media_pool, MEDIA_FOLDER_NAME)
    if folder:
        media_pool.SetCurrentFolder(folder)
    imported = media_pool.ImportMedia([os.path.abspath(srt_path)])
    if previous_folder:
        media_pool.SetCurrentFolder(previous_folder)

    if not imported:
        raise RuntimeError(
            "Не удалось импортировать SRT в Media Pool.\nФайл сохранён: %s" % srt_path
        )
    clip = imported[0]

    try:
        if timeline.GetIsTrackLocked("subtitle", track_index):
            timeline.SetTrackLock("subtitle", track_index, False)
    except Exception:
        pass

    before_count = int(timeline.GetTrackCount("subtitle") or 0)
    if not timeline.AddTrack("subtitle"):
        raise RuntimeError("Не удалось создать новую subtitle-дорожку.")
    new_index = int(timeline.GetTrackCount("subtitle") or 0)
    if new_index <= before_count:
        new_index = before_count + 1
    timeline.SetTrackName("subtitle", new_index, "%s (без пауз)" % original_name)
    try:
        timeline.SetTrackLock("subtitle", new_index, False)
        timeline.SetTrackEnable("subtitle", new_index, True)
    except Exception:
        pass

    set_playhead_start(timeline)
    first_abs = filled_cues[0].start if filled_cues else origin
    record_frames = record_frame_candidates(
        get_timeline_start(timeline), origin, first_abs
    )
    placed = place_srt_on_track(media_pool, timeline, clip, new_index, record_frames)
    if not placed:
        try:
            timeline.DeleteTrack("subtitle", new_index)
        except Exception:
            pass
        raise RuntimeError(
            "Не удалось положить SRT на таймлайн.\nИмпортируйте вручную: %s" % srt_path
        )

    new_items = timeline.GetItemListInTrack("subtitle", new_index) or []
    if not new_items:
        # Resolve may have appended onto another subtitle track. Keep those clips.
        for index in range(1, int(timeline.GetTrackCount("subtitle") or 0) + 1):
            if index == track_index:
                continue
            extra = timeline.GetItemListInTrack("subtitle", index) or []
            if extra:
                new_index = index
                new_items = extra
                break

    if not new_items:
        raise RuntimeError(
            "После импорта клипов не видно.\nФайл сохранён: %s" % srt_path
        )

    hide_status = "kept"
    if hide_original:
        hide_status = hide_subtitle_track(timeline, track_index)

    first_start = min(item_span(item)[0] for item in new_items)
    aligned = first_start_is_aligned(first_start, filled_cues, origin)
    return srt_path, len(new_items), hide_status, aligned, first_start


def glue_tracks(resolve_app, project, timeline, track_indices, gap_frames, max_extend_seconds, hide_original):
    fps = get_fps(timeline)
    max_extend_frames = int(round(float(max_extend_seconds) * fps)) if max_extend_seconds else 0

    summaries = []
    for track_index in sorted(track_indices, reverse=True):
        cues, _items, origin = read_subtitle_track(timeline, track_index)
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
        srt_path, placed_count, hide_status, aligned, first_start = rebuild_track_from_srt(
            resolve_app,
            project,
            timeline,
            track_index,
            filled,
            fps,
            origin,
            hide_original,
        )
        seconds = extra / fps
        hide_note = {
            "disabled": "исходная дорожка отключена",
            "kept": "исходная дорожка оставлена",
        }.get(hide_status, hide_status)
        place_note = (
            "новые клипы на исходном таймкоде"
            if aligned
            else "Resolve дописал SRT после старых субтитров (кадр %d); гэпы закрыты"
            % first_start
        )
        summaries.append(
            "S%d: удлинено %d из %d (+%.2f с, клипов: %d). %s. %s.\nSRT: %s"
            % (track_index, grown, len(cues), seconds, placed_count, hide_note, place_note, srt_path)
        )
    return summaries


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
                                "до начала следующего. Resolve не умеет править длительность "
                                "уже лежащих subtitle-клипов и всегда дописывает SRT после "
                                "них — новые субтитры без пауз появятся на отдельной дорожке. "
                                "Исходные клипы не удаляются."
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
                            "Text": "Скрыть исходную subtitle-дорожку",
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
