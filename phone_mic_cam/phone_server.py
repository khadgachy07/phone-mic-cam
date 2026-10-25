"""What the phone talks to over HTTPS: its page, the mic and camera
WebSockets, and error reports. Every request needs the pairing token."""
import hmac
import importlib.resources
import json
import secrets
import time

from aiohttp import web, WSCloseCode, WSMsgType

from . import audio, camera, sounds, stats

WEB = importlib.resources.files(__package__) / "web"


def load_page(name):
    """web/<name>.html with web/<name>.js inlined, served as one document."""
    page = (WEB / f"{name}.html").read_text(encoding="utf-8")
    return page.replace("{{SCRIPT}}", (WEB / f"{name}.js").read_text(encoding="utf-8"))


PAGE = load_page("phone")
TOKEN = secrets.token_urlsafe(16) # pairing code; only the QR link carries it


clients = set()                   # open phone WebSockets, closed on shutdown


cam_clients = set()               # the subset streaming the camera


def authorized(request) -> bool:
    return hmac.compare_digest(request.query.get("t", ""), TOKEN)


async def index(request):
    if not authorized(request):
        raise web.HTTPForbidden(text="This link is out of date. Scan the QR code shown on your computer.")
    return web.Response(text=PAGE, content_type="text/html")


async def ws_handler(request):
    if not authorized(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(max_msg_size=1 << 20)
    await ws.prepare(request)
    clients.add(ws)
    audio.mic_clients.add(ws)
    print(f"Phone mic connected from {request.remote}")
    try:
        await ws.send_str("on" if audio.mic_wanted else "off")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                stats.mic_meter.add(len(msg.data))
                audio.feed(msg.data)
            elif msg.type == WSMsgType.TEXT:             # a clap or snap the phone heard
                kind = sounds.parse(msg.data)
                if kind:
                    await sounds.heard(kind)
    finally:
        clients.discard(ws)
        audio.mic_clients.discard(ws)
    print("Phone mic disconnected")
    return ws


async def video_handler(request):
    if not authorized(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(max_msg_size=camera.MAX_FRAME)
    await ws.prepare(request)
    if camera.try_enable_camera() is None:
        await ws.close(code=4000, message=b"no virtual camera")
        return ws
    codec = request.query.get("codec", "jpeg")
    if codec not in camera.CODECS:
        codec = "jpeg"
    camera.cam.set_codec(codec)
    clients.add(ws)
    cam_clients.add(ws)
    print(f"Phone camera connected from {request.remote} ({'H.264' if codec == 'h264' else 'JPEG'})")
    try:
        async for msg in ws:
            if msg.type != WSMsgType.BINARY:
                continue
            if codec == "jpeg" and msg.data[:2] == b"\xff\xd8":
                stats.cam_meter.add(len(msg.data))
                camera.cam.push(msg.data)
            elif codec == "h264" and msg.data[:1] in (b"\x00", b"\x01"):    # keyframe flag
                stats.cam_meter.add(len(msg.data))
                camera.cam.push(msg.data[1:], key=msg.data[0] == 1)
    finally:
        clients.discard(ws)
        cam_clients.discard(ws)
    print("Phone camera disconnected")
    return ws


MAX_REPORT = 4096                 # bytes; reports are a few short fields


def clean(value, limit):
    """Untrusted text from the phone, made safe to print as part of one log line."""
    return "".join(c if c.isprintable() else "?" for c in str(value)[:limit])


async def report_handler(request):
    """Log why the phone's browser refused the mic or camera."""
    if not authorized(request):
        raise web.HTTPForbidden()
    if (request.content_length or 0) > MAX_REPORT:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REPORT,
                                            actual_size=request.content_length)
    try:
        r = json.loads(await request.content.read(MAX_REPORT))
    except ValueError:
        raise web.HTTPBadRequest()
    if not isinstance(r, dict):
        raise web.HTTPBadRequest()
    step = f" at {clean(r.get('step'), 12)}" if r.get("step") else ""
    print(f"{time.strftime('%H:%M:%S')} Phone {clean(r.get('what'), 20)} refused{step}: "
          f"{clean(r.get('name'), 60)}: {clean(r.get('message'), 200)} "
          f"(mic {clean(r.get('mic'), 10)}, page {clean(r.get('page'), 10)})")
    return web.Response(status=204)


async def close_clients(app):
    """Close open phone connections so Ctrl+C doesn't wait for them to time out."""
    for ws in list(clients):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
