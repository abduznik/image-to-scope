"""
backend.py

Everything that turns an image into an oscilloscope X-Y signal, with no GUI
code in it. The GUI (gui.py) and the command line (main.py) both call into
this module.

Pipeline:
  1. Load the image (PNG, JPEG, WebP, BMP, GIF, TIFF, ICO or SVG) and
     flatten transparency onto white.
  2. Canny edge detection -> find contours (the outline strokes).
  3. Simplify each contour and drop tiny noise specks.
  4. Greedy nearest-neighbour ordering of strokes, to minimise pen-up
     jump distance between them.
  5. Concatenate strokes into one path, resample to uniform arc-length
     spacing (constant drawing speed).
  6. Normalise to -1..1, flip Y (image Y grows down, scope Y grows up).
  7. Tile one "frame" of the path to fill the requested duration and write
     it as a stereo WAV (left channel = X, right channel = Y).
  8. Render a preview PNG of what the trace should look like on a scope.

This module also holds the processing history (persisted as JSON in the
user's app-data folder) and a small WAV player used for previews.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageSequence

__version__ = "0.0.1"
APP_NAME = "Image to Scope"
APP_SLUG = "ImageToScope"

RASTER_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".bmp", ".dib",
    ".gif", ".tif", ".tiff", ".ico",
)
VECTOR_EXTENSIONS = (".svg", ".svgz")
SUPPORTED_EXTENSIONS = RASTER_EXTENSIONS + VECTOR_EXTENSIONS

PREVIEW_SUFFIX = "_scope_preview"
TRACE_COLOR = (0x39, 0xFF, 0x6A)


class ScopeError(Exception):
    """A user-facing error (bad input file, nothing to trace, ...)."""


# ---------------------------------------------------------------- config --
@dataclass
class ScopeSettings:
    sample_rate: int = 96000
    points_per_frame: int = 4000     # points drawn per redraw of the image
    loop_seconds: float = 6.0        # total length of the output file
    margin: float = 0.88             # keep drawing inside +-margin of full scale
    canny_low: int = 40
    canny_high: int = 120
    min_contour_perimeter: float = 25.0  # drop noise specks smaller than this
    svg_render_size: int = 1024      # longest side when rasterising an SVG

    def validate(self) -> None:
        if self.sample_rate < 8000:
            raise ScopeError("Sample rate must be at least 8000 Hz.")
        if self.points_per_frame < 100:
            raise ScopeError("Points per frame must be at least 100.")
        if self.points_per_frame > self.sample_rate:
            raise ScopeError("Points per frame cannot exceed the sample rate.")
        if not 0 < self.loop_seconds <= 600:
            raise ScopeError("Duration must be between 0 and 600 seconds.")
        if not 0 < self.margin <= 1:
            raise ScopeError("Margin must be between 0 and 1.")
        if not 0 <= self.canny_low < self.canny_high:
            raise ScopeError("Canny low threshold must be below the high threshold.")
        if self.min_contour_perimeter < 0:
            raise ScopeError("Minimum stroke length cannot be negative.")
        if self.svg_render_size < 64:
            raise ScopeError("SVG render size must be at least 64 px.")

    @property
    def refresh_rate(self) -> float:
        return self.sample_rate / self.points_per_frame

    @classmethod
    def from_dict(cls, data: dict) -> "ScopeSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ---------------------------------------------------------- image loading --
def is_supported(path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def file_dialog_types():
    """File-type filters for tkinter's askopenfilename."""
    patterns = " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
    return [
        ("All supported images", patterns),
        ("PNG", "*.png"),
        ("JPEG", "*.jpg *.jpeg *.jpe *.jfif"),
        ("WebP", "*.webp"),
        ("SVG", "*.svg *.svgz"),
        ("BMP", "*.bmp *.dib"),
        ("GIF", "*.gif"),
        ("TIFF", "*.tif *.tiff"),
        ("Icon", "*.ico"),
        ("All files", "*.*"),
    ]


