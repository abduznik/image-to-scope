# Image to Scope

Turn a picture into sound that **draws that picture on an oscilloscope**.

Image to Scope traces the outlines of an image and writes a stereo WAV file where
the left channel is X and the right channel is Y. Play it into an oscilloscope in
X-Y mode and the image appears on screen. It also renders a
`*_scope_preview.png` showing what the trace should look like.

## Features

- Browse for PNG, JPEG, WebP, SVG/SVGZ, BMP, GIF, TIFF and ICO images.
- Each image is processed straight away into a WAV and a scope preview.
- Save both to any folder with your own file names (defaults: `<image>.wav` and
  `<image>_scope_preview.png`).
- History sidebar: every processed image with its source thumbnail, scope preview
  and WAV, which you can play back, reopen or remove.
- Adjustable sample rate, points per frame, duration and edge-detection settings.

## Download

Get the latest build from the
[Releases](https://github.com/abduznik/image-to-scope/releases) page. No
installation or Python needed.

| Platform | File |
|---|---|
| Windows 10/11 (64-bit) | `ImageToScope-vX.Y.Z-windows-x64.exe` |
| Linux (x86_64) | `ImageToScope-vX.Y.Z-linux-x86_64.AppImage` |
| macOS, Apple Silicon | `ImageToScope-vX.Y.Z-macos-arm64.zip` |
| macOS, Intel | `ImageToScope-vX.Y.Z-macos-x86_64.zip` |

The builds aren't code-signed yet, so the first launch needs one extra step:

- **Windows:** if SmartScreen warns, choose **More info → Run anyway**.
- **Linux:** `chmod +x ImageToScope-*.AppImage` and run it. If it asks for FUSE,
  install `libfuse2` or run it with `--appimage-extract-and-run`. Sound preview
  uses `pw-play`, `paplay` or `aplay`, one of which most desktops already have.
- **macOS:** unzip, move **ImageToScope.app** to Applications and open it. If macOS
  refuses, use **System Settings → Privacy & Security → Open Anyway**, or run
  `xattr -dr com.apple.quarantine /Applications/ImageToScope.app`.

History is stored in `%LOCALAPPDATA%\ImageToScope` on Windows and
`~/.local/share/image-to-scope` on Linux and macOS.

## Run from source

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python main.py                 # open the app
python main.py logo.svg        # open the app with an image loaded
python main.py --convert logo.png -o out/   # convert without the app
python main.py --self-test     # quick end-to-end check (add --require-gui to insist on the window)
```

## Project layout

| File         | What it does                                                         |
|--------------|----------------------------------------------------------------------|
| `main.py`    | Entry point: starts the app, plus `--convert` and `--self-test`.     |
| `backend.py` | Image loading, tracing, WAV and preview generation, history, audio.  |
| `gui.py`     | The Tkinter app: browsing, previews, saving and the history sidebar. |
| `tests/`     | Unit tests (pytest).                                                 |
| `assets/`    | App icon (PNG, ICO, ICNS).                                           |
| `packaging/` | Linux AppImage files (`.desktop`, `AppRun`, build script).           |

## How it works

1. Load the image and flatten transparency onto white (SVGs are rendered first).
2. Canny edge detection, then find the contours (the outline strokes).
3. Simplify each contour and drop tiny specks.
4. Order the strokes nearest-neighbour first to keep jumps between them short.
5. Join the strokes into one path and resample it evenly, so the beam moves at
   constant speed.
6. Normalise to -1…1 and flip Y (image Y grows down, scope Y grows up).
7. Repeat that one frame to fill the duration and write a 24-bit stereo WAV.

The picture redraws `sample rate / points per frame` times per second (24 Hz with
the defaults). Fewer points means less flicker but less detail.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
python -m pytest         # GUI tests are skipped if no display is available
```

### CI/CD

- **CI** (`.github/workflows/ci.yml`) lints and runs the tests on Windows and Linux
  with Python 3.10-3.13, and on macOS, for every push to `main` and every pull
  request.
- **Release** (`.github/workflows/release.yml`) runs on pushes to `main`. When the
  version in `backend.py` (`__version__`) has no release yet, it runs the tests,
  builds with PyInstaller on each platform (Windows `.exe`, Linux AppImage, macOS
  `.app` for Apple Silicon and Intel), smoke-tests every build including opening
  its window, and publishes a GitHub release tagged `v<version>` with all of them
  plus `SHA256SUMS.txt`.

To ship a new version, bump `__version__` in `backend.py` and merge to `main`.
To rebuild and overwrite the current version's release, run the **Release**
workflow by hand from the Actions tab with **replace** ticked, or push to `main`
with `[replace-release]` in the commit message.

Build the AppImage locally (Linux) with:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --windowed --name ImageToScope \
  --hidden-import PIL._tkinter_finder --add-data assets/icon-256.png:assets main.py
packaging/linux/build-appimage.sh ImageToScope.AppImage
```
