from PIL import Image

from tisplay.cli import PRESETS, fit_ansi, fit_kitty, resolve_performance
from tisplay.terminal import KittyRenderer


def test_presets_resolve_targets_and_explicit_overrides():
    assert resolve_performance("balanced", None, None) == (60.0, 1600, 1, 1600, 900)
    assert resolve_performance("quality", None, None) == (60.0, None, 6, 1920, 1080)
    assert resolve_performance("fast", None, None) == (30.0, 960, 1, 1280, 800)
    assert resolve_performance("quality", 24, 1280, 1440, 900) == (24, 1280, 6, 1440, 900)
    assert set(PRESETS) == {"quality", "balanced", "fast"}


def test_kitty_renderer_skips_identical_frame_but_redraws_on_terminal_resize():
    renderer = KittyRenderer(first_image_id=7)
    frame = Image.new("RGB", (32, 20), "#234567")
    first = renderer.render(frame, 80, 24)

    assert b"a=T" in first
    assert renderer.render(frame.copy(), 80, 24) == b""
    resized_terminal_frame = renderer.render(frame, 81, 24)
    assert b"a=T" in resized_terminal_frame
    assert b"a=d,d=i,i=7" in resized_terminal_frame


def test_fit_helpers_keep_viewport_bounds_for_matching_aspects():
    frame = Image.new("RGB", (160, 96), "#234567")
    kitty_frame, kitty_box = fit_kitty(frame, 80, 24, (800, 480))
    ansi_frame, ansi_box = fit_ansi(frame, 80, 24)

    assert kitty_frame is frame
    assert kitty_box == (0.0, 0.0, 1.0, 1.0)
    assert ansi_frame.size == (80, 48)
    assert ansi_box == (0.0, 0.0, 1.0, 1.0)
