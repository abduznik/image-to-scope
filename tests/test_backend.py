import json
import os

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

import backend
from backend import HistoryStore, ScopeError, ScopeSettings

FAST = dict(loop_seconds=0.25, sample_rate=48000, points_per_frame=1000)


# ------------------------------------------------------------- loading --
@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".webp", ".bmp",
                                 ".gif", ".tiff", ".svg", ".svgz"])
def test_load_image_supported_formats(make_image, ext):
    img = backend.load_image(make_image(ext))
    assert img.mode == "RGBA"
    assert img.width > 0 and img.height > 0


def test_svg_is_rendered_at_requested_size_keeping_aspect(make_image):
    img = backend.load_image(make_image(".svg"), svg_render_size=600)
    assert img.size == (600, 300)


def test_load_image_rejects_unknown_extension(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(ScopeError, match="Unsupported"):
        backend.load_image(path)


def test_load_image_missing_file(tmp_path):
    with pytest.raises(ScopeError, match="not found"):
        backend.load_image(tmp_path / "nope.png")


def test_load_image_corrupt_file(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"definitely not a png")
    with pytest.raises(ScopeError):
        backend.load_image(path)


def test_load_image_corrupt_svg(tmp_path):
    path = tmp_path / "broken.svg"
    path.write_text("<svg")
    with pytest.raises(ScopeError):
        backend.load_image(path)


def test_is_supported_is_case_insensitive():
    assert backend.is_supported("LOGO.PNG")
    assert backend.is_supported("a.WebP")
    assert not backend.is_supported("a.pdf")


def test_file_dialog_types_cover_all_extensions():
    all_patterns = backend.file_dialog_types()[0][1].split()
    for ext in backend.SUPPORTED_EXTENSIONS:
        assert f"*{ext}" in all_patterns


def test_flatten_transparency_onto_white():
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 0))
    img.putpixel((0, 0), (0, 0, 255, 255))
    bgr = backend.flatten_to_bgr(img)
    assert bgr.shape == (4, 4, 3) and bgr.dtype == np.uint8
    assert tuple(bgr[1, 1]) == (255, 255, 255)   # transparent -> white
    assert tuple(bgr[0, 0]) == (255, 0, 0)       # opaque blue in BGR order


# ------------------------------------------------------------- tracing --
def test_trace_finds_strokes(shape_png):
    bgr = backend.flatten_to_bgr(backend.load_image(shape_png))
    strokes, w, h = backend.trace_strokes(bgr, ScopeSettings())
    assert (w, h) == (200, 160)
    assert len(strokes) >= 2
    assert all(s.ndim == 2 and s.shape[1] == 2 for s in strokes)


def test_trace_blank_image_has_no_strokes():
    bgr = np.full((50, 50, 3), 255, np.uint8)
    strokes, _, _ = backend.trace_strokes(bgr, ScopeSettings())
    assert strokes == []


def test_order_strokes_reverses_when_end_is_closer():
    a = np.array([[0.0, 0.0], [10.0, 0.0]])
    far = np.array([[100.0, 100.0], [110.0, 100.0]])
    b = np.array([[50.0, 0.0], [11.0, 0.0]])   # its end is next to a's end
    ordered = backend.order_strokes_nearest_neighbor([a, far, b])
    assert ordered[0] is a
    np.testing.assert_array_equal(ordered[1], b[::-1])
    np.testing.assert_array_equal(ordered[2], far)


def test_order_strokes_keeps_every_stroke():
    rng = np.random.default_rng(0)
    strokes = [rng.random((5, 2)) * 100 for _ in range(20)]
    ordered = backend.order_strokes_nearest_neighbor(strokes)
    assert len(ordered) == 20
    assert backend.order_strokes_nearest_neighbor([]) == []


def test_xy_signal_is_normalised_and_y_flipped():
    settings = ScopeSettings(points_per_frame=400)
    square = np.array([[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]], float)
    x, y = backend.strokes_to_xy_signal([square], 100, 100, settings)
    assert len(x) == len(y) == 400
    assert np.abs(x).max() <= settings.margin + 1e-9
    assert np.abs(y).max() <= settings.margin + 1e-9
    # First point is the image's top-left corner -> scope's upper-left.
    assert x[0] == pytest.approx(-settings.margin)
    assert y[0] == pytest.approx(settings.margin)


