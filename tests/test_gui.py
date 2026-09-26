"""Smoke tests for the Tk app. Skipped when no display is available."""
import time

import pytest

tk = pytest.importorskip("tkinter")

import backend  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    import gui
    # Dialogs would block the test run; fail loudly instead.
    for name in ("showerror", "showinfo"):
        monkeypatch.setattr(gui.messagebox, name,
                            lambda *a, **k: pytest.fail(f"dialog: {a}"))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    a = gui.ScopeApp(root, history=backend.HistoryStore(tmp_path / "hist"))
    a.vars["loop_seconds"].set("0.5")
    yield a
    root.destroy()


def wait_idle(app, timeout=30):
    end = time.time() + timeout
    while app.busy:
        app.root.update()
        assert time.time() < end, "processing timed out"
        time.sleep(0.02)
    app.root.update()


def test_browse_process_history_and_save(app, make_image, tmp_path):
    src = make_image(".svg", name="logo")
    assert app.load_image(src)
    wait_idle(app)

    assert app.entry is not None
    assert len(app.history.entries) == 1
    assert app.wav_name_var.get() == "logo.wav"
    assert app.preview_name_var.get() == "logo_scope_preview.png"
    assert str(app.save_btn.cget("state")) == "normal"
    assert app.entry.id in app._card_widgets

    out = tmp_path / "saved"
    app.folder_var.set(str(out))
    app.wav_name_var.set("my sound")
    app.preview_name_var.set("my preview")
    app.save_files()
    assert (out / "my sound.wav").is_file()
    assert (out / "my preview.png").is_file()


def test_open_and_remove_history_entry(app, make_image):
    app.load_image(make_image(".png"))
    wait_idle(app)
    entry = app.entry
    app.reset_settings()
    app.show_entry(entry)
    assert app.vars["loop_seconds"].get() == "0.5"
    app.remove_entry(entry.id)
    assert app.history.entries == [] and app.entry is None
    assert str(app.save_btn.cget("state")) == "disabled"


def test_invalid_settings_block_processing(app, make_image, monkeypatch):
    import gui
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    app.load_image(make_image(".png"), auto_process=False)
    app.vars["canny_low"].set("abc")
    app.start_processing()
    assert not app.busy and errors
