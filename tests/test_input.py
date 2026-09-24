from tisplay.cli import process_input
from tisplay.capture import XTestController
from types import SimpleNamespace


class FakeScreen:
    monitor = {"width": 1280, "height": 800, "left": 0, "top": 0}


class RecordingController:
    def __init__(self):
        self.keys = []
        self.buttons = []

    def key(self, name, down):
        self.keys.append((name, down))

    def button(self, *args):
        self.buttons.append(args)


def parse(data):
    controller = RecordingController()
    keep_running = process_input(bytearray(data), controller, FakeScreen(), 80, 24)
    return keep_running, controller.keys


def test_raw_enter_carriage_return_and_line_feed_reach_controller():
    running, keys = parse(b"\r\n")
    assert running
    assert keys == [("enter", True), ("enter", False)] * 2


def test_kitty_keyboard_protocol_enter_reaches_controller():
    for sequence in (b"\x1b[13u", b"\x1b[13;1u", b"\x1b[13;1:1u", b"\x1b[57414u"):
        running, keys = parse(sequence)
        assert running
        assert keys == [("enter", True), ("enter", False)]


def test_xterm_modify_other_keys_enter_reaches_controller():
    running, keys = parse(b"\x1b[27;1;13~")
    assert running
    assert keys == [("enter", True), ("enter", False)]


def test_kitty_key_release_event_reaches_controller_as_release_only():
    running, keys = parse(b"\x1b[13;1:3u")
    assert running
    assert keys == [("enter", False)]


def test_q_is_forwarded_as_a_character():
    running, keys = parse(b"q")
    assert running
    assert keys == [("q", True), ("q", False)]


def test_ctrl_c_is_forwarded_but_ctrl_right_bracket_quits():
    running, keys = parse(b"\x03")
    assert running
    assert keys == [("ctrl", True), ("c", True), ("c", False), ("ctrl", False)]
    assert not parse(b"\x1d")[0]


def test_mouse_release_outside_letterboxed_image_is_still_forwarded():
    controller = RecordingController()
    data = bytearray(b"\x1b[<0;40;12M\x1b[<0;1;1m")
    process_input(data, controller, FakeScreen(), 80, 24, "kitty", (0.0, 0.25, 1.0, 0.5))
    assert controller.buttons[0][0] == "left"
    assert controller.buttons[0][1] is True
    assert controller.buttons[-1][0] == "left"
    assert controller.buttons[-1][1] is False


def test_xtest_wheel_buttons_are_pulsed_and_do_not_remain_held():
    events = []

    class Root:
        def warp_pointer(self, *_args):
            pass

        def get_geometry(self):
            return SimpleNamespace(x=0, y=0)

    class Display:
        def screen(self):
            return SimpleNamespace(root=Root())

        def flush(self):
            pass

    controller = XTestController.__new__(XTestController)
    controller.display = Display()
    controller.X = SimpleNamespace(ButtonPress="press", ButtonRelease="release")
    controller.xtest = SimpleNamespace(fake_input=lambda _display, event, button: events.append((event, button)))
    controller.button("wheel_up", True, 10, 20)
    controller.button("wheel_up", False, 10, 20)
    assert events == [("press", 4), ("release", 4)]