def test_xy_signal_zero_length_path():
    with pytest.raises(ScopeError):
        backend.strokes_to_xy_signal([np.zeros((3, 2))], 10, 10, ScopeSettings())


# --------------------------------------------------------------- output --
def test_write_wav_is_stereo_with_expected_length(tmp_path):
    settings = ScopeSettings(**FAST)
    x = np.linspace(-0.5, 0.5, settings.points_per_frame)
    y = -x
    out = tmp_path / "a.wav"
    n = backend.write_wav(x, y, out, settings)
    data, rate = sf.read(out)
    assert rate == settings.sample_rate
    assert data.shape == (n, 2)
    assert n == 12 * settings.points_per_frame  # 0.25 s * 48000 / 1000
    np.testing.assert_allclose(data[: len(x), 0], x, atol=1e-5)
    np.testing.assert_allclose(data[: len(y), 1], y, atol=1e-5)


def test_render_preview_draws_green_on_black():
    t = np.linspace(0, 2 * np.pi, 500)
    img = backend.render_preview(0.8 * np.cos(t), 0.8 * np.sin(t), size=200)
    assert img.size == (200, 200)
    arr = np.asarray(img)
    assert tuple(arr[100, 100]) == (0, 0, 0)          # centre stays black
    assert (arr[:, :, 1] > 100).any()                 # some green trace


def test_process_image_end_to_end(make_image, tmp_path):
    src = make_image(".svg")
    res = backend.process_image(src, tmp_path / "o" / "x.wav",
                                tmp_path / "o" / "x.png", ScopeSettings(**FAST))
    assert res.n_strokes >= 1
    assert sf.info(res.wav_path).channels == 2
    assert Image.open(res.preview_path).size == (750, 750)
    assert res.duration == pytest.approx(0.25)
    assert res.refresh_rate == pytest.approx(48.0)


def test_process_blank_image_raises(tmp_path):
    src = tmp_path / "blank.png"
    Image.new("RGB", (64, 64), "white").save(src)
    with pytest.raises(ScopeError, match="No outlines"):
        backend.process_image(src, tmp_path / "a.wav", tmp_path / "a.png")


# ------------------------------------------------------------- settings --
@pytest.mark.parametrize("bad", [
    dict(sample_rate=100),
    dict(points_per_frame=10),
    dict(points_per_frame=50000, sample_rate=44100),
    dict(loop_seconds=0),
    dict(margin=1.5),
    dict(canny_low=200, canny_high=100),
    dict(min_contour_perimeter=-1),
])
def test_settings_validation(bad):
    with pytest.raises(ScopeError):
        ScopeSettings(**bad).validate()


def test_settings_from_dict_ignores_unknown_keys():
    s = ScopeSettings.from_dict({"sample_rate": 48000, "bogus": 1})
    assert s.sample_rate == 48000


# ---------------------------------------------------------------- names --
def test_default_output_names():
    assert backend.default_output_names("/x/My Logo.svg") == (
        "My Logo.wav", "My Logo_scope_preview.png")


def test_ensure_extension():
    assert backend.ensure_extension("song", ".wav") == "song.wav"
    assert backend.ensure_extension("song.WAV", ".wav") == "song.WAV"


def test_invalid_filename_chars():
    assert backend.invalid_filename_chars("ok name.wav") == ""
    assert backend.invalid_filename_chars('a:b?c') == ":?"


def test_export_files_uses_custom_names(tmp_path):
    wav, png = tmp_path / "src.wav", tmp_path / "src.png"
    wav.write_bytes(b"RIFF")
    png.write_bytes(b"PNG")
    out = tmp_path / "exports" / "nested"
    w, p = backend.export_files(wav, png, out, "custom", "custom_scope_preview")
    assert w == out / "custom.wav" and w.read_bytes() == b"RIFF"
    assert p == out / "custom_scope_preview.png" and p.read_bytes() == b"PNG"
    w, p = backend.export_files(wav, png, out, "only-sound", "")
    assert p is None and w.name == "only-sound.wav"


