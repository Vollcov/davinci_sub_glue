# -*- coding: utf-8 -*-
"""
Subtitle Glue for DaVinci Resolve.

Reads subtitle clips on the active timeline and removes empty gaps by
extending each caption until the next one starts. Resolve cannot trim
native subtitle items in place, so the script rebuilds the track from
an SRT file with the new timings.

Run from: Workspace > Scripts > Utility > SubtitleGlue
"""

from __future__ import print_function

import os
import sys
import tempfile

VERSION = "1.1.0"
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
            # Never overlap the next cue and never shrink the original.
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


def count_extended(original, filled_cues):
    """How many cues grew, and by how many frames."""
    grown = 0
    extra = 0
    for before, after in zip(original, filled_cues):
        if after.end > before.end:
            grown += 1
            extra += after.end - before.end
    return grown, extra


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
    """Frames to try when placing an SRT clip.

    Resolve is inconsistent about whether recordFrame is 0-based from the
    timeline start or an absolute GetStartFrame() value. Trying 0 first
    avoids dropping the clip one hour later (01:00:00:00) at the end of
    the old subtitles.
    """
    return unique_frames(0, offset, timeline_start, first_cue_abs)


def is_placement_aligned(actual_start, expected_starts, tolerance=2):
    """True if the first placed subtitle starts at a timeline origin."""
    if actual_start is None:
        return False
    actual_start = int(actual_start)
    for expected in expected_starts:
        if expected is None:
            continue
        if abs(actual_start - int(expected)) <= int(tolerance):
            return True
    return False


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

def read_subtitle_track(timeline, track_index):
    items = timeline.GetItemListInTrack("subtitle", track_index) or []
    cues = []
    timeline_start = get_timeline_start(timeline)
    min_start = None
    for item in items:
        start = int(item.GetStart())
        end = int(item.GetEnd())
        duration = int(item.GetDuration() or 0)
        if end <= start and duration > 0:
            end = start + duration
        if min_start is None or start < min_start:
            min_start = start
        cues.append(Cue(start, end, item.GetName() or ""))

    # Convert to timeline-relative frames for the SRT file.
    offset = timeline_start
    if cues and min_start is not None and min_start < timeline_start:
        offset = 0
    relative = [Cue(cue.start - offset, cue.end - offset, cue.text) for cue in cues]
    relative.sort(key=lambda cue: (cue.start, cue.end))
    return relative, items, offset


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


def subtitle_track_count(timeline):
    return int(timeline.GetTrackCount("subtitle") or 0)


def items_on_track(timeline, track_index):
    return list(timeline.GetItemListInTrack("subtitle", track_index) or [])


def all_subtitle_items(timeline):
    items = []
    for index in range(1, subtitle_track_count(timeline) + 1):
        items.extend(items_on_track(timeline, index))
    return items


def first_item_start(items):
    if not items:
        return None
    return min(int(item.GetStart()) for item in items)


def find_empty_subtitle_tracks(timeline):
    empty = []
    for index in range(1, subtitle_track_count(timeline) + 1):
        if not items_on_track(timeline, index):
            empty.append(index)
    return empty


def set_playhead_to_start(timeline):
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


def capture_track_writable_state(timeline):
    states = []
    for index in range(1, subtitle_track_count(timeline) + 1):
        states.append({
            "index": index,
            "locked": bool(timeline.GetIsTrackLocked("subtitle", index)),
            "enabled": bool(timeline.GetIsTrackEnabled("subtitle", index)),
        })
    return states


def isolate_subtitle_track(timeline, target_index):
    states = capture_track_writable_state(timeline)
    for state in states:
        index = state["index"]
        if index == target_index:
            timeline.SetTrackLock("subtitle", index, False)
            timeline.SetTrackEnable("subtitle", index, True)
        else:
            timeline.SetTrackLock("subtitle", index, True)
    return states


def restore_track_writable_state(timeline, states):
    count = subtitle_track_count(timeline)
    for state in states:
        index = state["index"]
        if index < 1 or index > count:
            continue
        try:
            timeline.SetTrackLock("subtitle", index, state["locked"])
            timeline.SetTrackEnable("subtitle", index, state["enabled"])
        except Exception:
            pass


def delete_timeline_items(timeline, items):
    if not items:
        return True
    try:
        return bool(timeline.DeleteClips(list(items), False))
    except Exception:
        return False


