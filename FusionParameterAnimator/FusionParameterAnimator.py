
import csv
import datetime
import html
import json
import math
import os
import re
import traceback

import adsk.core
import adsk.fusion


# -----------------------------------------------------------------------------
# USER CONFIGURATION
# -----------------------------------------------------------------------------
# Keyframe numbers are zero based. The "ease" on a keyframe controls the
# transition FROM that keyframe TO the next one. Supported easing modes:
# "linear", "smoothstep", "smootherstep", "ease_in_out_cubic", and "hold".
#
# Always include units in keyframe values (for example "80 mm" or "2 deg").
# Parameter names are case-sensitive and must be USER parameters.
CONFIG = {
    # These are dialog defaults. The dialog remembers the last successfully
    # submitted values in ParametricViewportAnimator.settings.json.
    "frame_count": 180,
    "fps": 30,
    "image_width": 1920,
    "image_height": 1080,
    "anti_alias": True,

    # One turn over frame_count frames. Use a negative number to reverse it.
    "orbit_turns": 1.0,
    "orbit_axis": [0.0, 0.0, 1.0],
    # "linear", "smoothstep", "smootherstep", or "ease_in_out_cubic".
    # "smootherstep" starts and ends especially gently.
    "orbit_easing": "smootherstep",
    # True reaches the exact final angle on the last frame, which gives an
    # eased orbit a complete stop. False omits the endpoint, which is better
    # for a continuously repeating linear turntable loop.
    "orbit_include_endpoint": True,
    # "model_center" or "current_camera_target"
    "orbit_target": "model_center",

    # Fit only once; never fit per frame, because that causes zoom pumping.
    # For complete control, set this to False and frame the largest expected
    # version of the model manually before running.
    "fit_at_start": True,
    "camera_padding": 1.15,

    "restore_model_after_capture": True,
    "restore_camera_after_capture": True,
    "stop_on_timeline_error": True,

    "tracks": [
        {
            "name": "width",
            "keyframes": [
                {"frame": 0, "value": "50 mm", "ease": "smoothstep"},
                {"frame": 59, "value": "100 mm", "ease": "smoothstep"},
                {"frame": 119, "value": "50 mm", "ease": "hold"},
                {"frame": 179, "value": "50 mm"},
            ],
        },
        {
            "name": "thickness",
            "keyframes": [
                {"frame": 0, "value": "3 mm", "ease": "hold"},
                {"frame": 59, "value": "3 mm", "ease": "smoothstep"},
                {"frame": 89, "value": "10 mm", "ease": "smoothstep"},
                {"frame": 119, "value": "3 mm", "ease": "hold"},
                {"frame": 179, "value": "3 mm"},
            ],
        },
    ],
}


APP = adsk.core.Application.get()
UI = APP.userInterface if APP else None

COMMAND_ID = "OpenAI_ParametricViewportAnimator"
COMMAND_NAME = "Parametric Viewport Animation"
SCRIPT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
MENU_SETTINGS_PATH = os.path.join(
    SCRIPT_DIRECTORY, "ParametricViewportAnimator.settings.json"
)

# Event handlers and command inputs must stay referenced while the dialog is
# alive or Fusion can release them before their events fire.
HANDLERS = []
DIALOG_INPUTS = {}
DIALOG_OPTION_SETS = {}

EASING_OPTIONS = [
    ("Smootherstep (gentlest)", "smootherstep"),
    ("Smoothstep", "smoothstep"),
    ("Ease in/out cubic", "ease_in_out_cubic"),
    ("Linear", "linear"),
]
TARGET_OPTIONS = [
    ("Model center", "model_center"),
    ("Current camera target", "current_camera_target"),
]
AXIS_OPTIONS = [
    ("X axis", [1.0, 0.0, 0.0]),
    ("Y axis", [0.0, 1.0, 0.0]),
    ("Z axis", [0.0, 0.0, 1.0]),
    ("Negative X axis", [-1.0, 0.0, 0.0]),
    ("Negative Y axis", [0.0, -1.0, 0.0]),
    ("Negative Z axis", [0.0, 0.0, -1.0]),
]

MENU_SETTING_KEYS = (
    "animation_name",
    "output_parent",
    "frame_count",
    "fps",
    "image_width",
    "image_height",
    "anti_alias",
    "orbit_turns",
    "orbit_axis",
    "orbit_easing",
    "orbit_include_endpoint",
    "orbit_target",
    "fit_at_start",
    "camera_padding",
    "restore_model_after_capture",
    "restore_camera_after_capture",
    "stop_on_timeline_error",
)


class AnimationError(RuntimeError):
    """A configuration, modeling, camera, or capture failure."""


def _max_track_frame():
    maximum = 1
    for track in CONFIG.get("tracks", []):
        for keyframe in track.get("keyframes", []):
            frame = keyframe.get("frame")
            if isinstance(frame, int):
                maximum = max(maximum, frame)
    return maximum


def _default_output_parent():
    desktop = os.path.expanduser("~/Desktop")
    if os.path.isdir(desktop):
        return desktop
    return os.path.expanduser("~")


def _menu_defaults():
    document_name = (
        APP.activeDocument.name if APP and APP.activeDocument else "FusionDesign"
    )
    return {
        "animation_name": document_name,
        "output_parent": _default_output_parent(),
        **{key: CONFIG[key] for key in MENU_SETTING_KEYS if key in CONFIG},
    }


