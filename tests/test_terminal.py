import base64
import zlib

from PIL import Image

from tisplay import cli
from tisplay.cli import fit_kitty, process_input, resize_for_ansi
from tisplay.terminal import render_blocks, render_kitty


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


def test_kitty_renderer_emits_graphics_protocol_and_png_payload():
    image = Image.new("RGB", (16, 12), "#ff0000")
    rendered = render_kitty(image, 80, 24)
    assert rendered.startswith(b"\x1b[H\x1b_Ga=d,d=i,i=31")
    assert b"a=T,f=24,o=z" in rendered
    encoded = rendered.split(b"m=0;", 1)[1].split(b"\x1b\\", 1)[0]
    payload = zlib.decompress(base64.b64decode(encoded))
    assert payload == bytes((255, 0, 0)) * (16 * 12)


def test_ansi_renderer_uses_truecolor_half_blocks():
    image = resize_for_ansi(Image.new("RGB", (8, 8), "#123456"), 8, 4)
    rendered = render_blocks(image)
    assert "▀".encode() in rendered
    assert b"38;2;18;52;86" in rendered


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


def test_q_requests_exit():
    assert not process_input(bytearray(b"q"), FakeController(), FakeScreen(), 80, 24)
