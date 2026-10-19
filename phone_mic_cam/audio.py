"""Phone Mic: a PulseAudio pipe source fed with the phone's audio, and the
automatic on/off that follows whether any app on this computer records."""
import asyncio
import json
import os
import subprocess
import threading
import time

RATE = 48000                      # samples/sec, mono, signed 16-bit


CHUNK_MS = 10


CHUNK_BYTES = RATE * CHUNK_MS // 1000 * 2


PREBUFFER = RATE * 40 // 1000 * 2     # wait for 40 ms before playing


MAX_BUFFER = RATE * 150 // 1000 * 2   # if lag exceeds 150 ms...


TRIM_TO = RATE * 60 // 1000 * 2       # ...drop back to 60 ms


FIFO = "/tmp/phone_mic_cam.fifo"


SOURCE = "phone_mic"


MIC_LABEL = "Phone Mic"


buf = bytearray()


buf_lock = threading.Lock()


stop_event = threading.Event()


mic_clients = set()               # the subset streaming the mic


mic_wanted = True                 # an app is recording; stays True if we can't tell


mic_always_on = False             # --mic-always-on


MIC_OFF_DELAY = 3                 # seconds without a recording app before mic off


def pactl(*args, check=True):
    r = subprocess.run(["pactl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def unload_stale_modules():
    for line in pactl("list", "short", "modules", check=False).splitlines():
        if "module-pipe-source" in line and FIFO in line:
            pactl("unload-module", line.split()[0], check=False)
    if os.path.exists(FIFO):
        os.remove(FIFO)


def create_virtual_mic():
    unload_stale_modules()
    # Ask before loading: PulseAudio may switch the default to a new source
    # by itself, and then there'd be nothing to restore on exit.
    previous = pactl("get-default-source", check=False) or None
    module_id = pactl(
        "load-module", "module-pipe-source",
        f"source_name={SOURCE}", f"file={FIFO}",
        "format=s16le", f"rate={RATE}", "channels=1",
        f"source_properties='device.description=\"{MIC_LABEL}\"'",
    )
    for _ in range(50):              # wait for the FIFO to appear
        if os.path.exists(FIFO):
            break
        time.sleep(0.05)
    pactl("set-default-source", SOURCE)
    return module_id, previous


def mic_buffer_ms() -> int:
    """Audio received from the phone but not yet played into the mic: its lag."""
    with buf_lock:
        return len(buf) * 1000 // (RATE * 2)


def feed(data: bytes):
    if len(data) % 2:
        return
    with buf_lock:
        buf.extend(data)
        if len(buf) > MAX_BUFFER:            # keep latency low
            del buf[: len(buf) - TRIM_TO]


def writer_thread():
    """Write steady 10 ms chunks into the virtual mic; silence when idle."""
    fd = os.open(FIFO, os.O_RDWR)
    silence = bytes(CHUNK_BYTES)
    playing = False
    nxt = time.monotonic()
    while not stop_event.is_set():
        with buf_lock:
            if not playing and len(buf) >= PREBUFFER:
                playing = True
            if playing and len(buf) >= CHUNK_BYTES:
                chunk = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
            else:
                playing = False
                chunk = silence
        try:
            os.write(fd, chunk)
        except OSError:
            break
        nxt += CHUNK_MS / 1000
        delay = nxt - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.1:
            nxt = time.monotonic()
    os.close(fd)


async def pactl_json(*args):
    proc = await asyncio.create_subprocess_exec(
        "pactl", "-f", "json", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return json.loads(out or b"[]")


async def mic_in_use() -> bool:
    """True while any app on this computer is recording from the virtual mic."""
    sources = await pactl_json("list", "short", "sources")
    ids = {s["index"] for s in sources if s["name"] == SOURCE}
    outputs = await pactl_json("list", "source-outputs")
    return any(o["source"] in ids and not o["corked"] for o in outputs)


async def set_mic_wanted(wanted: bool, quiet=False):
    global mic_wanted
    if wanted == mic_wanted:
        return
    mic_wanted = wanted
    if wanted:
        # Audio left from the previous Live session waits in the buffer while
        # no app records (the pipe into PulseAudio is full); drop it so the
        # new recording starts with fresh audio.
        with buf_lock:
            buf.clear()
    if not quiet:
        print("An app is using the mic: phone mic on" if wanted else
              "No app is using the mic: phone mic off")
    for ws in list(mic_clients):
        try:
            await ws.send_str("on" if wanted else "off")
        except ConnectionError:
            pass                     # closing; its handler cleans up


async def watch_mic_demand():
    """Follow PulseAudio events and switch the phone mic on/off to match."""
    proc = turn_off = None

    async def turn_off_later():
        await asyncio.sleep(MIC_OFF_DELAY)       # apps often reopen the mic
        if not await mic_in_use():
            await set_mic_wanted(False)

    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE)
        await set_mic_wanted(await mic_in_use())
        while line := await proc.stdout.readline():
            if b"source-output" not in line:
                continue
            if turn_off:
                turn_off.cancel()
                turn_off = None
            if await mic_in_use():
                await set_mic_wanted(True)
            elif mic_wanted:
                turn_off = asyncio.create_task(turn_off_later())
        print("pactl subscribe exited; leaving the phone mic always on")
    except (OSError, ValueError, KeyError) as e:
        print(f"Can't watch mic usage ({e}); leaving the phone mic always on")
    finally:
        if turn_off:
            turn_off.cancel()
        if proc and proc.returncode is None:
            proc.terminate()
            await proc.wait()
    await set_mic_wanted(True, quiet=True)     # fallback already explained above


def mic_watch_hooks(enabled):
    task = None

    async def start(app):
        nonlocal task
        global mic_wanted
        if enabled:
            mic_wanted = False
            task = asyncio.create_task(watch_mic_demand())
        else:
            print("  Phone mic is always on (--mic-always-on).")

    async def stop(app):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return start, stop
