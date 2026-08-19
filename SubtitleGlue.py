# -*- coding: utf-8 -*-
"""
Subtitle Glue for DaVinci Resolve.

Reads generated subtitle clips on the active timeline and removes empty
gaps by extending each caption until the next one starts.

Native subtitle clips cannot be trimmed, moved, or reliably deleted
through the scripting API. Importing an SRT also ignores recordFrame and
is always appended after the last existing caption. This script therefore
rebuilds the result as Text+ clips on a video track (where recordFrame
works) and disables the original subtitle track.

Run from: Workspace > Scripts > Utility > SubtitleGlue
"""

from __future__ import print_function

import os
import sys
import tempfile

VERSION = "1.3.0"
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


def harvest_textplus_template(project, media_pool):
    """Create a Text+ generator on a throwaway timeline so the user sequence is untouched.

    InsertFusionTitleIntoTimeline on the active timeline inserts a 5-second
    title at the playhead. With ripple/insert that shoves video, audio and
    subtitles by exactly 5 seconds.
    """
    original = project.GetCurrentTimeline()
    tmp = None
    try:
        tmp_name = "__SubtitleGlue_Tmp__"
        tmp = media_pool.CreateEmptyTimeline(tmp_name)
        if tmp is None:
            tmp = media_pool.CreateEmptyTimeline(tmp_name + str(os.getpid()))
        if tmp is None:
            return None, 1.0, None
        project.SetCurrentTimeline(tmp)
        inserted = None
        for title_name in ("Text+", "Text"):
            try:
                inserted = tmp.InsertFusionTitleIntoTimeline(title_name)
            except Exception:
                inserted = None
            if inserted:
                break
        item = inserted if inserted and not isinstance(inserted, bool) else None
        if item is None:
            items = tmp.GetItemListInTrack("video", 1) or []
            item = items[0] if items else None
        if item is None:
            return None, 1.0, tmp
        media_item = item.GetMediaPoolItem()
        multiplier = 1.0
        if media_item is not None:
            test_duration = 200
            test_info = {
                "mediaPoolItem": media_item,
                "startFrame": 0,
                "endFrame": test_duration - 1,
                "trackIndex": 1,
                "recordFrame": int(tmp.GetStartFrame() or 0),
            }
            test_items = media_pool.AppendToTimeline([test_info])
            if test_items:
                real_duration = int(test_items[0].GetDuration() or 0)
                if real_duration > 0:
                    multiplier = float(test_duration) / float(real_duration)
                try:
                    tmp.DeleteClips(list(test_items), False)
                except Exception:
                    pass
        return media_item, multiplier, tmp
    except Exception:
        if tmp is not None:
            try:
                deleter = getattr(media_pool, "DeleteTimelines", None)
                if deleter:
                    deleter([tmp])
            except Exception:
                pass
        raise
    finally:
        if original is not None:
            try:
                project.SetCurrentTimeline(original)
            except Exception:
                pass


def set_textplus_text(timeline_item, text):
    count = int(timeline_item.GetFusionCompCount() or 0)
    for index in range(1, count + 1):
        comp = timeline_item.GetFusionCompByIndex(index)
        if not comp:
            continue
        tool = None
        finder = getattr(comp, "FindToolByID", None)
        if finder:
            try:
                tool = finder("TextPlus")
            except Exception:
                tool = None
        if tool is None:
            getter = getattr(comp, "GetToolList", None)
            tools = getter(False) if getter else None
            values = []
            if isinstance(tools, dict):
                values = list(tools.values())
            elif tools:
                try:
                    values = list(tools)
                except TypeError:
                    values = []
            for maybe in values:
                try:
                    attrs = maybe.GetAttrs() or {}
                    if attrs.get("TOOLS_RegID") == "TextPlus" or maybe.ID == "TextPlus":
                        tool = maybe
                        break
                except Exception:
                    continue
        if tool:
            tool.SetInput("StyledText", text)
            return True
    return False


def append_textplus_with_duration(
    media_pool, timeline, text_clip, track_index, record_frame, target_duration, duration_multiplier
):
    source_duration = max(1, int(target_duration * duration_multiplier + 0.999))
    for attempt in range(6):
        clip_info = {
            "mediaPoolItem": text_clip,
            "startFrame": 0,
            "endFrame": source_duration - 1,
            "trackIndex": int(track_index),
            "recordFrame": int(record_frame),
        }
        items = media_pool.AppendToTimeline([clip_info])
        if not items:
            return None
        timeline_item = items[0]
        actual = int(timeline_item.GetDuration() or 0)
        adjustment = int(target_duration) - actual
        if adjustment == 0:
            return timeline_item
        try:
            timeline.DeleteClips([timeline_item], False)
        except Exception:
            return timeline_item
        if attempt >= 4:
            source_duration = max(source_duration * 2, int(target_duration) * 4, 10000)
        else:
            source_duration = max(1, source_duration + adjustment)
    return None


