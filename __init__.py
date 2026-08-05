"""Make the media pickers list files in subdirectories.

WHY
---
Core ComfyUI builds the LoadImage / LoadImageMask / LoadAudio / LoadVideo
dropdowns like this:

    files = [f for f in os.listdir(input_dir) if os.path.isfile(...)]
    files = folder_paths.filter_files_content_types(files, ["image"])

`os.listdir` + `isfile` is non-recursive, so nothing inside a subdirectory is
ever offered. Any media library that organizes files into subfolders (by
date, by project, by shoot, ...) looks nearly empty in the picker even though
the files are sitting right there in the input directory.

Nothing about the backend needs fixing. `LoadImage` defines VALIDATE_INPUTS,
which makes ComfyUI skip the usual "value must be one of the combo options"
check and accept any path that exists under the input directory -- verified
by executing a graph whose image value was a nested path like
"2026-08-01/shoot-3/frame_0012.png". So subfolder paths already load; they
were merely unselectable in the UI. This pack only widens the dropdown,
which is why it cannot break existing graphs: every value that was valid
before is still valid, and every value it adds was already accepted by the
backend.

CONFIGURATION (environment, read once at import)
-----------------------------------------------
COMFYUI_PICKER_RECURSIVE   "false"/"0"/"no" disables the patch entirely.
                           Default enabled -- installing this pack is the
                           opt-in.
COMFYUI_PICKER_EXCLUDE     Comma-separated top-level directory names to skip.
                           Use it for bulk machine-generated output: those
                           dirs add thousands of entries to /object_info,
                           which every browser refetches on page load.
COMFYUI_PICKER_MAX_FILES   Safety cap per picker (default 20000). A runaway
                           directory degrades the UI rather than hanging it.
                           Truncation is logged loudly because it silently
                           hides files, which is a confusing failure on its
                           own.
"""

import os
import time

import folder_paths
import nodes

_LOG = "[recursive_media_picker]"


def _say(msg: str) -> None:
    """Print a diagnostic and flush.

    Without the flush these lines sit in Python's block buffer (stdout is
    not a TTY under Docker) and never appear in `docker logs` on a
    low-output container.
    """
    print(f"{_LOG} {msg}", flush=True)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


ENABLED = _env_flag("COMFYUI_PICKER_RECURSIVE", True)
EXCLUDE = {
    d.strip().strip("/")
    for d in os.environ.get("COMFYUI_PICKER_EXCLUDE", "").split(",")
    if d.strip()
}
try:
    MAX_FILES = int(os.environ.get("COMFYUI_PICKER_MAX_FILES", "20000"))
except ValueError:
    MAX_FILES = 20000

# (node name, widget name, content types) -- widget names differ per node, and
# LoadVideo calls its input "file" rather than "video".
_TARGETS = (
    ("LoadImage", "image", ["image"]),
    ("LoadImageMask", "image", ["image"]),
    ("LoadAudio", "audio", ["audio", "video"]),
    ("LoadVideo", "file", ["video"]),
)


def _relative_media_files(root: str) -> list[str]:
    """Every file under `root`, as forward-slash paths relative to it.

    Excludes dotfiles/dotdirs and any top-level directory in EXCLUDE. Uses
    forward slashes because the value is consumed by the web UI and by
    folder_paths.get_annotated_filepath(), not by the OS directly.
    """
    found: list[str] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        rel_dir = os.path.relpath(dirpath, root)

        if rel_dir == ".":
            rel_dir = ""
            dirnames[:] = [
                d for d in dirnames if d not in EXCLUDE and not d.startswith(".")
            ]
        elif rel_dir.split(os.sep)[0] in EXCLUDE:
            dirnames[:] = []
            continue
        else:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for name in filenames:
            if name.startswith("."):
                continue
            found.append(name if not rel_dir else f"{rel_dir}/{name}".replace(os.sep, "/"))
            if len(found) >= MAX_FILES:
                truncated = True
                break
        if truncated:
            break

    if truncated:
        _say(
            f"WARNING: hit COMFYUI_PICKER_MAX_FILES={MAX_FILES} while "
            f"scanning {root}. Pickers are TRUNCATED and some files are hidden. "
            f"Raise the cap or add bulk dirs to COMFYUI_PICKER_EXCLUDE."
        )
    return found


def _listing(content_types: list[str]) -> list[str]:
    files = _relative_media_files(folder_paths.get_input_directory())
    return sorted(folder_paths.filter_files_content_types(files, content_types))


def _patch_input_types(cls, node_name: str, widget: str, content_types: list[str]) -> bool:
    """Legacy (V1) nodes: INPUT_TYPES returns a dict of widget specs."""
    original = cls.INPUT_TYPES

    def INPUT_TYPES(_cls=None, _orig=original, _widget=widget, _types=content_types):
        # Start from core's own spec so upload flags (image_upload, tooltips,
        # and any future keys) are preserved -- only the option list is replaced.
        spec = _orig()
        try:
            section = "required" if _widget in spec.get("required", {}) else "optional"
            entry = spec[section][_widget]
            options = entry[1] if len(entry) > 1 else {}
            spec[section][_widget] = (_listing(_types), options)
        except Exception as exc:  # never take a node down over a directory listing
            _say(f"ERROR listing for {node_name}: {exc!r}; using core list")
        return spec

    cls.INPUT_TYPES = classmethod(INPUT_TYPES)
    return True


