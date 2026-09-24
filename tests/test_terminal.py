import base64
import random
import re
import zlib

from PIL import Image

from tisplay import cli
from tisplay.cli import fit_kitty, process_input, resize_for_ansi
from tisplay.terminal import KittyRenderer, render_blocks, render_kitty


class FakeScreen:
    monitor = {"width": 1280, "height": 800, "left": 0, "top": 0}


class FakeController:
    def __init__(self):
        self.keys = []
        self.buttons = []

    def key(self, *args):
        self.keys.append(args)

    def button(self, *args):
        self.buttons.append(args)


def decode_kitty_transmission(rendered):
    """Read the actual Kitty APC chunks and reconstruct their compressed data."""
    commands = re.findall(rb"\x1b_G([^;]*);([^\x1b]*)\x1b\\", rendered)
    assert commands
    control = dict(item.split(b"=", 1) for item in commands[0][0].split(b","))
    assert control[b"a"] == b"T"
    assert control[b"f"] == b"24"
    assert control[b"o"] == b"z"
    assert control[b"C"] == b"1"
    assert control[b"m"] == (b"1" if len(commands) > 1 else b"0")
    for continuation, _ in commands[1:]:
        assert continuation in (b"m=0", b"m=1")
    assert [chunk[0].split(b"=")[-1] for chunk in commands] == [
        *([b"1"] * (len(commands) - 1)), b"0"
    ]
    encoded = b"".join(payload for _, payload in commands)
    return control, zlib.decompress(base64.b64decode(encoded))


def test_kitty_renderer_emits_decodable_chunked_rgb_payload():
    raw = random.Random(0).randbytes(120 * 80 * 3)
    image = Image.frombytes("RGB", (120, 80), raw)
    rendered = render_kitty(image, 80, 24)
    assert rendered.startswith(b"\x1b[H")
    control, payload = decode_kitty_transmission(rendered)
    assert control[b"s"] == b"120"
    assert control[b"v"] == b"80"
    assert payload == raw


def test_kitty_stream_places_new_frame_before_retiring_previous_image():
    renderer = KittyRenderer(first_image_id=41)
    first = renderer.render(Image.new("RGB", (2, 2), "red"), 80, 24)
    second = renderer.render(Image.new("RGB", (2, 2), "blue"), 80, 24)

    assert b"a=T" in first and b"i=41" in first
    assert b"a=d" not in first
    new_frame = second.index(b"a=T")
    retire_old = second.index(b"a=d,d=i,i=41")
    assert new_frame < retire_old
    assert b"i=42" in second[new_frame:retire_old]
    assert b"i=42" in renderer.close()
    assert renderer.close() == b""


def test_ansi_renderer_uses_truecolor_half_blocks():
    image = resize_for_ansi(Image.new("RGB", (8, 8), "#123456"), 8, 4)
    rendered = render_blocks(image)
    assert "▀".encode() in rendered
    assert b"38;2;18;52;86" in rendered


def test_test_pattern_screen_generates_distinct_color_fields():
    frame = cli.TestPatternScreen(320, 240).frame()
    assert frame.size == (320, 240)
    assert {frame.getpixel(point) for point in ((20, 20), (300, 20), (20, 220), (300, 220))} == {
        (240, 32, 32), (32, 220, 64), (40, 80, 240), (240, 220, 32)
    }


def test_kitty_fit_preserves_source_aspect_and_reports_letterbox():
    frame = Image.new("RGB", (16, 9), "white")
    canvas, box = fit_kitty(frame, 80, 24, (800, 800))
    assert canvas.size == (16, 16)
    assert box == (0.0, 0.1875, 1.0, 0.5625)


def test_terminal_pixel_size_reads_pty_dimensions(monkeypatch):
    class Stdout:
        def fileno(self):
            return 1

    def fake_ioctl(_fd, _request, winsize, _mutate):
        winsize[2], winsize[3] = 1920, 1080

    monkeypatch.setattr(cli.sys, "stdout", Stdout())
    monkeypatch.setattr(cli.fcntl, "ioctl", fake_ioctl)
    assert cli.terminal_pixel_size() == (1920, 1080)


def test_input_forwards_keys_click_and_pointer_motion():
    controller = FakeController()
    data = bytearray(b"a\x1b[A\x1b[<0;10;5M\x1b[<32;12;7M\x1b[<0;12;7m")
    assert process_input(data, controller, FakeScreen(), 80, 24, "ansi", (0, 0, 1, 1))
    assert ("a", True) in controller.keys and ("up", False) in controller.keys
    assert ("left", True, 146, 153) in controller.buttons
    assert any(entry[0] == "move" for entry in controller.buttons)
    assert controller.buttons[-1][1] is False


def test_q_is_forwarded_as_a_character():
    controller = FakeController()
    assert process_input(bytearray(b"q"), controller, FakeScreen(), 80, 24)
    assert controller.keys == [("q", True), ("q", False)]