# -------------------------------------------------------------- history --
def test_app_data_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_TO_SCOPE_HOME", str(tmp_path / "h"))
    assert backend.app_data_dir() == tmp_path / "h"


def test_history_process_and_reload(make_image, tmp_path):
    root = tmp_path / "hist"
    store = HistoryStore(root)
    src = make_image(".webp", name="logo")
    entry = store.process(src, ScopeSettings(**FAST))

    assert store.entries[0] is entry
    assert entry.image_name == "logo.webp"
    assert entry.wav == "logo.wav"
    assert entry.preview == "logo_scope_preview.png"
    for p in (store.wav_path(entry), store.preview_path(entry),
              store.thumbnail_path(entry)):
        assert p.is_file()
    assert max(Image.open(store.thumbnail_path(entry)).size) <= 256

    reloaded = HistoryStore(root)
    assert [e.id for e in reloaded.entries] == [entry.id]
    assert reloaded.entries[0].settings["sample_rate"] == 48000


def test_history_newest_first_and_pruned(make_image, tmp_path):
    store = HistoryStore(tmp_path / "hist", max_entries=2)
    src = make_image(".png")
    ids = [store.process(src, ScopeSettings(**FAST)).id for _ in range(3)]
    assert [e.id for e in store.entries] == ids[:0:-1]
    assert not (store.root / ids[0]).exists()


def test_history_remove_and_clear(make_image, tmp_path):
    store = HistoryStore(tmp_path / "hist")
    src = make_image(".png")
    a = store.process(src, ScopeSettings(**FAST))
    b = store.process(src, ScopeSettings(**FAST))
    store.remove(a.id)
    assert store.get(a.id) is None and not (store.root / a.id).exists()
    store.remove("does-not-exist")
    store.clear()
    assert store.entries == [] and not (store.root / b.id).exists()
    assert HistoryStore(store.root).entries == []


def test_history_failed_processing_leaves_no_entry(tmp_path):
    store = HistoryStore(tmp_path / "hist")
    blank = tmp_path / "blank.png"
    Image.new("RGB", (32, 32), "white").save(blank)
    with pytest.raises(ScopeError):
        store.process(blank)
    assert store.entries == []
    assert [p.name for p in store.root.iterdir()] == []


def test_history_survives_corrupt_index_and_missing_files(make_image, tmp_path):
    root = tmp_path / "hist"
    store = HistoryStore(root)
    entry = store.process(make_image(".png"), ScopeSettings(**FAST))
    store.wav_path(entry).unlink()
    assert HistoryStore(root).entries == []

    (root / HistoryStore.INDEX).write_text("{not json")
    assert HistoryStore(root).entries == []
    (root / HistoryStore.INDEX).write_text(json.dumps({"entries": [{"x": 1}]}))
    assert HistoryStore(root).entries == []


def test_audio_player_without_backend_reports_unavailable():
    player = backend.AudioPlayer()
    player._backend = None
    assert not player.available
    assert not player.is_playing()
    player.stop()
    with pytest.raises(ScopeError):
        player.play("x.wav")


def test_command_player_plays_and_stops(tmp_path, monkeypatch):
    settings = ScopeSettings(**FAST)
    wav = tmp_path / "a.wav"
    backend.write_wav(np.zeros(1000), np.zeros(1000), wav, settings)

    calls = []

    class FakeProc:
        def __init__(self, args, **kw):
            calls.append(args)
            self.done = False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(backend.subprocess, "Popen", FakeProc)
    player = backend.AudioPlayer()
    player._backend = ("command", "aplay")
    player.play(wav)
    assert calls[0][0] == "aplay"
    tmp_copy = calls[0][1]
    assert sf.info(tmp_copy).subtype == "PCM_16"
    assert player.is_playing()
    player.stop()
    assert not player.is_playing()
    assert not os.path.exists(tmp_copy)


def test_resource_path_finds_bundled_icon():
    assert backend.resource_path("assets/icon-256.png").is_file()
