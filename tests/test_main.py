import pytest
import soundfile as sf

import main


def test_convert_writes_default_names(make_image, tmp_path):
    src = make_image(".png", name="logo")
    out = tmp_path / "out"
    assert main.main(["--convert", str(src), "-o", str(out), "--duration", "0.2"]) == 0
    assert sf.info(str(out / "logo.wav")).channels == 2
    assert (out / "logo_scope_preview.png").is_file()


def test_convert_reports_errors(tmp_path, capsys):
    assert main.main(["--convert", str(tmp_path / "missing.png")]) == 1
    assert "not found" in capsys.readouterr().err


def test_self_test_passes(capsys):
    pytest.importorskip("tkinter")  # the self-test also imports the GUI module
    assert main.main(["--self-test"]) == 0
    assert "self-test OK" in capsys.readouterr().out