def _load_menu_settings():
    settings = _menu_defaults()
    try:
        if not os.path.isfile(MENU_SETTINGS_PATH):
            return settings
        with open(MENU_SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
            saved = json.load(settings_file)
        if not isinstance(saved, dict):
            return settings

        active_document = (
            APP.activeDocument.name if APP and APP.activeDocument else ""
        )
        for key in MENU_SETTING_KEYS:
            if (
                key == "animation_name"
                and saved.get("source_document") != active_document
            ):
                continue
            if key in saved:
                settings[key] = saved[key]
    except Exception:
        if APP:
            APP.log(
                "Parametric Viewport Animator could not load saved menu settings:\n{}".format(
                    traceback.format_exc()
                )
            )
    return settings


def _save_menu_settings(settings):
    saved = {key: settings[key] for key in MENU_SETTING_KEYS if key in settings}
    saved["source_document"] = (
        APP.activeDocument.name if APP and APP.activeDocument else ""
    )
    try:
        with open(MENU_SETTINGS_PATH, "w", encoding="utf-8") as settings_file:
            json.dump(saved, settings_file, indent=2)
    except Exception:
        # Remembering menu values is convenient, but it must never prevent a run.
        if APP:
            APP.log(
                "Parametric Viewport Animator could not save menu settings:\n{}".format(
                    traceback.format_exc()
                )
            )


def _clamped_int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _clamped_float(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _bool_setting(value, default):
    return value if isinstance(value, bool) else default


def _option_label(options, selected_value):
    for label, value in options:
        if value == selected_value:
            return label
    return options[0][0]


def _selected_option_value(input_id, options):
    selected = DIALOG_INPUTS[input_id].selectedItem
    selected_name = selected.name if selected else options[0][0]
    for label, value in options:
        if label == selected_name:
            return value
    return options[0][1]


def _add_dropdown(
    command_inputs, input_id, name, options, selected_value, tooltip
):
    drop_down = command_inputs.addDropDownCommandInput(
        input_id, name, adsk.core.DropDownStyles.TextListDropDownStyle
    )
    drop_down.tooltip = tooltip
    selected_label = _option_label(options, selected_value)
    for label, _value in options:
        drop_down.listItems.add(label, label == selected_label, "")
    DIALOG_INPUTS[input_id] = drop_down
    return drop_down


def _tracks_summary_html():
    lines = []
    for track in CONFIG.get("tracks", []):
        name = html.escape(str(track.get("name", "Unnamed")))
        keyframes = track.get("keyframes", [])
        points = [
            "{} <i>(frame {})</i>".format(
                html.escape(str(item.get("value", "?"))),
                html.escape(str(item.get("frame", "?"))),
            )
            for item in keyframes
        ]
        rows = [" &nbsp;→&nbsp; ".join(points[index:index + 3])
                for index in range(0, len(points), 3)]
        lines.append(
            "<b>{}</b><br/>{}".format(
                name, "<br/>".join(rows) if rows else "<i>No keyframes</i>"
            )
        )
    lines.append(
        "<br/><i>Each entry shows the parameter value first and its keyframe "
        "number second. Edit tracks in the CONFIG block.</i>"
    )
    return "<br/><br/>".join(lines)


def _update_duration_display():
    if not DIALOG_INPUTS:
        return
    frames = DIALOG_INPUTS["frame_count"].value
    fps = DIALOG_INPUTS["fps"].value
    duration = frames / float(fps) if fps else 0.0
    DIALOG_INPUTS["duration"].value = "{:.2f} seconds".format(duration)


def _normalized_output_path(raw_path):
    return os.path.abspath(os.path.expanduser(raw_path.strip()))


def _mark_dialog_errors():
    name_input = DIALOG_INPUTS.get("animation_name")
    path_input = DIALOG_INPUTS.get("output_parent")
    if not name_input or not path_input:
        return False

    valid_name = bool(name_input.value.strip())
    raw_path = path_input.value.strip()
    output_path = _normalized_output_path(raw_path) if raw_path else ""
    valid_path = bool(
        output_path
        and os.path.isdir(output_path)
        and os.access(output_path, os.W_OK)
    )
    name_input.isValueError = not valid_name
    path_input.isValueError = not valid_path
    return valid_name and valid_path


def _read_dialog_settings():
    settings = {
        "animation_name": DIALOG_INPUTS["animation_name"].value.strip(),
        "output_parent": _normalized_output_path(
            DIALOG_INPUTS["output_parent"].value
        ),
        "frame_count": int(DIALOG_INPUTS["frame_count"].value),
        "fps": int(DIALOG_INPUTS["fps"].value),
        "image_width": int(DIALOG_INPUTS["image_width"].value),
        "image_height": int(DIALOG_INPUTS["image_height"].value),
        "anti_alias": bool(DIALOG_INPUTS["anti_alias"].value),
        "orbit_turns": float(DIALOG_INPUTS["orbit_turns"].value),
        "orbit_axis": _selected_option_value(
            "orbit_axis", DIALOG_OPTION_SETS["orbit_axis"]
        ),
        "orbit_easing": _selected_option_value(
            "orbit_easing", DIALOG_OPTION_SETS["orbit_easing"]
        ),
        "orbit_include_endpoint": bool(
            DIALOG_INPUTS["orbit_include_endpoint"].value
        ),
        "orbit_target": _selected_option_value(
            "orbit_target", DIALOG_OPTION_SETS["orbit_target"]
        ),
        "fit_at_start": bool(DIALOG_INPUTS["fit_at_start"].value),
        "camera_padding": float(DIALOG_INPUTS["camera_padding"].value),
        "restore_model_after_capture": bool(
            DIALOG_INPUTS["restore_model_after_capture"].value
        ),
        "restore_camera_after_capture": bool(
            DIALOG_INPUTS["restore_camera_after_capture"].value
        ),
        "stop_on_timeline_error": bool(
            DIALOG_INPUTS["stop_on_timeline_error"].value
        ),
    }
    return settings


def _create_dialog_inputs(command, settings):
    DIALOG_INPUTS.clear()
    DIALOG_OPTION_SETS.clear()
    command.isRepeatable = False
    command.okButtonText = "Start Capture"
    command.setDialogInitialSize(440, 720)
    command.setDialogMinimumSize(380, 520)
    inputs = command.commandInputs

    tracks_group = inputs.addGroupCommandInput("tracks_group", "Parameter Tracks")
    tracks_group.isExpanded = True
    tracks_group.tooltip = (
        "Read-only overview of the user parameters and keyframes configured in "
        "the script's CONFIG block."
    )
    summary_rows = max(4, min(10, len(CONFIG.get("tracks", [])) * 3 + 1))
    summary = tracks_group.children.addTextBoxCommandInput(
        "tracks_summary", "", _tracks_summary_html(), summary_rows, True
    )
    summary.isFullWidth = True
    summary.tooltip = (
        "Each keyframe is shown as parameter value followed by frame number. "
        "Tracks are edited in the CONFIG block near the top of the Python file."
    )

    output_group = inputs.addGroupCommandInput("output_group", "Output & Image")
    output_group.isExpanded = True
    output_group.tooltip = (
        "Choose the output location, frame size, and viewport image quality."
    )
    output_inputs = output_group.children
    animation_name = settings.get("animation_name")
    if not isinstance(animation_name, str) or not animation_name.strip():
        animation_name = _menu_defaults()["animation_name"]
    DIALOG_INPUTS["animation_name"] = output_inputs.addStringValueInput(
        "animation_name", "Animation Name", animation_name
    )
    DIALOG_INPUTS["animation_name"].tooltip = (
        "Names the timestamped output folder and the suggested video filename."
    )
    output_parent = settings.get("output_parent")
    if not isinstance(output_parent, str):
        output_parent = _default_output_parent()
    DIALOG_INPUTS["output_parent"] = output_inputs.addStringValueInput(
        "output_parent", "Output Folder", output_parent
    )
    DIALOG_INPUTS["output_parent"].tooltip = (
        "Choose the parent folder. A new timestamped animation folder will be "
        "created inside it, so existing files are not overwritten."
    )
    browse = output_inputs.addBoolValueInput(
        "browse_output", "", False, "", False
    )
    browse.text = "Choose Output Folder…"
    browse.isFullWidth = True
    browse.tooltip = (
        "Open Fusion's folder picker and select where the new animation folder "
        "will be created."
    )
    DIALOG_INPUTS["browse_output"] = browse

    DIALOG_INPUTS["image_width"] = output_inputs.addIntegerSpinnerCommandInput(
        "image_width",
        "Image Width (px)",
        16,
        16384,
        16,
        _clamped_int(settings.get("image_width"), 1920, 16, 16384),
    )
    DIALOG_INPUTS["image_width"].tooltip = (
        "Width of every captured PNG in pixels. Higher values take more time and "
        "disk space."
    )
    DIALOG_INPUTS["image_height"] = output_inputs.addIntegerSpinnerCommandInput(
        "image_height",
        "Image Height (px)",
        16,
        16384,
        16,
        _clamped_int(settings.get("image_height"), 1080, 16, 16384),
    )
    DIALOG_INPUTS["image_height"].tooltip = (
        "Height of every captured PNG in pixels. Use the aspect ratio required "
        "by the final video."
    )
    DIALOG_INPUTS["anti_alias"] = output_inputs.addBoolValueInput(
        "anti_alias",
        "Anti-alias Image",
        True,
        "",
        _bool_setting(settings.get("anti_alias"), True),
    )
    DIALOG_INPUTS["anti_alias"].tooltip = (
        "Smooth jagged model edges in each PNG. This improves quality but can "
        "make capture slightly slower."
    )

    timing_group = inputs.addGroupCommandInput("timing_group", "Timing")
    timing_group.isExpanded = True
    timing_group.tooltip = (
        "Set the number of captured frames and their intended video playback rate."
    )
    timing_inputs = timing_group.children
    minimum_frames = _max_track_frame() + 1
    frame_count = _clamped_int(
        settings.get("frame_count"), max(180, minimum_frames), minimum_frames, 1000000
    )
    DIALOG_INPUTS["frame_count"] = timing_inputs.addIntegerSpinnerCommandInput(
        "frame_count", "Frame Count", minimum_frames, 1000000, 1, frame_count
    )
    DIALOG_INPUTS["frame_count"].tooltip = (
        "Total PNG frames to capture. More frames make motion smoother but take "
        "longer. This cannot be lower than the final configured keyframe."
    )
    DIALOG_INPUTS["fps"] = timing_inputs.addIntegerSpinnerCommandInput(
        "fps",
        "Playback FPS",
        1,
        240,
        1,
        _clamped_int(settings.get("fps"), 30, 1, 240),
    )
    DIALOG_INPUTS["fps"].tooltip = (
        "Intended playback rate for the assembled video. This changes the shown "
        "duration and metadata, not the number of captured PNGs."
    )
    duration = timing_inputs.addStringValueInput("duration", "Duration", "")
    duration.isReadOnly = True
    duration.tooltip = (
        "Estimated video length, calculated as Frame Count divided by Playback FPS."
    )
    DIALOG_INPUTS["duration"] = duration

    orbit_group = inputs.addGroupCommandInput("orbit_group", "Camera Orbit")
    orbit_group.isExpanded = True
    orbit_group.tooltip = (
        "Control how the Fusion viewport camera moves around the model during capture."
    )
    orbit_inputs = orbit_group.children
    DIALOG_INPUTS["orbit_turns"] = orbit_inputs.addFloatSpinnerCommandInput(
        "orbit_turns",
        "Rotations",
        "",
        -100.0,
        100.0,
        0.25,
        _clamped_float(settings.get("orbit_turns"), 1.0, -100.0, 100.0),
    )
    DIALOG_INPUTS["orbit_turns"].tooltip = (
        "Number of turns over the animation: 0 disables rotation, 1 is one full "
        "360° turn, 0.5 is 180°, and a negative value reverses direction."
    )
    selected_axis = settings.get("orbit_axis", [0.0, 0.0, 1.0])
    axis_options = list(AXIS_OPTIONS)
    if not any(value == selected_axis for _label, value in axis_options):
        try:
            custom_axis = [float(value) for value in selected_axis]
            if len(custom_axis) != 3:
                raise ValueError
            axis_options.append(("Custom axis from saved settings", custom_axis))
        except (TypeError, ValueError):
            selected_axis = [0.0, 0.0, 1.0]
    DIALOG_OPTION_SETS["orbit_axis"] = axis_options
    _add_dropdown(
        orbit_inputs,
        "orbit_axis",
        "Orbit Axis",
        axis_options,
        selected_axis,
        "Axis the camera rotates around. Z is typical for a model whose up "
        "direction is Z; negative axes reverse the orbit direction.",
    )
    DIALOG_OPTION_SETS["orbit_easing"] = EASING_OPTIONS
    _add_dropdown(
        orbit_inputs,
        "orbit_easing",
        "Rotation Easing",
        EASING_OPTIONS,
        settings.get("orbit_easing", "smootherstep"),
        "Controls angular speed over time. Smootherstep starts and finishes most "
        "gently; Linear keeps a constant speed.",
    )
    endpoint = orbit_inputs.addBoolValueInput(
        "orbit_include_endpoint",
        "Reach Final Angle",
        True,
        "",
        _bool_setting(settings.get("orbit_include_endpoint"), True),
    )
    endpoint.tooltip = (
        "Include the exact final orbit pose on the last frame. Enable for an "
        "eased orbit that stops completely; disable with Linear easing for a "
        "seamless repeating turntable loop."
    )
    DIALOG_INPUTS["orbit_include_endpoint"] = endpoint
    DIALOG_OPTION_SETS["orbit_target"] = TARGET_OPTIONS
    _add_dropdown(
        orbit_inputs,
        "orbit_target",
        "Orbit Center",
        TARGET_OPTIONS,
        settings.get("orbit_target", "model_center"),
        "Model Center follows the root component bounding-box center. Current "
        "Camera Target preserves the pivot point already set in the viewport.",
    )
    DIALOG_INPUTS["fit_at_start"] = orbit_inputs.addBoolValueInput(
        "fit_at_start",
        "Fit View at Start",
        True,
        "",
        _bool_setting(settings.get("fit_at_start"), True),
    )
    DIALOG_INPUTS["fit_at_start"].tooltip = (
        "Fit the complete model into view once before frame 0. It is never fitted "
        "again during capture, which prevents zoom pumping as dimensions change."
    )
    DIALOG_INPUTS["camera_padding"] = orbit_inputs.addFloatSpinnerCommandInput(
        "camera_padding",
        "Camera Padding",
        "",
        1.0,
        5.0,
        0.05,
        _clamped_float(settings.get("camera_padding"), 1.15, 1.0, 5.0),
    )
    DIALOG_INPUTS["camera_padding"].tooltip = (
        "Extra breathing room after Fit View at Start. 1.00 adds none; 1.15 adds "
        "approximately 15%. This has no effect when fitting is disabled."
    )

    safety_group = inputs.addGroupCommandInput("safety_group", "Restoration & Safety")
    safety_group.isExpanded = False
    safety_group.tooltip = (
        "Choose what Fusion restores after capture and whether model errors stop the run."
    )
    safety_inputs = safety_group.children
    DIALOG_INPUTS["restore_model_after_capture"] = safety_inputs.addBoolValueInput(
        "restore_model_after_capture",
        "Restore Parameter Expressions",
        True,
        "",
        _bool_setting(settings.get("restore_model_after_capture"), True),
    )
    DIALOG_INPUTS["restore_model_after_capture"].tooltip = (
        "Restore every animated user parameter's exact original expression after "
        "a successful capture. Restoration is always attempted after errors or cancellation."
    )
    DIALOG_INPUTS["restore_camera_after_capture"] = safety_inputs.addBoolValueInput(
        "restore_camera_after_capture",
        "Restore Camera",
        True,
        "",
        _bool_setting(settings.get("restore_camera_after_capture"), True),
    )
    DIALOG_INPUTS["restore_camera_after_capture"].tooltip = (
        "Return the viewport to its original camera position after a successful "
        "capture. Restoration is always attempted after errors or cancellation."
    )
    DIALOG_INPUTS["stop_on_timeline_error"] = safety_inputs.addBoolValueInput(
        "stop_on_timeline_error",
        "Stop on Timeline Error",
        True,
        "",
        _bool_setting(settings.get("stop_on_timeline_error"), True),
    )
    DIALOG_INPUTS["stop_on_timeline_error"].tooltip = (
        "Stop as soon as Fusion reports a timeline feature error, preventing the "
        "remaining frames from being captured from a broken model."
    )

    _update_duration_display()
    _mark_dialog_errors()


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            changed = args.input
            if changed.id == "browse_output":
                folder_dialog = UI.createFolderDialog()
                folder_dialog.title = "Choose a parent folder for animation frames"
                current_path = DIALOG_INPUTS["output_parent"].value.strip()
                if current_path:
                    current_path = _normalized_output_path(current_path)
                    if os.path.isdir(current_path):
                        folder_dialog.initialDirectory = current_path
                if folder_dialog.showDialog() == adsk.core.DialogResults.DialogOK:
                    DIALOG_INPUTS["output_parent"].value = folder_dialog.folder
            elif changed.id in ("frame_count", "fps"):
                _update_duration_display()
            _mark_dialog_errors()
        except Exception:
            UI.messageBox(
                "The animation dialog could not update:\n{}".format(
                    traceback.format_exc()
                )
            )


class CommandValidateInputsHandler(adsk.core.ValidateInputsEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            args.areInputsValid = _mark_dialog_errors()
        except Exception:
            args.areInputsValid = False


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, _args):
        try:
            if not _mark_dialog_errors():
                raise AnimationError(
                    "Enter an animation name and choose an existing writable output folder."
                )
            settings = _read_dialog_settings()
            CONFIG.update(settings)
            _save_menu_settings(settings)
            _capture_animation(
                settings["output_parent"], settings["animation_name"]
            )
        except Exception:
            UI.messageBox(
                "Failed to start capture:\n{}".format(traceback.format_exc()),
                "Parametric animation failed",
            )


class CommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, _args):
        adsk.terminate()


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            command = args.command
            on_destroy = CommandDestroyHandler()
            command.destroy.add(on_destroy)
            HANDLERS.append(on_destroy)

            _create_dialog_inputs(command, _load_menu_settings())

            on_execute = CommandExecuteHandler()
            command.execute.add(on_execute)
            HANDLERS.append(on_execute)

            on_input_changed = CommandInputChangedHandler()
            command.inputChanged.add(on_input_changed)
            HANDLERS.append(on_input_changed)

            on_validate = CommandValidateInputsHandler()
            command.validateInputs.add(on_validate)
            HANDLERS.append(on_validate)

        except Exception:
            UI.messageBox(
                "Could not create the animation settings dialog:\n{}".format(
                    traceback.format_exc()
                )
            )


def _ease(mode, t):
    t = max(0.0, min(1.0, t))
    if mode == "linear":
        return t
    if mode == "smoothstep":
        return t * t * (3.0 - 2.0 * t)
    if mode == "smootherstep":
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)
    if mode == "ease_in_out_cubic":
        if t < 0.5:
            return 4.0 * t * t * t
        return 1.0 - pow(-2.0 * t + 2.0, 3.0) / 2.0
    if mode == "hold":
        return 0.0 if t < 1.0 else 1.0
    raise AnimationError('Unknown easing mode: "{}"'.format(mode))


