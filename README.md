# comfyui-recursive-media-picker

Makes ComfyUI's `LoadImage`, `LoadImageMask`, `LoadAudio`, and `LoadVideo`
pickers list files in subdirectories, not just the top level of your input
folder.

## The problem

Core ComfyUI builds those dropdowns with a plain, non-recursive
`os.listdir()`. If you organize your input/media library into subfolders
(by date, by project, by shoot — anything), every file below the top level
is invisible in the UI dropdown, even though it's sitting right there in the
input directory.

It's also not just a missing-files problem. Core's file-serving handler
(`GET /view`) strips directory info from the requested filename and expects
it in a separate `subfolder` parameter, which the picker widget never
supplies. If a nested file happens to share a basename with a top-level file
(easy to hit with auto-numbered output like `ComfyUI_00001_.png`), the
stripped lookup silently resolves to the *wrong file* — no error, just the
wrong thumbnail or the wrong audio track.

## What this does

- Widens the four pickers' option lists to a recursive scan of the input
  directory, contributing paths like `2026-08-01/shoot-3/frame_0012.png`.
- Installs a small `aiohttp` middleware that splits a nested `filename`
  value into `subfolder` + basename before core's `/view` handler runs —
  fixing both the 404 and the silent-wrong-file case above.
- Does **not** change what the backend accepts. `LoadImage` (and friends)
  define `VALIDATE_INPUTS`, so ComfyUI already skips its usual "must be one
  of the combo options" check and accepts any path under the input
  directory. Nested paths already loaded before this pack — they were just
  unselectable. That's why this cannot break existing graphs: every value
  that validated before still validates, and everything this pack adds was
  already accepted by the backend.
- Registers no new nodes. It's a pure patch applied at import time.

## Compatibility

Handles both node styles present in current ComfyUI:

- Legacy (V1) nodes, where `INPUT_TYPES()` returns the combo spec directly.
- V3 nodes (`LoadAudio`, `LoadVideo`), where the combo options live on a
  `Schema` returned by `define_schema()`. V3 nodes also inherit an
  `INPUT_TYPES` shim for backward compatibility, but it's *derived from* the
  schema — patching the shim instead of the schema is a silent no-op, so
  this pack checks for `define_schema` first.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Fulton-Engineering-Services/comfyui-recursive-media-picker.git
```

Restart ComfyUI. No extra Python dependencies — only `folder_paths`,
`nodes`, `server`, and `aiohttp`, all of which ship with ComfyUI itself.

## Configuration

Read once at import time, via environment variables:

| Variable | Default | Effect |
|---|---|---|
| `COMFYUI_PICKER_RECURSIVE` | `true` | Set to `false`/`0`/`no` to disable the patch entirely and fall back to core's non-recursive behavior. |
| `COMFYUI_PICKER_EXCLUDE` | *(none)* | Comma-separated top-level directory names to skip while scanning, e.g. `COMFYUI_PICKER_EXCLUDE=sweeps,tmp`. Use this for bulk machine-generated output directories — every entry adds to `/object_info`, which every browser tab refetches on load. |
| `COMFYUI_PICKER_MAX_FILES` | `20000` | Safety cap per picker. A runaway directory degrades the UI (truncated list, loudly logged) instead of hanging it. |

## Logging

On startup you'll see a line like:

```
[recursive_media_picker] patched ['LoadImage', 'LoadImageMask', 'LoadAudio', 'LoadVideo'] in 12ms; 11683 files (9241 images); exclude=['sweeps']; middleware installed
```

If a target node isn't found (e.g. you've removed/renamed it via another
pack), it's reported as `NOT FOUND` rather than failing silently or raising.

## License

MIT — see [LICENSE](LICENSE).