def _render_svg(path: Path, size: int) -> Image.Image:
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise ScopeError("SVG support needs the 'resvg_py' package.") from exc

    try:
        # Render once at native size to learn the aspect ratio, then again
        # with the longest side scaled to `size` so thin lines survive.
        native = Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_path=str(path))))
        w, h = native.size
        if max(w, h) == 0:
            raise ScopeError(f"{path.name} has no drawable area.")
        if w >= h:
            png = resvg_py.svg_to_bytes(svg_path=str(path), width=size)
        else:
            png = resvg_py.svg_to_bytes(svg_path=str(path), height=size)
    except ValueError as exc:
        raise ScopeError(f"Could not render SVG {path.name}: {exc}") from exc
    img = Image.open(io.BytesIO(png))
    img.load()
    return img.convert("RGBA")


def load_image(path, svg_render_size: int = 1024) -> Image.Image:
    """Load any supported image as an RGBA PIL image."""
    path = Path(path)
    if not path.is_file():
        raise ScopeError(f"File not found: {path}")
    if not is_supported(path):
        raise ScopeError(
            f"Unsupported file type '{path.suffix}'. Supported: "
            + ", ".join(SUPPORTED_EXTENSIONS)
        )
    if path.suffix.lower() in VECTOR_EXTENSIONS:
        return _render_svg(path, svg_render_size)
    try:
        with Image.open(path) as img:
            # Animated GIF/WebP: use the first frame.
            frame = next(ImageSequence.Iterator(img))
            return frame.convert("RGBA")
    except (OSError, SyntaxError, ValueError) as exc:
        raise ScopeError(f"Could not read image {path.name}: {exc}") from exc


def flatten_to_bgr(img: Image.Image) -> np.ndarray:
    """Flatten alpha onto white so transparent backgrounds don't confuse edges."""
    rgba = np.asarray(img.convert("RGBA"), dtype=np.float32)
    rgb = rgba[:, :, :3]
    alpha = rgba[:, :, 3:4] / 255.0
    flat = rgb * alpha + 255.0 * (1.0 - alpha)
    return np.ascontiguousarray(flat[:, :, ::-1]).astype(np.uint8)


def make_thumbnail(img: Image.Image, size: int = 256) -> Image.Image:
    """A copy of `img` shrunk to fit a size x size box, alpha kept."""
    thumb = img.convert("RGBA").copy()
    thumb.thumbnail((size, size), Image.LANCZOS)
    return thumb