def add_empty_subtitle_track(timeline, name):
    before_empty = set(find_empty_subtitle_tracks(timeline))
    before_count = subtitle_track_count(timeline)
    if not timeline.AddTrack("subtitle"):
        return None
    created = [
        index
        for index in find_empty_subtitle_tracks(timeline)
        if index not in before_empty
    ]
    if not created:
        index = subtitle_track_count(timeline)
    elif 1 in created and subtitle_track_count(timeline) == before_count + 1:
        # AddTrack prepended the new empty track.
        index = 1
    else:
        index = max(created)
    if name:
        timeline.SetTrackName("subtitle", index, name)
    return index


def clear_subtitle_track(timeline, track_index, original_name):
    """Remove clips from a subtitle track, recreating it if DeleteClips fails."""
    items = items_on_track(timeline, track_index)
    if items:
        delete_timeline_items(timeline, items)
    if not items_on_track(timeline, track_index):
        return track_index
    try:
        timeline.DeleteTrack("subtitle", track_index)
    except Exception:
        pass
    index = add_empty_subtitle_track(timeline, original_name)
    return index


def append_srt_clip(media_pool, clip, track_index, record_frame):
    attempts = [
        {
            "mediaPoolItem": clip,
            "recordFrame": int(record_frame),
            "trackIndex": int(track_index),
        },
        {
            "mediaPoolItem": clip,
            "startFrame": 0,
            "recordFrame": int(record_frame),
            "trackIndex": int(track_index),
        },
        {
            "mediaPoolItem": clip,
            "recordFrame": int(record_frame),
        },
    ]
    for info in attempts:
        placed = media_pool.AppendToTimeline([info])
        if placed:
            return placed
    return None


def new_items_since(timeline, before_ids):
    before = set(before_ids)
    return [item for item in all_subtitle_items(timeline) if id(item) not in before]


def place_srt_aligned(media_pool, timeline, clip, track_index, expected_starts):
    """Place an SRT so the first caption starts at the beginning of the timeline."""
    set_playhead_to_start(timeline)
    states = isolate_subtitle_track(timeline, track_index)
    last_new = []
    try:
        candidates = unique_frames(*(expected_starts or [0]))
        for record_frame in candidates:
            before_ids = [id(item) for item in all_subtitle_items(timeline)]
            placed = append_srt_clip(media_pool, clip, track_index, record_frame)
            new_items = new_items_since(timeline, before_ids)
            if not new_items and placed:
                new_items = list(placed)
            last_new = new_items
            actual = first_item_start(new_items)
            print(
                "Subtitle Glue: place recordFrame=%s start=%s expected=%s"
                % (record_frame, actual, expected_starts)
            )
            if new_items and is_placement_aligned(actual, expected_starts):
                return new_items
            if new_items:
                delete_timeline_items(timeline, new_items)

        before_ids = [id(item) for item in all_subtitle_items(timeline)]
        placed = media_pool.AppendToTimeline([clip])
        new_items = new_items_since(timeline, before_ids)
        if not new_items and placed:
            new_items = list(placed)
        last_new = new_items
        actual = first_item_start(new_items)
        print(
            "Subtitle Glue: place append-fallback start=%s expected=%s"
            % (actual, expected_starts)
        )
        if new_items and is_placement_aligned(actual, expected_starts):
            return new_items
        dest_items = items_on_track(timeline, track_index)
        if dest_items and is_placement_aligned(first_item_start(dest_items), expected_starts):
            return dest_items
        return last_new
    finally:
        restore_track_writable_state(timeline, states)


def _item_is_on_subtitle_track(item, expected_index):
    getter = getattr(item, "GetTrackTypeAndIndex", None)
    if getter is None:
        return True
    try:
        info = getter()
    except Exception:
        return True
    if not info:
        return True
    track_type, track_index = info[0], info[1]
    if str(track_type).lower() != "subtitle":
        return False
    if expected_index and int(track_index) != int(expected_index):
        return False
    return True