def hide_subtitle_track(timeline, track_index):
    """Turn off the original captions. Do not delete — that can ripple audio."""
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


def export_srt_sidecar(cues, fps):
    fd, srt_path = tempfile.mkstemp(prefix="subtitle_glue_", suffix=".srt")
    os.close(fd)
    write_srt_file(srt_path, cues, fps)
    return srt_path


def rebuild_as_textplus(
    resolve_app, project, timeline, track_index, cues, fps, offset, hide_original
):
    """Place gap-filled captions as Text+ clips at the original timeline times."""
    try:
        resolve_app.OpenPage("edit")
    except Exception:
        pass
    try:
        project.SetCurrentTimeline(timeline)
    except Exception:
        pass

    media_pool = project.GetMediaPool()
    tmp_timeline = None
    folder, previous = find_or_create_media_folder(media_pool, MEDIA_FOLDER_NAME)
    if folder:
        media_pool.SetCurrentFolder(folder)
    try:
        text_clip, duration_multiplier, tmp_timeline = harvest_textplus_template(project, media_pool)
    finally:
        if previous:
            media_pool.SetCurrentFolder(previous)
        try:
            project.SetCurrentTimeline(timeline)
        except Exception:
            pass

    try:
        if text_clip is None:
            raise RuntimeError(
                "Не удалось создать шаблон Text+.\n"
                "Откройте Effects, перетащите Fusion Title «Text+» в Media Pool и повторите."
            )

        if not timeline.AddTrack("video"):
            raise RuntimeError("Не удалось создать видеодорожку для Text+.")
        video_track = int(timeline.GetTrackCount("video"))
        original_name = timeline.GetTrackName("subtitle", track_index) or ("Subtitle %d" % track_index)
        timeline.SetTrackName("video", video_track, "%s (без пауз)" % original_name)

        created = []
        first_expected = to_record_frame(cues[0].start, offset) if cues else 0
        for cue in cues:
            duration = max(1, cue.end - cue.start)
            record_frame = to_record_frame(cue.start, offset)
            item = append_textplus_with_duration(
                media_pool,
                timeline,
                text_clip,
                video_track,
                record_frame,
                duration,
                duration_multiplier,
            )
            if item is None:
                continue
            try:
                item.SetClipColor("Green")
            except Exception:
                pass
            set_textplus_text(item, cue.text)
            created.append(item)

        if not created:
            try:
                timeline.DeleteTrack("video", video_track)
            except Exception:
                pass
            raise RuntimeError("Не удалось поставить Text+ клипы на таймлайн.")

        actual_first = int(created[0].GetStart())
        if not is_placement_aligned(actual_first, [first_expected, offset, 0, get_timeline_start(timeline)]):
            print(
                "Subtitle Glue warning: first Text+ starts at %s, expected %s"
                % (actual_first, first_expected)
            )

        hide_status = "kept"
        if hide_original:
            hide_status = hide_subtitle_track(timeline, track_index)

        srt_path = export_srt_sidecar(cues, fps)
        return srt_path, len(created), hide_status
    finally:
        if tmp_timeline is not None:
            try:
                project.SetCurrentTimeline(timeline)
            except Exception:
                pass
            deleter = getattr(media_pool, "DeleteTimelines", None)
            if deleter:
                try:
                    deleter([tmp_timeline])
                except Exception:
                    pass


def glue_tracks(resolve_app, project, timeline, track_indices, gap_frames, max_extend_seconds, replace_original):
    fps = get_fps(timeline)
    max_extend_frames = int(round(float(max_extend_seconds) * fps)) if max_extend_seconds else 0

    summaries = []
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
        srt_path, placed_count, hide_status = rebuild_as_textplus(
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
        hide_note = {
            "deleted": "исходная subtitle-дорожка удалена",
            "cleared": "исходные клипы удалены",
            "disabled": "исходная subtitle-дорожка отключена (API не даёт удалить клипы)",
            "kept": "исходная дорожка оставлена",
        }.get(hide_status, hide_status)
        summaries.append(
            "S%d: Text+ %d клипов, удлинено %d из %d (+%.2f с). %s.\nSRT: %s"
            % (track_index, placed_count, grown, len(cues), seconds, hide_note, srt_path)
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
                                "Закрывает паузы между субтитрами. Resolve не умеет "
                                "менять длительность subtitle-клипов и всегда дописывает "
                                "SRT в конец дорожки, поэтому результат ставится как Text+ "
                                "на видеодорожку в исходных таймкодах."
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