def _value_at_frame(keyframes, frame):
    if frame <= keyframes[0]["frame"]:
        return keyframes[0]["internal_value"]
    if frame >= keyframes[-1]["frame"]:
        return keyframes[-1]["internal_value"]

    for index in range(len(keyframes) - 1):
        left = keyframes[index]
        right = keyframes[index + 1]
        if left["frame"] <= frame <= right["frame"]:
            span = right["frame"] - left["frame"]
            t = (frame - left["frame"]) / float(span)
            amount = _ease(left.get("ease", "smoothstep"), t)
            return left["internal_value"] + (
                right["internal_value"] - left["internal_value"]
            ) * amount

    raise AnimationError("Could not evaluate keyframes at frame {}".format(frame))


def _safe_name(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "FusionDesign"


def _create_output_directory(parent, document_name):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = "ParametricAnimation_{}_{}".format(
        _safe_name(document_name), stamp
    )
    candidate = os.path.join(parent, base_name)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, "{}_{}".format(base_name, suffix))
        suffix += 1
    os.makedirs(candidate)
    return candidate


def _get_model_center(design):
    bounds = design.rootComponent.boundingBox
    if not bounds:
        raise AnimationError("The root component has no bounding box.")
    return adsk.core.Point3D.create(
        (bounds.minPoint.x + bounds.maxPoint.x) / 2.0,
        (bounds.minPoint.y + bounds.maxPoint.y) / 2.0,
        (bounds.minPoint.z + bounds.maxPoint.z) / 2.0,
    )