def rebuild_track_from_srt(resolve_app, project, timeline, track_index, cues, fps, offset, replace_original):
    media_pool = project.GetMediaPool()
    original_name = timeline.GetTrackName("subtitle", track_index) or ("Subtitle %d" % track_index)
    timeline_start = get_timeline_start(timeline)
    first_cue_abs = (cues[0].start + offset) if cues else offset
    expected_starts = record_frame_candidates(timeline_start, offset, first_cue_abs)

    fd, srt_path = tempfile.mkstemp(prefix="subtitle_glue_", suffix=".srt")
    os.close(fd)
    write_srt_file(srt_path, cues, fps)

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

    if timeline.GetIsTrackLocked("subtitle", track_index):
        timeline.SetTrackLock("subtitle", track_index, False)

    destination = track_index
    extra_track = None
    if replace_original:
        # Clear first. AppendToTimeline ignores recordFrame for SRT and
        # otherwise concatenates after the old captions.
        destination = clear_subtitle_track(timeline, track_index, original_name)
        if destination is None:
            raise RuntimeError("Не удалось очистить исходную subtitle-дорожку.")
    else:
        extra_track = add_empty_subtitle_track(timeline, "%s (без пауз)" % original_name)
        if extra_track is None:
            raise RuntimeError("Не удалось создать новую subtitle-дорожку.")
        destination = extra_track

    placed_items = place_srt_aligned(
        media_pool, timeline, clip, destination, expected_starts
    )
    if not placed_items:
        if extra_track:
            try:
                timeline.DeleteTrack("subtitle", extra_track)
            except Exception:
                pass
        raise RuntimeError(
            "Не удалось положить SRT на таймлайн.\nИмпортируйте вручную: %s" % srt_path
        )

    actual_start = first_item_start(placed_items)
    if not is_placement_aligned(actual_start, expected_starts):
        delete_timeline_items(timeline, placed_items)
        if extra_track:
            try:
                timeline.DeleteTrack("subtitle", extra_track)
            except Exception:
                pass
        raise RuntimeError(
            "SRT попал не в начало таймлайна (кадр %s, ожидалось %s).\n"
            "Перетащите файл на пустую subtitle-дорожку в начало таймлайна:\n%s"
            % (actual_start, expected_starts, srt_path)
        )

    if not _item_is_on_subtitle_track(placed_items[0], destination):
        delete_timeline_items(timeline, placed_items)
        if extra_track:
            try:
                timeline.DeleteTrack("subtitle", extra_track)
            except Exception:
                pass
        raise RuntimeError(
            "Resolve положил клип не на subtitle-дорожку.\n"
            "Перетащите файл на subtitle-трек вручную:\n%s" % srt_path
        )

    new_items = items_on_track(timeline, destination) or placed_items
    if not new_items:
        if extra_track:
            try:
                timeline.DeleteTrack("subtitle", extra_track)
            except Exception:
                pass
        raise RuntimeError(
            "Новая дорожка пустая после импорта.\nФайл: %s" % srt_path
        )

    timeline.SetTrackName("subtitle", destination, original_name if replace_original else ("%s (без пауз)" % original_name))
    return srt_path, len(new_items)


def glue_tracks(resolve_app, project, timeline, track_indices, gap_frames, max_extend_seconds, replace_original):
    fps = get_fps(timeline)
    max_extend_frames = int(round(float(max_extend_seconds) * fps)) if max_extend_seconds else 0

    summaries = []
    # Process from highest index to lowest so deletions do not shift remaining targets.
    for track_index in sorted(track_indices, reverse=True):
        cues, _items, offset = read_subtitle_track(timeline, track_index)
        if len(cues) < 2:
            summaries.append(
                "S%d: недостаточно субтитров (%d)." % (track_index, len(cues))
            )
            continue
        filled, grown, extra = fill_gaps(cues, gap_frames, max_extend_frames)
        if grown == 0:
            summaries.append("S%d: пауз нет, дорожка не изменена." % track_index)
            continue
        srt_path, placed_count = rebuild_track_from_srt(
            resolve_app,
            project,
            timeline,
            track_index,
            filled,
            fps,
            offset,
            replace_original,
        )
        seconds = extra / fps
        summaries.append(
            "S%d: удлинено %d из %d субтитров (+%.2f с, клипов: %d).\nSRT: %s"
            % (track_index, grown, len(cues), seconds, placed_count, srt_path)
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
            "Geometry": [200, 200, 520, 220],
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
            "Geometry": [120, 120, 520, 360],
        },
        [
            ui.VGroup(
                {"Spacing": 8, "Weight": 1},
                [
                    ui.Label(
                        {
                            "Text": "Удлиняет субтитры на активном таймлайне, чтобы закрыть пустые паузы между ними.",
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
                            "Text": "Заменить исходную дорожку",
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
        replace_original = bool(items["replaceCheck"].Checked)
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
                replace_original,
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
