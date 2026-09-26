"""
main.py

Entry point for Image to Scope.

    python main.py                         # open the app
    python main.py logo.svg                # open the app with an image loaded
    python main.py --convert logo.png -o out/   # convert without the GUI
    python main.py --self-test             # quick end-to-end check, exit code 0/1
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import backend


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="ImageToScope",
        description="Turn an image into a WAV that draws it on an "
                    "oscilloscope in X-Y mode.")
    parser.add_argument("image", nargs="?", help="image to open in the app")
    parser.add_argument("--convert", metavar="IMAGE",
                        help="convert IMAGE without opening the app")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="folder for --convert output (default: next to the image)")
    parser.add_argument("--duration", type=float, default=None,
                        help="length of the WAV in seconds (--convert only)")
    parser.add_argument("--self-test", action="store_true",
                        help="run a quick end-to-end check and exit")
    parser.add_argument("--require-gui", action="store_true",
                        help="with --self-test: fail if the window can't be opened")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {backend.__version__}")
    return parser.parse_args(argv)


def convert(image, output_dir=None, duration=None) -> int:
    settings = backend.ScopeSettings()
    if duration is not None:
        settings.loop_seconds = duration
    image = Path(image)
    out_dir = Path(output_dir) if output_dir else image.parent
    wav_name, preview_name = backend.default_output_names(image)
    try:
        result = backend.process_image(image, out_dir / wav_name,
                                       out_dir / preview_name, settings)
    except backend.ScopeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Traced {result.n_strokes} strokes from {image.name}")
    print(f"Wrote {result.wav_path}: {result.n_samples} samples, "
          f"{result.duration:.2f}s, refresh rate {result.refresh_rate:.1f} Hz")
    print(f"Wrote {result.preview_path}")
    return 0


def self_test(require_gui: bool = False) -> int:
    """Draw a shape, run it through the pipeline and check the outputs.

    Used by the release workflow to make sure the frozen .exe actually works.
    """
    import soundfile as sf
    from PIL import Image, ImageDraw

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        img = Image.new("RGB", (256, 256), "white")
        draw = ImageDraw.Draw(img)
        draw.ellipse((40, 40, 216, 216), outline="black", width=6)
        draw.rectangle((100, 100, 156, 156), fill="black")
        src = tmp / "self_test.webp"
        img.save(src)
        svg = tmp / "self_test.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" '
                       'height="64"><circle cx="32" cy="32" r="20" fill="none" '
                       'stroke="black" stroke-width="4"/></svg>')
        try:
            for path in (src, svg):
                wav, preview = tmp / f"{path.stem}.wav", tmp / f"{path.stem}.png"
                result = backend.process_image(
                    path, wav, preview, backend.ScopeSettings(loop_seconds=0.5))
                info = sf.info(str(wav))
                assert info.channels == 2 and info.frames == result.n_samples
                assert preview.stat().st_size > 0
            history = backend.HistoryStore(tmp / "history")
            entry = history.process(src, backend.ScopeSettings(loop_seconds=0.5))
            assert history.wav_path(entry).is_file()
            gui_status = _self_test_gui(history, src, require_gui)
        except Exception as exc:
            print(f"self-test FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(f"self-test OK ({backend.APP_NAME} {backend.__version__}, GUI {gui_status})")
    return 0


def _self_test_gui(history, image, require_gui: bool) -> str:
    """Build the real (hidden) main window and show an image in it.

    Catches packaging problems that only appear once Tk and Pillow's Tk
    bridge are used. Skipped when there is no display, unless required.
    """
    import tkinter as tk

    import gui

    try:
        root = tk.Tk(className=backend.APP_SLUG)
    except tk.TclError:
        if require_gui:
            raise
        return "skipped (no display)"
    try:
        root.withdraw()
        app = gui.ScopeApp(root, history=history)
        assert app.load_image(image, auto_process=False)
        app.show_entry(history.entries[0])
        root.update()
    finally:
        root.destroy()
    return "OK"


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test(require_gui=args.require_gui)
    if args.convert:
        return convert(args.convert, args.output_dir, args.duration)

    import gui
    gui.run(image=args.image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
