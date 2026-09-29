from __future__ import annotations

import asyncio
import ctypes
import os
import time
from dataclasses import dataclass


class InputValidationError(ValueError):
    pass


@dataclass(slots=True)
class PendingDrag:
    action: str
    x: float
    y: float
    down: bool = False


MOUSE_ACTIONS = {
    "move", "click", "down", "up", "double_click",
    "right_click", "middle_click",
}
INPUT_ACTIONS = MOUSE_ACTIONS | {"scroll", "key", "text"}


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _write_text_windows(text: str) -> None:
    import array
    import ctypes.wintypes

    wintypes = ctypes.wintypes

    class KeybdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeybdInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    keyeventf_unicode = 0x0004
    keyeventf_keyup = 0x0002
    for unit in array.array("H", text.encode("utf-16-le")):
        inputs = (Input * 2)()
        inputs[0].type = 1
        inputs[0].union.ki.wScan = unit
        inputs[0].union.ki.dwFlags = keyeventf_unicode
        inputs[1].type = 1
        inputs[1].union.ki.wScan = unit
        inputs[1].union.ki.dwFlags = keyeventf_unicode | keyeventf_keyup
        sent = ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(Input))
        if sent != 2:
            raise OSError("SendInput rejected keyboard text")
        time.sleep(0.002)


class SystemInputService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._drag: PendingDrag | None = None

    async def handle(self, payload: dict) -> dict:
        action = str(payload.get("action", "")).casefold()
        if action not in INPUT_ACTIONS:
            raise InputValidationError("unsupported screen input action")
        if action in MOUSE_ACTIONS:
            x, y = self._coordinates(payload)
            async with self._lock:
                await asyncio.to_thread(self._apply, action, x, y)
            return {"accepted": True, "action": action, "x": x, "y": y}

        async with self._lock:
            await asyncio.to_thread(self._apply_auxiliary, action, payload)
        result: dict = {"accepted": True, "action": action}
        if action == "scroll":
            result["delta"] = int(payload.get("delta", 0))
        if action == "key":
            result["key"] = str(payload.get("key", ""))
        if action == "text":
            result["length"] = len(str(payload.get("text", "")))
        return result

    def _coordinates(self, payload: dict) -> tuple[float, float]:
        width = float(payload.get("screen_width", 0))
        height = float(payload.get("screen_height", 0))
        origin_x = float(payload.get("origin_x", 0))
        origin_y = float(payload.get("origin_y", 0))
        real_width = float(payload.get("real_width", 0))
        real_height = float(payload.get("real_height", 0))
        x = float(payload.get("x", 0))
        y = float(payload.get("y", 0))
        if not (width > 0 and height > 0):
            raise InputValidationError("screen_width and screen_height are required")
        if x < 0 or y < 0 or x > width or y > height:
            raise InputValidationError("coordinates are outside the screen")
        scale_x = real_width / width if real_width > 0 else 1.0
        scale_y = real_height / height if real_height > 0 else 1.0
        if scale_x <= 0 or scale_y <= 0:
            raise InputValidationError("screen dimensions are invalid")
        real_x = origin_x + x * scale_x
        real_y = origin_y + y * scale_y
        # mss and Windows SendInput both use physical pixels; a per-monitor
        # origin already describes the selected monitor, so no extra DPI
        # conversion is valid here.
        return real_x, real_y

    def _apply(self, action: str, x: float, y: float) -> None:
        import pyautogui

        pyautogui.FAILSAFE = False
        if action == "down":
            self._mouse_down(x, y)
            self._drag = PendingDrag(action, x, y, True)
            return
        if action == "up":
            self._mouse_up(x, y)
            self._drag = None
            return
        self._mouse_move(x, y)
        if action == "click":
            time.sleep(0.015)
            self._mouse_down(x, y)
            time.sleep(0.018)
            self._mouse_up(x, y)
        elif action == "double_click":
            time.sleep(0.015)
            self._mouse_down(x, y)
            time.sleep(0.018)
            self._mouse_up(x, y)
            time.sleep(0.035)
            self._mouse_down(x, y)
            time.sleep(0.018)
            self._mouse_up(x, y)
        elif action == "right_click":
            time.sleep(0.015)
            self._mouse_down(x, y, "right")
            time.sleep(0.018)
            self._mouse_up(x, y, "right")
        elif action == "middle_click":
            time.sleep(0.015)
            self._mouse_down(x, y, "middle")
            time.sleep(0.018)
            self._mouse_up(x, y, "middle")

    @staticmethod
    def _apply_auxiliary(action: str, payload: dict) -> None:
        import pyautogui

        pyautogui.FAILSAFE = False

        if action == "scroll":
            delta = int(payload.get("delta", 0))
            if delta == 0:
                raise InputValidationError("scroll delta cannot be zero")
            pyautogui.scroll(delta)
            return
        if action == "text":
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                raise InputValidationError("text is required")
            if len(text) > 2000:
                raise InputValidationError("text is too long")
            if os.name == "nt":
                _write_text_windows(text)
                return
            pyautogui.write(text, interval=float(payload.get("interval", 0.012)))
            return
        key = str(payload.get("key", "")).strip()
        if not key or len(key) > 80:
            raise InputValidationError("key is required")
        try:
            pyautogui.hotkey(*[part.strip() for part in key.split("+") if part.strip()])
        except (ValueError, pyautogui.FailSafeException) as exc:
            raise InputValidationError(f"unsupported key: {key}") from exc

    @staticmethod
    def _mouse_move(x: float, y: float) -> None:
        import pyautogui

        pyautogui.moveTo(x, y, duration=0)

    @staticmethod
    def _mouse_down(x: float, y: float, button: str = "left") -> None:
        import pyautogui

        pyautogui.mouseDown(x=x, y=y, button=button)

    @staticmethod
    def _mouse_up(x: float, y: float, button: str = "left") -> None:
        import pyautogui

        pyautogui.mouseUp(x=x, y=y, button=button)


system_input = SystemInputService()
