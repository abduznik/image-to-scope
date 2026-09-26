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

## Download (Windows)

Grab `ImageToScope-vX.Y.Z-windows-x64.exe` from the
[Releases](https://github.com/abduznik/image-to-scope/releases) page and run it.
No installation or Python required. Windows SmartScreen may warn because the exe
isn't code-signed yet: choose **More info → Run anyway**.

History is stored in `%LOCALAPPDATA%\ImageToScope`.

## Run from source

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python main.py                 # open the app
python main.py logo.svg        # open the app with an image loaded
python main.py --convert logo.png -o out/   # convert without the app
python main.py --self-test     # quick end-to-end check
```

## Project layout

| File         | What it does                                                         |
|--------------|----------------------------------------------------------------------|
| `main.py`    | Entry point: starts the app, plus `--convert` and `--self-test`.     |
| `backend.py` | Image loading, tracing, WAV and preview generation, history, audio.  |
| `gui.py`     | The Tkinter app: browsing, previews, saving and the history sidebar. |
| `tests/`     | Unit tests (pytest).                                                 |

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
  with Python 3.10-3.13 for every push to `main` and every pull request.
- **Release** (`.github/workflows/release.yml`) runs on pushes to `main`. When the
  version in `backend.py` (`__version__`) has no release yet, it runs the tests,
  builds a single-file Windows exe with PyInstaller, smoke-tests the exe and
  publishes a GitHub release tagged `v<version>` with the exe and its SHA-256.

To ship a new version, bump `__version__` in `backend.py` and merge to `main`.