def _normalized_axis(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise AnimationError("orbit_axis must contain three numbers.")
    axis = adsk.core.Vector3D.create(
        float(values[0]), float(values[1]), float(values[2])
    )
    if axis.length < 1e-12:
        raise AnimationError("orbit_axis cannot be the zero vector.")
    axis.normalize()
    return axis


def _shift_camera_target(camera, target):
    offset = camera.target.vectorTo(camera.eye)
    eye = target.copy()
    eye.translateBy(offset)
    camera.target = target
    camera.eye = eye


def _pad_camera(camera, target, padding):
    padding = float(padding)
    if padding < 1.0:
        raise AnimationError("camera_padding must be at least 1.0.")
    if abs(padding - 1.0) < 1e-12:
        return

    # Fusion 2023+ exposes orthographic extents explicitly. Perspective views
    # instead get padding by moving the eye away from the target.
    if camera.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
        try:
            success, width, height = camera.getExtents()
            if success:
                if not camera.setExtents(width * padding, height * padding):
                    raise AnimationError("Could not set orthographic camera extents.")
                return
        except AttributeError:
            # Compatibility fallback for Fusion releases before October 2023.
            camera.viewExtents = camera.viewExtents * padding
            return

    offset = target.vectorTo(camera.eye)
    offset.scaleBy(padding)
    eye = target.copy()
    eye.translateBy(offset)
    camera.eye = eye


def _timeline_errors(design):
    errors = []
    timeline = design.timeline
    for index in range(timeline.count):
        try:
            item = timeline.item(index)
            if (
                item.healthState
                == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState
            ):
                message = item.errorOrWarningMessage or "Unknown timeline error"
                errors.append("Timeline item {}: {}".format(index + 1, message))
        except Exception:
            # Some timeline objects do not expose meaningful health information.
            continue
    return errors


def _compute_and_check(design, frame):
    if not design.computeAll():
        raise AnimationError("Compute All did not complete at frame {}.".format(frame))
    if CONFIG["stop_on_timeline_error"]:
        errors = _timeline_errors(design)
        if errors:
            preview = "\n".join(errors[:5])
            if len(errors) > 5:
                preview += "\n...and {} more".format(len(errors) - 5)
            raise AnimationError(
                "The model has timeline errors at frame {}:\n{}".format(
                    frame, preview
                )
            )


def _prepare_tracks(design):
    frame_count = CONFIG.get("frame_count")
    if not isinstance(frame_count, int) or frame_count < 2:
        raise AnimationError("frame_count must be an integer of at least 2.")

    tracks_config = CONFIG.get("tracks")
    if not isinstance(tracks_config, list) or not tracks_config:
        raise AnimationError("CONFIG must contain at least one parameter track.")

    units_manager = design.unitsManager
    seen_names = set()
    prepared = []

    for track_config in tracks_config:
        name = track_config.get("name", "")
        if not name or name in seen_names:
            raise AnimationError(
                'Track names must be present and unique; got "{}".'.format(name)
            )
        seen_names.add(name)

        parameter = design.userParameters.itemByName(name)
        if not parameter:
            model_parameter = design.allParameters.itemByName(name)
            if model_parameter:
                raise AnimationError(
                    '"{}" exists, but it is not a User Parameter.'.format(name)
                )
            raise AnimationError(
                'User Parameter "{}" was not found. Names are case-sensitive.'.format(
                    name
                )
            )

        # Text parameters were added to Fusion's parameter API in 2025. They
        # cannot be interpolated numerically.
        try:
            if (
                parameter.valueType
                == adsk.fusion.ParameterValueTypes.TextParameterValueType
            ):
                raise AnimationError(
                    'User Parameter "{}" is text, not numeric.'.format(name)
                )
        except AttributeError:
            pass

        raw_keyframes = track_config.get("keyframes")
        if not isinstance(raw_keyframes, list) or not raw_keyframes:
            raise AnimationError('Track "{}" has no keyframes.'.format(name))

        keyframes = []
        seen_frames = set()
        for raw in raw_keyframes:
            keyframe_frame = raw.get("frame")
            expression = raw.get("value")
            ease = raw.get("ease", "smoothstep")
            if (
                not isinstance(keyframe_frame, int)
                or keyframe_frame < 0
                or keyframe_frame >= frame_count
            ):
                raise AnimationError(
                    'Track "{}" has an invalid keyframe number: {}.'.format(
                        name, keyframe_frame
                    )
                )
            if keyframe_frame in seen_frames:
                raise AnimationError(
                    'Track "{}" has duplicate keyframe {}.'.format(
                        name, keyframe_frame
                    )
                )
            seen_frames.add(keyframe_frame)
            if not isinstance(expression, str) or not expression.strip():
                raise AnimationError(
                    'Track "{}", frame {} needs a unit-aware string value.'.format(
                        name, keyframe_frame
                    )
                )
            if ease not in (
                "linear",
                "smoothstep",
                "smootherstep",
                "ease_in_out_cubic",
                "hold",
            ):
                raise AnimationError(
                    'Track "{}" uses unknown easing "{}".'.format(name, ease)
                )

            parameter_units = parameter.unit
            if not units_manager.isValidExpression(expression, parameter_units):
                raise AnimationError(
                    '"{}" is not valid for parameter "{}" (unit: "{}").'.format(
                        expression, name, parameter_units or "unitless"
                    )
                )
            keyframes.append(
                {
                    "frame": keyframe_frame,
                    "value": expression,
                    "ease": ease,
                    "internal_value": units_manager.evaluateExpression(
                        expression, parameter_units
                    ),
                }
            )

        keyframes.sort(key=lambda item: item["frame"])
        prepared.append(
            {
                "name": name,
                "parameter": parameter,
                "unit": parameter.unit,
                "original_expression": parameter.expression,
                "keyframes": keyframes,
            }
        )

    return prepared


def _apply_frame_values(tracks, frame):
    for track in tracks:
        track["parameter"].value = _value_at_frame(track["keyframes"], frame)


def _set_orbit_camera(viewport, camera, base_eye, base_up, target, axis, angle):
    rotation = adsk.core.Matrix3D.create()
    if not rotation.setToRotation(angle, axis, target):
        raise AnimationError("Could not create the camera rotation matrix.")

    eye = base_eye.copy()
    eye.transformBy(rotation)
    up = base_up.copy()
    up.transformBy(rotation)

    camera.isFitView = False
    camera.isSmoothTransition = False
    camera.target = target
    camera.eye = eye
    camera.upVector = up
    viewport.camera = camera


def _save_viewport_image(viewport, filename):
    width = int(CONFIG["image_width"])
    height = int(CONFIG["image_height"])
    if width < 1 or height < 1:
        raise AnimationError("Image width and height must be positive integers.")

    try:
        options = adsk.core.SaveImageFileOptions.create()
        options.filename = filename
        options.width = width
        options.height = height
        options.isAntiAliased = bool(CONFIG["anti_alias"])
        success = viewport.saveAsImageFileWithOptions(options)
    except (AttributeError, TypeError):
        # Compatibility fallback for Fusion versions older than May 2022.
        success = viewport.saveAsImageFile(filename, width, height)

    if not success:
        raise AnimationError("Fusion failed to save {}".format(filename))
    if not os.path.isfile(filename):
        raise AnimationError("Fusion reported success but no file exists: {}".format(filename))


def _write_settings(output_directory, design, tracks):
    settings = {
        "document": APP.activeDocument.name,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "config": CONFIG,
        "parameters": [
            {
                "name": track["name"],
                "unit": track["unit"],
                "original_expression": track["original_expression"],
            }
            for track in tracks
        ],
        "note": (
            "Numeric values in frames.csv are Fusion database values: "
            "lengths are centimeters and angles are radians."
        ),
    }
    path = os.path.join(output_directory, "animation_settings.json")
    with open(path, "w", encoding="utf-8") as settings_file:
        json.dump(settings, settings_file, indent=2)


def _restore_model(design, tracks):
    # Restore expressions, not only numeric values, so equations and references
    # are put back exactly as they were before capture.
    for track in tracks:
        track["parameter"].expression = track["original_expression"]
    if not design.computeAll():
        raise AnimationError("The design did not recompute after restoration.")


def _capture_animation(output_parent, animation_name):
    progress = None
    original_camera = None
    tracks = []
    output_directory = None
    captured = 0
    cancelled = False
    completed = False
    model_was_modified = False
    camera_was_modified = False
    error_text = ""
    restoration_error = ""

    try:
        if not APP or not UI:
            raise AnimationError("Fusion is not available.")
        design = adsk.fusion.Design.cast(APP.activeProduct)
        if not design:
            raise AnimationError(
                "Open a design and switch to the Design workspace before running."
            )

        tracks = _prepare_tracks(design)
        axis = _normalized_axis(CONFIG["orbit_axis"])
        orbit_easing = CONFIG.get("orbit_easing", "linear")
        if orbit_easing not in (
            "linear",
            "smoothstep",
            "smootherstep",
            "ease_in_out_cubic",
        ):
            raise AnimationError(
                'orbit_easing must be "linear", "smoothstep", '
                '"smootherstep", or "ease_in_out_cubic".'
            )
        orbit_include_endpoint = CONFIG.get("orbit_include_endpoint", True)
        if not isinstance(orbit_include_endpoint, bool):
            raise AnimationError("orbit_include_endpoint must be True or False.")
        original_camera = APP.activeViewport.camera

        output_directory = _create_output_directory(output_parent, animation_name)
        _write_settings(output_directory, design, tracks)

        viewport = APP.activeViewport
        frame_count = int(CONFIG["frame_count"])
        digits = max(5, len(str(frame_count - 1)))

        progress = UI.createProgressDialog()
        progress.cancelButtonText = "Stop after this frame"
        progress.isBackgroundTranslucent = False
        progress.isCancelButtonShown = True
        progress.show(
            "Parametric viewport animation",
            "Capturing frame %v of %m (%p)",
            0,
            frame_count,
            0,
        )

        # Establish framing using the first keyframed model state.
        model_was_modified = True
        _apply_frame_values(tracks, 0)
        _compute_and_check(design, 0)
        adsk.doEvents()

        if CONFIG["fit_at_start"]:
            camera_was_modified = True
            if not viewport.fit():
                raise AnimationError("Fusion could not fit the model in the viewport.")
            viewport.refresh()
            adsk.doEvents()

        camera = viewport.camera
        target_mode = CONFIG["orbit_target"]
        if target_mode == "model_center":
            target = _get_model_center(design)
            _shift_camera_target(camera, target)
        elif target_mode == "current_camera_target":
            target = camera.target.copy()
        else:
            raise AnimationError(
                'orbit_target must be "model_center" or "current_camera_target".'
            )

        _pad_camera(camera, target, CONFIG["camera_padding"])
        camera.isFitView = False
        camera.isSmoothTransition = False
        camera_was_modified = True
        viewport.camera = camera
        viewport.refresh()
        adsk.doEvents()

        base_eye = camera.eye.copy()
        base_up = camera.upVector.copy()

        csv_path = os.path.join(output_directory, "frames.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                ["frame", "orbit_degrees"]
                + [track["name"] + "_internal" for track in tracks]
            )

            for frame in range(frame_count):
                if progress.wasCancelled:
                    cancelled = True
                    break

                _apply_frame_values(tracks, frame)
                _compute_and_check(design, frame)

                orbit_denominator = (
                    frame_count - 1 if orbit_include_endpoint else frame_count
                )
                orbit_time = frame / float(orbit_denominator)
                orbit_progress = _ease(orbit_easing, orbit_time)
                angle = (
                    2.0
                    * math.pi
                    * float(CONFIG["orbit_turns"])
                    * orbit_progress
                )
                _set_orbit_camera(
                    viewport,
                    camera,
                    base_eye,
                    base_up,
                    target,
                    axis,
                    angle,
                )

                # Recompute updates model geometry; refresh updates viewport
                # graphics; doEvents lets Fusion process both before capture.
                if not viewport.refresh():
                    raise AnimationError(
                        "Viewport refresh failed at frame {}.".format(frame)
                    )
                adsk.doEvents()

                filename = os.path.join(
                    output_directory,
                    "frame_{:0{width}d}.png".format(frame, width=digits),
                )
                _save_viewport_image(viewport, filename)

                writer.writerow(
                    [frame, math.degrees(angle)]
                    + [track["parameter"].value for track in tracks]
                )
                csv_file.flush()
                captured += 1
                progress.progressValue = captured
                adsk.doEvents()

        completed = not cancelled and captured == frame_count

    except Exception:
        error_text = traceback.format_exc()
    finally:
        try:
            if model_was_modified and tracks and (
                CONFIG["restore_model_after_capture"] or not completed
            ):
                design = adsk.fusion.Design.cast(APP.activeProduct)
                if design:
                    _restore_model(design, tracks)
            if camera_was_modified and original_camera and (
                CONFIG["restore_camera_after_capture"] or not completed
            ):
                APP.activeViewport.camera = original_camera
            if APP and APP.activeViewport:
                APP.activeViewport.refresh()
                adsk.doEvents()
        except Exception:
            restoration_error = traceback.format_exc()
        try:
            if progress:
                progress.hide()
        except Exception:
            pass

    if error_text:
        message = "Capture stopped after {} frame(s).\n\n{}".format(
            captured, error_text
        )
        if restoration_error:
            message += "\n\nRestoration also failed:\n{}".format(restoration_error)
        if output_directory:
            message += "\n\nPartial output:\n{}".format(output_directory)
        UI.messageBox(message, "Parametric animation failed")
        return

    if restoration_error:
        UI.messageBox(
            "Frames were captured, but restoring the original model/camera failed:\n\n{}"
            "\n\nOutput:\n{}".format(restoration_error, output_directory),
            "Parametric animation warning",
        )
        return

    if cancelled:
        UI.messageBox(
            "Stopped after {} frame(s). The original parameter expressions and "
            "camera were restored.\n\nPartial output:\n{}".format(
                captured, output_directory
            ),
            "Parametric animation stopped",
        )
        return

    digits = max(5, len(str(int(CONFIG["frame_count"]) - 1)))
    pattern = "frame_%0{}d.png".format(digits)
    video_filename = os.path.join(
        output_directory, _safe_name(animation_name) + ".mp4"
    )
    ffmpeg = (
        'ffmpeg -framerate {fps} -i "{pattern}" -c:v libx264 -crf 18 '
        '-pix_fmt yuv420p -movflags +faststart "{video}"'
    ).format(
        fps=CONFIG["fps"],
        pattern=os.path.join(output_directory, pattern),
        video=video_filename,
    )
    UI.messageBox(
        "Captured {} frames.\n\nOutput:\n{}\n\nVideo command:\n{}".format(
            captured, output_directory, ffmpeg
        ),
        "Parametric animation complete",
    )


def run(_context):
    try:
        if not APP or not UI:
            raise AnimationError("Fusion is not available.")
        design = adsk.fusion.Design.cast(APP.activeProduct)
        if not design:
            raise AnimationError(
                "Open a design and switch to the Design workspace before running."
            )

        command_definitions = UI.commandDefinitions
        command_definition = command_definitions.itemById(COMMAND_ID)
        if not command_definition:
            command_definition = command_definitions.addButtonDefinition(
                COMMAND_ID,
                COMMAND_NAME,
                "Configure parameter animation, camera orbit, and frame capture.",
            )

        on_command_created = CommandCreatedHandler()
        command_definition.commandCreated.add(on_command_created)
        HANDLERS.append(on_command_created)

        if not command_definition.execute():
            raise AnimationError("Fusion could not open the animation settings dialog.")
        adsk.autoTerminate(False)
    except Exception:
        if UI:
            UI.messageBox(
                "Failed to open Parametric Viewport Animation:\n{}".format(
                    traceback.format_exc()
                )
            )


def stop(_context):
    try:
        if UI:
            command_definition = UI.commandDefinitions.itemById(COMMAND_ID)
            if command_definition:
                command_definition.deleteMe()
        DIALOG_INPUTS.clear()
        DIALOG_OPTION_SETS.clear()
        HANDLERS.clear()
    except Exception:
        if UI:
            UI.messageBox(
                "Failed to clean up Parametric Viewport Animation:\n{}".format(
                    traceback.format_exc()
                )
            )
