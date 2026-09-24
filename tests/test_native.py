import socket
import struct
import threading

from tisplay.native import NativeWaylandController, _Rfb, _cleanup_owned_runtime, _outputs


def test_wlr_randr_parses_enabled_noop_and_geometry(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""
        stdout = '''NOOP-1 "Headless output 2"
  Enabled: yes
  Modes:
    1920x1080 px (current)
  Position: 0,0
HDMI-A-1
  Enabled: no
'''

    monkeypatch.setattr("tisplay.native.subprocess.run", lambda *a, **k: Result())
    assert _outputs() == [{"name": "NOOP-1", "enabled": True, "width": 1920, "height": 1080, "left": 0, "top": 0}]


def _rfb_server(sock):
    sock.sendall(b"RFB 003.008\n")
    assert sock.recv(12) == b"RFB 003.008\n"
    sock.sendall(b"\x01\x01")
    assert sock.recv(1) == b"\x01"
    sock.sendall(struct.pack(">I", 0))
    assert sock.recv(1) == b"\x01"
    pixelformat = b"\x00" * 16
    sock.sendall(struct.pack(">HH", 1920, 1080) + pixelformat + struct.pack(">I", 0))
    assert len(sock.recv(20)) == 20  # SetPixelFormat
    assert len(sock.recv(8)) == 8  # SetEncodings
    assert len(sock.recv(10)) == 10  # bounded initial framebuffer request
    sock.sendall(b"\x00\x00\x00\x01" + struct.pack(">HHHHi", 0, 0, 1, 1, 0) + b"\x00" * 4)
    # Read events until the test closes its client side.
    try:
        while sock.recv(32):
            pass
    except OSError:
        pass


def test_rfb_handshake_and_key_event():
    client, server = socket.socketpair()
    thread = threading.Thread(target=_rfb_server, args=(server,), daemon=True)
    thread.start()
    rfb = _Rfb(client)
    rfb.key("enter", True)
    assert server.recv(8) == struct.pack(">BBHI", 4, 1, 0, 0xFF0D)
    client.close()
    server.close()
    thread.join(timeout=1)


def test_controller_keeps_button_held_during_move_and_releases_target_only():
    class Recorder:
        def __init__(self): self.events = []
        def pointer(self, mask, x, y): self.events.append((mask, x, y))

    controller = NativeWaylandController.__new__(NativeWaylandController)
    controller.rfb = Recorder()
    controller.monitor = {"left": 0, "top": 0}
    controller.button_mask = 0
    controller.button("left", True, 12, 15)
    controller.move(18, 19)
    controller.button("right", True, 18, 19)
    controller.release_button("left")
    assert [event[0] for event in controller.rfb.events] == [1, 1, 5, 4]


def test_runtime_cleanup_leaves_tree_when_unmount_fails(tmp_path, monkeypatch):
    marker = tmp_path / "user-data"
    marker.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr("tisplay.native._mounted_paths_below", lambda root: [str(tmp_path / "runtime/doc")])
    assert not _cleanup_owned_runtime(str(tmp_path))
    assert marker.read_text(encoding="utf-8") == "preserve"
