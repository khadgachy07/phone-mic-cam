"""Live numbers for the control page: message and byte rates from the phone."""
import asyncio
import time

from . import audio, camera

class Meter:
    """Counts messages and bytes from the phone; tick() turns them into rates."""

    def __init__(self):
        self.count = self.bytes = 0
        self.per_sec = self.bytes_per_sec = 0.0
        self._last = (0, 0, time.monotonic())

    def add(self, nbytes):
        self.count += 1
        self.bytes += nbytes

    def tick(self):
        count, nbytes, then = self._last
        now = time.monotonic()
        elapsed = (now - then) or 1e-9
        self.per_sec = (self.count - count) / elapsed
        self.bytes_per_sec = (self.bytes - nbytes) / elapsed
        self._last = (self.count, self.bytes, now)


mic_meter = Meter()


cam_meter = Meter()


def snapshot():
    return {
        "cam_fps": round(cam_meter.per_sec, 1),
        "cam_kb_per_sec": round(cam_meter.bytes_per_sec / 1000),
        "cam_skipped": camera.cam.skipped if camera.cam else 0,
        "mic_kb_per_sec": round(mic_meter.bytes_per_sec / 1000),
        "mic_buffer_ms": audio.mic_buffer_ms(),
    }


def hooks():
    task = None

    async def tick_every_second():
        while True:
            await asyncio.sleep(1)
            mic_meter.tick()
            cam_meter.tick()
            camera.try_enable_camera()      # so the control page notices a newly loaded driver

    async def start(app):
        nonlocal task
        task = asyncio.create_task(tick_every_second())

    async def stop(app):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return start, stop
