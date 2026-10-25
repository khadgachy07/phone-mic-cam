"""The control page in this computer's browser (QR code, status, Stop), and
finding an already running copy."""
import asyncio
import html
import os
import signal
import socket
import tempfile
import time
import webbrowser

import qrcode
import qrcode.image.svg
from aiohttp import __version__ as aiohttp_version, web, WSCloseCode

from . import audio, camera, phone_server, sounds, stats

AIOHTTP_3_9 = tuple(int(p) for p in aiohttp_version.split(".")[:2]) >= (3, 9)


# URL of the running copy's control page, so a second launch can reopen it.
CONTROL_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                            "phone-mic-cam.url")


def hooks(phone_url, open_browser):
    """Serve the control page (QR code, status, Stop) to this computer's browser.

    Localhost only, plain HTTP. Every request needs the pairing token and the
    exact 127.0.0.1 Host, so other websites open in the browser can't read the
    page or press Stop (CSRF, DNS rebinding).
    """
    runner = None
    host = None                      # "127.0.0.1:<port>", known once listening
    svg = qrcode.make(phone_url, image_factory=qrcode.image.svg.SvgPathImage)
    # to_string() without arguments: qrcode 7.3 (Ubuntu 22.04) has no encoding=.
    page = (phone_server.load_page("control").replace("{{QR}}", svg.to_string().decode("utf-8"))
            .replace("{{URL}}", html.escape(phone_url)))

    @web.middleware
    async def local_only(request, handler):
        if request.host != host or not phone_server.authorized(request):
            raise web.HTTPForbidden()
        return await handler(request)

    async def show(request):
        return web.Response(text=page, content_type="text/html")

    async def status(request):
        last = sounds.last
        return web.json_response({
            "mic_connected": bool(audio.mic_clients),
            "last_sound": last and {"kind": last["kind"],
                                    "seconds_ago": round(time.time() - last["time"], 1)},
            "mic_live": audio.mic_wanted,
            "mic_always_on": audio.mic_always_on,
            "cam_connected": bool(phone_server.cam_clients),
            "camera_problem": camera.camera_problem,
            "stats": stats.snapshot(),
        })

    async def events(request):
        """Other apps on this computer (e.g. one woken by a clap) get each clap
        or snap the phone hears: {"event": "clap" or "snap", "time": <unix time>}."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        sounds.listeners.add(ws)
        try:
            async for _ in ws:
                pass                 # nothing to receive; just wait until it closes
        finally:
            sounds.listeners.discard(ws)
        return ws

    async def stop_app(request):
        print("Stop pressed on the control page")
        # Same path as Ctrl+C, after this response has been sent.
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGINT)
        return web.json_response({"stopping": True})

    async def start(app):
        nonlocal runner, host
        desk = web.Application(middlewares=[local_only])
        desk.router.add_get("/", show)
        desk.router.add_get("/status", status)
        desk.router.add_get("/events", events)
        desk.router.add_post("/stop", stop_app)
        # aiohttp 3.9 moved shutdown_timeout from the site to the runner;
        # Ubuntu 22.04 has 3.8, where the runner passes it on and breaks requests.
        quick = {"shutdown_timeout": 1}
        runner = web.AppRunner(desk, **(quick if AIOHTTP_3_9 else {}))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0,                # any free port
                          **({} if AIOHTTP_3_9 else quick)).start()
        host = f"127.0.0.1:{runner.addresses[0][1]}"
        url = f"http://{host}/?t={phone_server.TOKEN}"
        fd = os.open(CONTROL_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(url)
        print(f"  Control page on this computer: {url}\n")
        if open_browser:
            webbrowser.open(url)

    async def stop(app):
        try:
            os.remove(CONTROL_FILE)
        except FileNotFoundError:
            pass
        for ws in list(sounds.listeners):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
        if runner:
            await runner.cleanup()

    return start, stop


def running_copy_url():
    """Control page URL of an already running Phone Mic Cam, or None."""
    try:
        with open(CONTROL_FILE) as f:
            url = f.read().strip()
        port = int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except (OSError, ValueError, IndexError):
        return None                  # no file, or left over from a crash
    return url
