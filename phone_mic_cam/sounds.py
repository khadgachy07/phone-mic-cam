"""Claps and finger snaps heard by the phone, passed on to other apps on this
computer (for example as a wake sound) through the control page's /events."""
import json
import time

KINDS = ("clap", "snap")


MAX_MESSAGE = 64                  # bytes; the phone sends {"sound": "clap"}


MIN_GAP = 0.05                    # seconds; the phone needs ~0.1 s per sound


listeners = set()                 # /events WebSockets of other apps


last = None                       # the most recent: {"kind": ..., "time": ...}


_last_at = 0.0                    # monotonic time of the most recent


def parse(text):
    """The kind of sound in a message from the phone, or None if it isn't one."""
    if len(text) > MAX_MESSAGE:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    kind = message.get("sound") if isinstance(message, dict) else None
    return kind if kind in KINDS else None


async def heard(kind):
    """Log it and send it to every listening app as {"event": kind, "time": ...}."""
    global last, _last_at
    if time.monotonic() - _last_at < MIN_GAP:
        return                       # faster than the phone can detect: ignore
    _last_at = time.monotonic()
    last = {"kind": kind, "time": time.time()}
    print(f"{time.strftime('%H:%M:%S')} {kind.capitalize()} heard")
    event = json.dumps({"event": kind, "time": round(last["time"], 3)})
    for ws in list(listeners):
        try:
            await ws.send_str(event)
        except ConnectionError:
            pass                     # closing; its handler removes it
