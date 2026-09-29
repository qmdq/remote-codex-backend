from app.input_service import INPUT_ACTIONS, SystemInputService


def test_input_actions_include_mouse_and_keyboard():
    assert {"click", "move", "down", "up", "double_click", "right_click"} <= INPUT_ACTIONS
    assert {"scroll", "key", "text"} <= INPUT_ACTIONS


def test_click_timing_uses_short_hold():
    service = SystemInputService()
    calls: list[tuple[str, tuple[float, float], str]] = []

    def mouse(action, x, y, button="left"):
        calls.append((action, (x, y), button))

    service._mouse_move = lambda x, y: mouse("move", x, y)
    service._mouse_down = lambda x, y, button="left": mouse("down", x, y, button)
    service._mouse_up = lambda x, y, button="left": mouse("up", x, y, button)

    service._apply("right_click", 120, 80)

    assert calls == [
        ("move", (120, 80), "left"),
        ("down", (120, 80), "right"),
        ("up", (120, 80), "right"),
    ]
