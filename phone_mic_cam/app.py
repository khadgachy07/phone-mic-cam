"""Startup and shutdown: command-line options, the certificate, the log file,
and wiring the parts together."""
import argparse
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import webbrowser

import qrcode
from aiohttp import web

from . import __version__, audio, camera, control_page, phone_server, stats

PORT = 8443


# Per-user files: the installed script lives in read-only /usr/share.
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "phone-mic-cam")


CERT = os.path.join(DATA_DIR, "cert.pem")


KEY = os.path.join(DATA_DIR, "key.pem")


def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    print("Creating a self-signed HTTPS certificate (Safari needs HTTPS for the mic)...")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", KEY, "-out", CERT, "-days", "3650", "-subj", "/CN=phone-mic-cam"],
        check=True, capture_output=True,
    )


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def port_free(port):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # same as aiohttp
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def print_qr(url):
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def log_to_file(path):
    """Send this run's output (ours and ffmpeg's) to a fresh log file.

    The previous run's log is kept as <name>.previous<ext>, so a problem isn't
    lost when the app is restarted; it keeps its 0600 permissions.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        root, ext = os.path.splitext(path)
        os.replace(path, f"{root}.previous{ext}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)          # it contains the pairing link; an older log may be readable
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def fatal(message):
    """Exit with an error the user can see, even when started from the app grid."""
    print(message, file=sys.stderr)
    if not sys.stderr.isatty() and shutil.which("notify-send"):
        subprocess.run(["notify-send", "--icon=phone-mic-cam", "Phone Mic Cam", message],
                       check=False)
    sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="phone-mic-cam",
                                     description="Use your phone as this computer's mic and camera.")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the control page in this computer's browser")
    parser.add_argument("--mic-always-on", action="store_true",
                        help="stream the phone mic all the time, not only while an app records")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--log-file", metavar="PATH",
                        help="write this run's output to PATH (the launcher passes this "
                             "when there's no terminal)")
    args = parser.parse_args(argv)
    audio.mic_always_on = args.mic_always_on
    # Check before touching audio: a second copy would remove the first one's mic.
    if not port_free(PORT):
        url = control_page.running_copy_url()
        if url is None:
            fatal(f"Port {PORT} is already in use by another program, so Phone Mic Cam can't start.")
        print(f"Phone Mic Cam is already running. Control page: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    # Only now, as the copy that starts, replace the previous run's log.
    if args.log_file:
        log_to_file(args.log_file)
    for tool in ("pactl", "openssl"):
        if not shutil.which(tool):
            fatal(f"'{tool}' is missing. Install it with:  sudo apt install "
                  f"{'pulseaudio-utils' if tool == 'pactl' else 'openssl'}")
    try:
        ensure_cert()
        module_id, previous = audio.create_virtual_mic()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as e:
        fatal(f"Couldn't set up the microphone: {e}")
    t = threading.Thread(target=audio.writer_thread, daemon=True)
    t.start()
    camera.cam = camera.setup_camera()

    app = web.Application()
    app.router.add_get("/", phone_server.index)
    app.router.add_get("/ws", phone_server.ws_handler)
    app.router.add_get("/video", phone_server.video_handler)
    app.router.add_post("/report", phone_server.report_handler)
    sslctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sslctx.load_cert_chain(CERT, KEY)

    phone_url = f"https://{lan_ip()}:{PORT}/?t={phone_server.TOKEN}"
    start_desktop, stop_desktop = control_page.hooks(phone_url, not args.no_browser)
    start_watch, stop_watch = audio.mic_watch_hooks(not args.mic_always_on)
    app.on_startup.append(start_desktop)
    app.on_startup.append(start_watch)
    start_stats, stop_stats = stats.hooks()
    app.on_startup.append(start_stats)
    app.on_cleanup.append(stop_stats)
    app.on_shutdown.append(phone_server.close_clients)
    app.on_cleanup.append(stop_watch)
    app.on_cleanup.append(stop_desktop)

    print(f"\n  '{audio.MIC_LABEL}' is now your default microphone.")
    print("  Scan this QR code with your phone (same Wi-Fi):\n")
    print_qr(phone_url)
    print(f"\n  or open: {phone_url}")
    print("  Accept the certificate warning, then tap Start mic / Start camera.")
    print("  Stop with the control page's Stop button or Ctrl+C.")
    try:
        web.run_app(app, host="0.0.0.0", port=PORT, ssl_context=sslctx, print=None,
                    shutdown_timeout=5)
    finally:
        audio.stop_event.set()
        if camera.cam:
            camera.cam.close()
        if previous and previous != audio.SOURCE:
            audio.pactl("set-default-source", previous, check=False)
        audio.pactl("unload-module", module_id, check=False)
        print("Stopped. Previous microphone restored.")
