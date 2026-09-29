from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import threading
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger(__name__)

DESKTOP_READ_WALLPAPER = 0x0001
DESKTOP_READ_OBJECTS = 0x0002
DESKTOP_HOOK_CTRL = 0x0004
DESKTOP_CREATE_MENU = 0x0008
DESKTOP_CREATE_WINDOW = 0x0010
DESKTOP_SWITCHTO = 0x0100
DESKTOP_INTERACTIVE_RIGHTS = (
    DESKTOP_READ_WALLPAPER
    | DESKTOP_READ_OBJECTS
    | DESKTOP_HOOK_CTRL
    | DESKTOP_CREATE_MENU
    | DESKTOP_CREATE_WINDOW
    | DESKTOP_SWITCHTO
)

_DPI_AWARENESS_LOCK = threading.Lock()
_DPI_AWARENESS_DONE = False


def _open_interactive_desktop() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    desktop = ctypes.windll.user32.OpenInputDesktop(
        0,
        False,
        DESKTOP_INTERACTIVE_RIGHTS,
    )
    return int(desktop) if desktop else None


def _enable_windows_dpi_awareness() -> None:
    global _DPI_AWARENESS_DONE
    if os.name != "nt" or _DPI_AWARENESS_DONE:
        return
    with _DPI_AWARENESS_LOCK:
        if _DPI_AWARENESS_DONE:
            return
        try:
            import ctypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            logger.debug("Windows DPI awareness is unavailable", exc_info=True)
        finally:
            _DPI_AWARENESS_DONE = True


@dataclass(slots=True)
class ScreenSubscriber:
    device_id: str
    fps: float
    max_width: int
    queue: asyncio.Queue[dict]
    last_hash: str | None = None


class ScreenMonitor:
    def __init__(
        self,
        *,
        default_fps: float = 1,
        max_fps: float = 2,
        max_width: int = 1280,
        jpeg_quality: int = 60,
        max_queue: int = 8,
    ):
        _enable_windows_dpi_awareness()
        self.default_fps = default_fps
        self.max_fps = max_fps
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.max_queue = max_queue
        self._subscribers: dict[str, ScreenSubscriber] = {}
        self._task: asyncio.Task[None] | None = None
        self._frame_seq = 0
        self._capture_origin: tuple[int, int] | None = None
        self._real_size: tuple[int, int] | None = None

    def subscribe(
        self,
        device_id: str,
        fps: float | None = None,
        max_width: int | None = None,
    ) -> ScreenSubscriber:
        safe_fps = self.default_fps if fps is None else max(0.1, min(fps, self.max_fps))
        safe_width = self._resolve_width(max_width)
        old = self._subscribers.get(device_id)
        if old is not None:
            old.fps = safe_fps
            if old.max_width != safe_width:
                old.max_width = safe_width
                old.last_hash = None
            return old
        subscriber = ScreenSubscriber(
            device_id,
            safe_fps,
            safe_width,
            asyncio.Queue(maxsize=self.max_queue),
        )
        self._subscribers[device_id] = subscriber
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="screen-monitor")
        return subscriber

    def unsubscribe(self, device_id: str) -> None:
        self._subscribers.pop(device_id, None)
        if not self._subscribers and self._task is not None:
            self._task.cancel()
            self._task = None

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._subscribers.clear()

    async def _run(self) -> None:
        while self._subscribers:
            start = asyncio.get_running_loop().time()
            subscriber = max(
                self._subscribers.values(),
                key=lambda item: (item.max_width, item.fps),
            )
            try:
                frame = await asyncio.to_thread(self._capture, subscriber)
                if frame is not None:
                    self._frame_seq += 1
                    for subscriber in list(self._subscribers.values()):
                        message = dict(frame)
                        message["seq"] = self._frame_seq
                        try:
                            subscriber.queue.put_nowait({
                                "v": 1,
                                "type": "screen.frame",
                                "payload": message,
                            })
                        except asyncio.QueueFull:
                            pass
            except Exception as exc:
                logger.exception("screen monitor capture failed")
                for subscriber in list(self._subscribers.values()):
                    try:
                        subscriber.queue.put_nowait({
                            "v": 1,
                            "type": "error",
                            "payload": {
                                "code": "screen.unavailable",
                                "message": f"screen capture is unavailable: {exc}",
                                "detail": exc.__class__.__name__,
                            },
                        })
                    except asyncio.QueueFull:
                        pass
            elapsed = asyncio.get_running_loop().time() - start
            target = 1 / max(subscriber.fps for subscriber in self._subscribers.values())
            await asyncio.sleep(max(0, target - elapsed))

    def _resolve_width(self, requested: int | float | None) -> int:
        if requested is None:
            return self.max_width
        try:
            width = int(requested)
        except (TypeError, ValueError):
            return self.max_width
        if width <= 0:
            return self.max_width
        return max(320, min(width, self.max_width))

    def _capture(self, subscriber: ScreenSubscriber) -> dict | None:
        import ctypes

        original_desktop = None
        interactive_desktop = None
        if os.name == "nt":
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            original_desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
            interactive_desktop = _open_interactive_desktop()
            if interactive_desktop:
                user32.SetThreadDesktop(interactive_desktop)
        try:
            image, origin_x, origin_y = self._capture_image()
            self._capture_origin = (origin_x, origin_y)
            self._real_size = (image.width, image.height)
            if image.width > subscriber.max_width:
                ratio = subscriber.max_width / image.width
                image = image.resize(
                    (subscriber.max_width, max(1, int(image.height * ratio))),
                    Image.Resampling.BILINEAR,
                )
            digest = hashlib.sha256(image.tobytes()).digest()
            frame_hash = digest.hex()
            if frame_hash == subscriber.last_hash:
                return None
            subscriber.last_hash = frame_hash
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=self.jpeg_quality, optimize=True)
            return {
                "encoding": "base64/jpeg",
                "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "width": image.width,
                "height": image.height,
                "hash": frame_hash,
                "origin_x": self._capture_origin[0],
                "origin_y": self._capture_origin[1],
                "real_width": self._real_size[0],
                "real_height": self._real_size[1],
                "fps": subscriber.fps,
            }
        finally:
            if os.name == "nt":
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                if original_desktop:
                    user32.SetThreadDesktop(original_desktop)
                if interactive_desktop:
                    user32.CloseDesktop(interactive_desktop)

    def _capture_image(self) -> tuple:
        primary_error: Exception | None = None
        try:
            import mss

            with mss.MSS() as scanner:
                monitor = scanner.monitors[1]
                raw = scanner.grab(monitor)
                return (
                    Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"),
                    int(monitor["left"]),
                    int(monitor["top"]),
                )
        except Exception as exc:
            primary_error = exc

        if os.name != "nt":
            raise RuntimeError("screen monitor requires mss")
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab(all_screens=True)
            if image.mode != "RGB":
                image = image.convert("RGB")
            return image, 0, 0
        except Exception as exc:
            logger.exception(
                "screen capture failed; primary=%s: %s; fallback=%s: %s",
                type(primary_error).__name__,
                primary_error,
                type(exc).__name__,
                exc,
            )
            raise RuntimeError(
                "screen capture failed in this desktop session: "
                f"{primary_error}; fallback: {exc}"
            ) from exc