def _patch_define_schema(cls, node_name: str, widget: str, content_types: list[str]) -> bool:
    """V3 nodes (LoadAudio, LoadVideo): options live on a Combo input in a Schema.

    These do expose an INPUT_TYPES shim inherited from ComfyNode, but it is
    derived from the schema, so patching the shim has no effect -- the schema
    is the source of truth and must be patched instead.
    """
    original = cls.define_schema

    def define_schema(_cls=None, _orig=original, _widget=widget, _types=content_types):
        schema = _orig()
        try:
            for inp in schema.inputs:
                if getattr(inp, "id", None) == _widget:
                    inp.options = _listing(_types)
                    break
            else:
                _say(f"WARNING: no input {_widget!r} on {node_name}; left untouched")
        except Exception as exc:
            _say(f"ERROR listing for {node_name}: {exc!r}; using core list")
        return schema

    cls.define_schema = classmethod(define_schema)
    return True


def _patch(node_name: str, widget: str, content_types: list[str]) -> bool:
    cls = nodes.NODE_CLASS_MAPPINGS.get(node_name)
    if cls is None:
        return False
    # Check define_schema FIRST: V3 nodes also inherit an INPUT_TYPES shim, and
    # patching that one would be a silent no-op.
    if hasattr(cls, "define_schema"):
        return _patch_define_schema(cls, node_name, widget, content_types)
    if hasattr(cls, "INPUT_TYPES"):
        return _patch_input_types(cls, node_name, widget, content_types)
    return False


def _install_nested_view_middleware() -> str:
    """Teach GET /view how to serve a file that lives in a subdirectory.

    Core's handler does:

        filename = os.path.basename(filename)
        file = os.path.join(output_dir, filename)

    so it throws away any directory part of `filename` and expects the
    directory to arrive in a separate `subfolder` query parameter. The
    frontend's combo widget does not do that -- it puts the whole widget
    value in `filename` -- so a nested selection renders a broken thumbnail.

    Worse than a 404: if a nested file's basename also exists at the top
    level, the stripped path resolves to that *other* file and the UI
    silently shows the WRONG file. This is not a hypothetical: it is common
    for auto-numbered output (ComfyUI_00001_.png) to collide between a
    top-level directory and a nested one.

    This middleware splits `filename` into `subfolder` + basename before the
    handler runs, which is exactly the shape core already supports. Core's
    own `commonpath` check on `subfolder` still runs afterwards, so directory
    traversal stays blocked.
    """
    try:
        import server
        from aiohttp import web
    except Exception as exc:
        return f"middleware unavailable: {exc!r}"

    @web.middleware
    async def nested_view(request, handler):
        try:
            if request.path.rsplit("/", 1)[-1] == "view" and "filename" in request.query:
                name = request.query["filename"]
                # blake3: asset hashes are resolved by a different code path and
                # must be passed through untouched.
                if "/" in name and not name.startswith("blake3:"):
                    subdir, _, base = name.rpartition("/")
                    query = dict(request.query)
                    query["filename"] = base
                    existing = query.get("subfolder", "")
                    query["subfolder"] = f"{existing}/{subdir}" if existing else subdir
                    request = request.clone(rel_url=request.rel_url.with_query(query))
        except Exception as exc:  # a preview must never break the request chain
            _say(f"ERROR rewriting /view: {exc!r}")
        return await handler(request)

    instance = getattr(server.PromptServer, "instance", None)
    if instance is None or not hasattr(instance, "app"):
        return "middleware skipped: PromptServer.instance not ready"
    try:
        instance.app.middlewares.append(nested_view)
    except Exception as exc:
        # aiohttp freezes middlewares once the app starts serving.
        return f"middleware rejected: {exc!r}"
    return "middleware installed"


if not ENABLED:
    _say("disabled via COMFYUI_PICKER_RECURSIVE")
else:
    started = time.monotonic()
    patched = [name for name, widget, types in _TARGETS if _patch(name, widget, types)]
    missing = [name for name, _, _ in _TARGETS if name not in patched]

    try:
        sample = _relative_media_files(folder_paths.get_input_directory())
        images = len(folder_paths.filter_files_content_types(sample, ["image"]))
        detail = f"{len(sample)} files ({images} images)"
    except Exception as exc:
        detail = f"scan failed: {exc!r}"
    _say(
        f"patched {patched} in {(time.monotonic() - started) * 1000:.0f}ms; "
        f"{detail}; exclude={sorted(EXCLUDE) or 'none'}; "
        f"{_install_nested_view_middleware()}"
        + (f"; NOT FOUND {missing}" if missing else "")
    )

# No new nodes -- this pack only widens existing pickers. ComfyUI expects the
# mapping to exist.
NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
