import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
       '<rect x="10" y="10" width="100" height="40" fill="none" '
       'stroke="black" stroke-width="4"/></svg>')


@pytest.fixture(autouse=True)
def isolated_app_home(tmp_path, monkeypatch):
    """Never touch the real user history while testing."""
    monkeypatch.setenv("IMAGE_TO_SCOPE_HOME", str(tmp_path / "app-home"))


def draw_shapes(size=(200, 160), mode="RGB", background="white"):
    img = Image.new(mode, size, background)
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, 120, 120), outline="black", width=5)
    d.rectangle((140, 30, 180, 130), outline="black", width=5)
    return img


@pytest.fixture
def shape_png(tmp_path):
    path = tmp_path / "shapes.png"
    draw_shapes().save(path)
    return path


@pytest.fixture
def make_image(tmp_path):
    """Write the test drawing in any format: make_image('.webp')."""
    def _make(ext, name="shapes"):
        path = tmp_path / f"{name}{ext}"
        if ext in (".svg", ".svgz"):
            data = SVG.encode()
            if ext == ".svgz":
                import gzip
                data = gzip.compress(data)
            path.write_bytes(data)
        elif ext in (".jpg", ".jpeg", ".bmp"):
            draw_shapes().save(path)
        else:
            draw_shapes(mode="RGBA", background=(0, 0, 0, 0)).save(path)
        return path
    return _make