# ---------------------------------------------------------- image tracing --
def trace_strokes(bgr: np.ndarray, settings: ScopeSettings):
    """Return (strokes, width, height); each stroke is an (N, 2) float array."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blurred, settings.canny_low, settings.canny_high)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    strokes = []
    for c in contours:
        peri = cv2.arcLength(c, True)
        if peri < settings.min_contour_perimeter:
            continue
        approx = cv2.approxPolyDP(c, 0.0015 * peri, True)
        pts = approx.reshape(-1, 2).astype(float)
        if len(pts) >= 2:
            strokes.append(pts)

    return strokes, bgr.shape[1], bgr.shape[0]


def order_strokes_nearest_neighbor(strokes):
    """Greedily chain strokes end-to-end to minimise pen-up jump distance."""
    if not strokes:
        return []
    remaining = list(strokes)
    ordered = [remaining.pop(0)]
    while remaining:
        last_pt = ordered[-1][-1]
        starts = np.array([s[0] for s in remaining])
        ends = np.array([s[-1] for s in remaining])
        d_start = np.hypot(*(starts - last_pt).T)
        d_end = np.hypot(*(ends - last_pt).T)
        i_start, i_end = int(np.argmin(d_start)), int(np.argmin(d_end))
        if d_end[i_end] < d_start[i_start]:
            ordered.append(remaining.pop(i_end)[::-1])
        else:
            ordered.append(remaining.pop(i_start))
    return ordered


# ----------------------------------------------------------- audio synth --
def strokes_to_xy_signal(strokes, width, height, settings: ScopeSettings):
    """Concatenate strokes, resample to uniform arc length, normalise."""
    if not strokes:
        raise ScopeError("No strokes to draw.")
    path = np.concatenate(strokes, axis=0)

    deltas = np.diff(path, axis=0)
    seglen = np.hypot(deltas[:, 0], deltas[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(seglen)])
    total_len = cum[-1]
    if total_len == 0:
        raise ScopeError("The traced path has zero length.")

    target_s = np.linspace(0, total_len, settings.points_per_frame, endpoint=False)
    x = np.interp(target_s, cum, path[:, 0])
    y = np.interp(target_s, cum, path[:, 1])

    cx, cy = width / 2.0, height / 2.0
    scale = settings.margin / (max(width, height) / 2.0)
    x_norm = (x - cx) * scale
    y_norm = -(y - cy) * scale  # flip Y: image-down vs scope-up
    return x_norm, y_norm


def write_wav(x_norm, y_norm, out_path, settings: ScopeSettings) -> int:
    """Tile one frame to fill the duration; returns the number of samples."""
    n_frames = max(1, int(round(settings.loop_seconds * settings.sample_rate
                                / len(x_norm))))
    x_full = np.tile(x_norm, n_frames)
    y_full = np.tile(y_norm, n_frames)
    stereo = np.stack([x_full, y_full], axis=1).astype(np.float32)
    sf.write(str(out_path), stereo, settings.sample_rate, subtype="PCM_24")
    return len(x_full)


def render_preview(x_norm, y_norm, size: int = 750) -> Image.Image:
    """Draw the X-Y trace like a scope screen: green on black."""
    ss = 2  # supersample for smooth lines
    canvas = size * ss
    img = Image.new("RGB", (canvas, canvas), "black")
    draw = ImageDraw.Draw(img)
    half = canvas / 2.0
    scale = half / 1.05
    pts = list(zip((np.asarray(x_norm) * scale + half).tolist(),
                   (half - np.asarray(y_norm) * scale).tolist(), strict=True))
    if pts:
        # Close the loop: the scope jumps from the last point back to the first.
        draw.line(pts + [pts[0]], fill=TRACE_COLOR, width=ss, joint="curve")
    return img.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------- process --
@dataclass
class ScopeResult:
    image_path: str
    wav_path: str
    preview_path: str
    n_strokes: int
    n_samples: int
    sample_rate: int
    refresh_rate: float

    @property
    def duration(self) -> float:
        return self.n_samples / self.sample_rate


def default_output_names(image_path):
    """(wav_name, preview_name) derived from the image file name."""
    stem = Path(image_path).stem or "image"
    return f"{stem}.wav", f"{stem}{PREVIEW_SUFFIX}.png"


def process_image(image_path, out_wav, out_preview,
                  settings: ScopeSettings | None = None,
                  image: Image.Image | None = None) -> ScopeResult:
    """Run the full pipeline: image in, WAV + preview PNG out."""
    settings = settings or ScopeSettings()
    settings.validate()
    if image is None:
        image = load_image(image_path, settings.svg_render_size)

    strokes, width, height = trace_strokes(flatten_to_bgr(image), settings)
    if not strokes:
        raise ScopeError(
            "No outlines were found in this image. Try an image with more "
            "contrast, or lower the edge thresholds / minimum stroke length."
        )
    ordered = order_strokes_nearest_neighbor(strokes)
    x_norm, y_norm = strokes_to_xy_signal(ordered, width, height, settings)

    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    Path(out_preview).parent.mkdir(parents=True, exist_ok=True)
    n_samples = write_wav(x_norm, y_norm, out_wav, settings)
    render_preview(x_norm, y_norm).save(out_preview)

    return ScopeResult(
        image_path=str(image_path),
        wav_path=str(out_wav),
        preview_path=str(out_preview),
        n_strokes=len(strokes),
        n_samples=n_samples,
        sample_rate=settings.sample_rate,
        refresh_rate=settings.refresh_rate,
    )


def ensure_extension(name: str, ext: str) -> str:
    name = name.strip()
    return name if name.lower().endswith(ext) else name + ext


def export_files(wav_src, preview_src, out_dir, wav_name, preview_name):
    """Copy a processed WAV + preview to `out_dir` under custom names.

    Returns (wav_dest, preview_dest). Either name may be empty to skip it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_dest = preview_dest = None
    if wav_name and wav_name.strip():
        wav_dest = out_dir / ensure_extension(wav_name, ".wav")
        shutil.copyfile(wav_src, wav_dest)
    if preview_name and preview_name.strip():
        preview_dest = out_dir / ensure_extension(preview_name, ".png")
        shutil.copyfile(preview_src, preview_dest)
    return wav_dest, preview_dest


