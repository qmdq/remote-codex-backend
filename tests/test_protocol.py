import asyncio

from app.protocol.messages import parse_envelope, response
from app.input_service import SystemInputService
from app.monitors.screen import ScreenMonitor
from PIL import Image


def test_parse_and_serialize_envelope():
    envelope = parse_envelope(
        '{"v":1,"id":7,"type":"ping","payload":{"x":1}}'
    )
    assert envelope.id == "7"
    assert envelope.type == "ping"
    assert response("pong", {}, request_id=envelope.id).to_dict() == {
        "v": 1,
        "type": "pong",
        "id": "7",
        "payload": {},
    }


def test_screen_input_scales_mobile_coordinates_to_real_screen():
    service = SystemInputService()
    assert service._coordinates({
        "x": 100,
        "y": 60,
        "screen_width": 400,
        "screen_height": 200,
        "real_width": 1600,
        "real_height": 800,
        "origin_x": 10,
        "origin_y": 20,
    }) == (410.0, 260.0)


def test_screen_monitor_limits_resolution():
    monitor = ScreenMonitor(max_fps=10, max_width=1280)
    assert monitor._resolve_width(720) == 720
    assert monitor._resolve_width(99999) == 1280
    assert monitor._resolve_width(0) == 1280
    assert monitor._resolve_width(100) == 320


def test_screen_monitor_resends_first_frame_after_resubscribe():
    async def run():
        monitor = ScreenMonitor(max_fps=10, max_width=1280)
        subscriber = monitor.subscribe("device", fps=1, max_width=720)
        monitor._capture_image = lambda: (
            Image.new("RGB", (1280, 720), "#123456"),
            0,
            0,
        )

        first = monitor._capture(subscriber)
        second = monitor._capture(subscriber)

        assert first is not None
        assert second is None
        monitor.unsubscribe("device")
        await monitor.close()
        resubscribed = monitor.subscribe("device", fps=1, max_width=720)
        assert monitor._capture(resubscribed) is not None
        await monitor.close()

    asyncio.run(run())