def invalid_filename_chars(name: str) -> str:
    """Characters in `name` that Windows does not allow in file names."""
    bad = '<>:"/\\|?*'
    return "".join(sorted({ch for ch in name if ch in bad or ord(ch) < 32}))


# --------------------------------------------------------------- history --
def resource_path(relative: str) -> Path:
    """Path to a bundled file (works from source and from a PyInstaller build)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_data_dir() -> Path:
    override = os.environ.get("IMAGE_TO_SCOPE_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_SLUG
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "image-to-scope"


@dataclass
class HistoryEntry:
    id: str
    created: str
    image_name: str
    image_path: str
    thumbnail: str        # file name inside the entry folder
    wav: str
    preview: str
    n_strokes: int = 0
    duration: float = 0.0
    sample_rate: int = 0
    settings: dict = field(default_factory=dict)

    def folder(self, root: Path) -> Path:
        return root / self.id

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class HistoryStore:
    """Processed results kept in the app-data folder, newest first.

    Each entry gets its own folder holding a thumbnail of the source image,
    the WAV and the preview PNG, so the history keeps working even if the
    original image is moved or deleted.
    """

    INDEX = "history.json"

    def __init__(self, root: Path | None = None, max_entries: int = 50):
        self.root = Path(root) if root else app_data_dir() / "history"
        self.max_entries = max_entries
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries: list[HistoryEntry] = []
        self.load()

    # -- persistence
    def load(self) -> None:
        index = self.root / self.INDEX
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            entries = [HistoryEntry.from_dict(d) for d in data.get("entries", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            entries = []
        # Drop entries whose files have vanished.
        self.entries = [e for e in entries
                        if (e.folder(self.root) / e.wav).is_file()]

    def save(self) -> None:
        index = self.root / self.INDEX
        tmp = index.with_suffix(".tmp")
        payload = {"version": 1, "entries": [asdict(e) for e in self.entries]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, index)

    # -- paths
    def path(self, entry: HistoryEntry, name: str) -> Path:
        return entry.folder(self.root) / name

    def wav_path(self, entry: HistoryEntry) -> Path:
        return self.path(entry, entry.wav)

    def preview_path(self, entry: HistoryEntry) -> Path:
        return self.path(entry, entry.preview)

    def thumbnail_path(self, entry: HistoryEntry) -> Path:
        return self.path(entry, entry.thumbnail)

    # -- operations
    def get(self, entry_id: str) -> HistoryEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def process(self, image_path, settings: ScopeSettings | None = None,
                image: Image.Image | None = None) -> HistoryEntry:
        """Process `image_path` into a new history entry and return it."""
        settings = settings or ScopeSettings()
        settings.validate()
        if image is None:
            image = load_image(image_path, settings.svg_render_size)

        entry_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        folder = self.root / entry_id
        folder.mkdir(parents=True, exist_ok=True)
        wav_name, preview_name = default_output_names(image_path)
        try:
            result = process_image(image_path, folder / wav_name,
                                   folder / preview_name, settings, image=image)
            make_thumbnail(image).save(folder / "source.png")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        entry = HistoryEntry(
            id=entry_id,
            created=datetime.now().isoformat(timespec="seconds"),
            image_name=Path(image_path).name,
            image_path=str(Path(image_path).resolve()),
            thumbnail="source.png",
            wav=wav_name,
            preview=preview_name,
            n_strokes=result.n_strokes,
            duration=result.duration,
            sample_rate=result.sample_rate,
            settings=asdict(settings),
        )
        self.entries.insert(0, entry)
        for old in self.entries[self.max_entries:]:
            shutil.rmtree(old.folder(self.root), ignore_errors=True)
        del self.entries[self.max_entries:]
        self.save()
        return entry

    def remove(self, entry_id: str) -> None:
        entry = self.get(entry_id)
        if entry is None:
            return
        self.entries.remove(entry)
        shutil.rmtree(entry.folder(self.root), ignore_errors=True)
        self.save()

    def clear(self) -> None:
        for entry in self.entries:
            shutil.rmtree(entry.folder(self.root), ignore_errors=True)
        self.entries = []
        self.save()


# ----------------------------------------------------------------- audio --
class AudioPlayer:
    """Plays one WAV at a time, in the background.

    Uses `sounddevice` when available (volume control, works everywhere
    PortAudio does). Otherwise falls back to `winsound` on Windows, or to a
    command-line player (pw-play, paplay, aplay, afplay) on Linux and macOS.
    """

    COMMAND_PLAYERS = ("pw-play", "paplay", "aplay", "afplay")

    def __init__(self, volume: float = 0.4):
        self.volume = volume
        self.current: str | None = None
        self._lock = threading.Lock()
        self._backend = None
        self._proc = None
        self._tmp: str | None = None
        try:
            import sounddevice
            sounddevice.query_devices(kind="output")
            self._backend = ("sounddevice", sounddevice)
        except Exception:
            if sys.platform == "win32":
                import winsound
                self._backend = ("winsound", winsound)
            else:
                cmd = next((c for c in self.COMMAND_PLAYERS if shutil.which(c)), None)
                if cmd:
                    self._backend = ("command", cmd)

    @property
    def available(self) -> bool:
        return self._backend is not None

    def play(self, wav_path) -> None:
        if not self.available:
            raise ScopeError("No audio output is available on this system.")
        self.stop()
        name, mod = self._backend
        with self._lock:
            if name == "sounddevice":
                data, rate = sf.read(str(wav_path), dtype="float32")
                mod.play(data * self.volume, rate)
            elif name == "winsound":
                mod.PlaySound(str(wav_path),
                              mod.SND_FILENAME | mod.SND_ASYNC | mod.SND_NODEFAULT)
            else:
                # Play a quieter 16-bit copy: the full-scale X-Y signal is harsh.
                data, rate = sf.read(str(wav_path), dtype="float32")
                fd, self._tmp = tempfile.mkstemp(suffix=".wav", prefix="scope-play-")
                os.close(fd)
                sf.write(self._tmp, data * self.volume, rate, subtype="PCM_16")
                self._proc = subprocess.Popen(
                    [mod, self._tmp], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.current = str(wav_path)

    def stop(self) -> None:
        if not self.available:
            return
        name, mod = self._backend
        with self._lock:
            try:
                if name == "sounddevice":
                    mod.stop()
                elif name == "winsound":
                    mod.PlaySound(None, 0)
                else:
                    if self._proc is not None and self._proc.poll() is None:
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self._proc.kill()
                    self._proc = None
                    if self._tmp:
                        try:
                            os.remove(self._tmp)
                        except OSError:
                            pass
                        self._tmp = None
            finally:
                self.current = None

    def is_playing(self) -> bool:
        if not self.available or self.current is None:
            return False
        name, mod = self._backend
        if name == "sounddevice":
            try:
                return mod.get_stream().active
            except RuntimeError:
                return False
        if name == "command":
            return self._proc is not None and self._proc.poll() is None
        return True  # winsound gives no way to ask; assume until stopped
