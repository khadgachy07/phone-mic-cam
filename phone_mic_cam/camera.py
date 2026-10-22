"""Phone Cam: phone JPEG frames piped through ffmpeg into the v4l2loopback
webcam, and diagnosis of why the webcam isn't available."""
import functools
import glob
import os
import re
import shutil
import subprocess
import threading
import time

CAM_LABEL = "Phone Cam"           # must match v4l2loopback card_label


MOK_KEY = "/var/lib/shim-signed/mok/MOK.der"   # Ubuntu's DKMS module-signing key


CAM_FPS = 30


# Phone Cam's picture size. Apps using a webcam expect its size to stay put,
# so frames of any size the phone sends (1080p, 720p or 480p) are scaled to this.
CAM_SIZE = (1920, 1080)


# H.264 frames waiting for ffmpeg (~0.5 s at 30 fps); past this the queue is
# dropped up to the next keyframe.
H264_BACKLOG = 15


CODECS = ("jpeg", "h264")


# An H.264 access unit delimiter. ffmpeg's H.264 parser only knows a frame is
# complete when the next one starts, which holds every frame back by one; a
# delimiter after each frame lets it pass the frame on at once.
FRAME_END = b"\x00\x00\x00\x01\x09\xf0"


MAX_FRAME = 1 << 21               # 2 MB per JPEG frame


cam = None                        # VirtualCam, or None when unavailable


camera_problem = None             # why the camera is unavailable, with the fix


def find_loopback_device():
    """Return /dev/videoN of the v4l2loopback device labelled CAM_LABEL."""
    for name_file in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        with open(name_file) as f:
            if f.read().strip() == CAM_LABEL:
                return "/dev/" + name_file.split("/")[-2]
    return None


@functools.lru_cache(maxsize=None)
def ffmpeg_passthrough_args():
    """ffmpeg 5.1+ calls it -fps_mode; older ones (Ubuntu 22.04 has 4.4) only know
    -vsync, which newer ones deprecate."""
    help_text = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"],
                               capture_output=True, text=True).stdout
    if re.search(r"^-fps_mode\b", help_text, re.M):
        return ("-fps_mode", "passthrough")
    return ("-vsync", "passthrough")


class VirtualCam:
    """Feed the phone's video into the loopback device via ffmpeg: JPEG frames
    (only the newest is kept) or an H.264 stream (kept in order)."""

    def __init__(self, device):
        self.device = device
        self.proc = None
        self.codec = "jpeg"
        self.frames = []                 # waiting for ffmpeg, oldest first
        self.waiting_for_key = False
        self.skipped = 0                 # frames dropped before ffmpeg took them
        self._restart = False
        self.cond = threading.Condition()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def set_codec(self, codec):
        """A phone connected. H.264 decoding can only start at a keyframe, and
        a different codec needs a new ffmpeg."""
        with self.cond:
            if codec != self.codec:
                self.codec = codec
                self._restart = True
            self.frames.clear()
            self.waiting_for_key = codec == "h264"

    def push(self, data: bytes, key=False):
        with self.cond:
            if self.codec == "jpeg":
                self.skipped += len(self.frames)
                self.frames = [data]     # keep only the newest frame (low latency)
            else:
                # Each H.264 frame builds on the ones before it, so none can be
                # dropped on its own. When ffmpeg falls behind, drop the queue
                # and wait for the next keyframe instead.
                if len(self.frames) >= H264_BACKLOG:
                    self.skipped += len(self.frames)
                    self.frames.clear()
                    self.waiting_for_key = True
                if self.waiting_for_key and not key:
                    self.skipped += 1
                    return
                self.waiting_for_key = False
                self.frames.append(data + FRAME_END)
            self.cond.notify()

    def _spawn(self, codec="jpeg"):
        if codec == "h264":
            # low_delay: pass each frame on as soon as it's decoded (the phone
            # sends Baseline H.264, which has no reordered frames). Not
            # -fflags nobuffer: with H.264 it loses every other frame and
            # adds 1.7 s of delay.
            source = ["-flags", "low_delay", "-f", "h264"]
        else:
            source = ["-fflags", "nobuffer", "-f", "mjpeg"]
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             # The format is known, so skip probing the first frames: the
             # first picture reaches the webcam in ~0.25 s instead of ~2 s.
             # Without probing ffmpeg misjudges the frame rate (1200k fps)
             # and drops almost every frame, so pass frames through as they
             # arrive instead of retiming them.
             "-probesize", "32", "-analyzeduration", "0",
             # One thread each to decode and to write the webcam: with more,
             # ffmpeg holds several frames back (measured: 3-4, ~0.1 s).
             "-threads", "1", *source, "-framerate", str(CAM_FPS),
             "-i", "pipe:0", *ffmpeg_passthrough_args(),
             # lanczos: the sharpest of ffmpeg's scalers, for smaller sizes scaled up
             "-vf", "scale={}:{}:flags=lanczos".format(*CAM_SIZE),
             "-threads", "1", "-pix_fmt", "yuv420p", "-f", "v4l2", self.device],
            stdin=subprocess.PIPE,
        )

    def _end_ffmpeg(self):
        if self.proc and self.proc.poll() is None:
            # terminate rather than close stdin: the feeder thread may be
            # blocked mid-write holding stdin's lock
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _run(self):
        while not self._stop.is_set():
            with self.cond:
                if not self.frames:
                    self.cond.wait(0.5)
                frames, self.frames = self.frames, []
                codec, restart, self._restart = self.codec, self._restart, False
            if restart:
                self._end_ffmpeg()
            if not frames:
                continue
            if self.proc is None or self.proc.poll() is not None:
                self.proc = self._spawn(codec)
            try:
                for frame in frames:
                    self.proc.stdin.write(frame)
                self.proc.stdin.flush()
            except OSError:
                print("ffmpeg stopped; restarting it on the next frame")
                self.proc = None
                time.sleep(1)

    def close(self):
        self._stop.set()
        self._end_ffmpeg()


def secure_boot_blocks_driver():
    """True when Secure Boot is on and the key DKMS signs drivers with isn't enrolled."""
    if not (shutil.which("mokutil") and os.path.exists(MOK_KEY)):
        return False
    state = subprocess.run(["mokutil", "--sb-state"], capture_output=True, text=True)
    if "SecureBoot enabled" not in state.stdout:
        return False
    test = subprocess.run(["mokutil", "--test-key", MOK_KEY], capture_output=True, text=True)
    return "is not enrolled" in test.stdout + test.stderr


def diagnose_camera():
    """Explain why there's no Phone Cam device, and how to fix it."""
    if not shutil.which("ffmpeg"):
        return "ffmpeg isn't installed. Install it with:  sudo apt install ffmpeg"
    if os.path.isdir("/sys/module/v4l2loopback"):
        return (f"The webcam driver is loaded, but not as '{CAM_LABEL}' (another app may "
                "have loaded it). Restart your computer to apply Phone Mic Cam's settings.")
    if secure_boot_blocks_driver():
        return ("Secure Boot is blocking the webcam driver. Run  "
                f"sudo mokutil --import {MOK_KEY}  and choose a one-time password, then "
                "restart. On the blue screen choose Enroll MOK, Continue, Yes, and enter "
                "that password.")
    return ("The webcam driver isn't loaded. Restart your computer, or run:  "
            "sudo modprobe v4l2loopback devices=1 video_nr=10 "
            f"card_label=\"{CAM_LABEL}\" exclusive_caps=1")


def setup_camera():
    global camera_problem
    device = find_loopback_device() if shutil.which("ffmpeg") else None
    if device is None:
        camera_problem = diagnose_camera()
        print(f"  Camera disabled: {camera_problem}")
        return None
    print(f"  '{CAM_LABEL}' webcam available at {device}.")
    return VirtualCam(device)


def try_enable_camera():
    """Pick up Phone Cam if it appeared after startup, e.g. the driver was loaded
    while the app runs as the control page suggests. Returns the camera or None."""
    global cam, camera_problem
    if cam is None and shutil.which("ffmpeg"):
        device = find_loopback_device()
        if device:
            print(f"'{CAM_LABEL}' webcam is now available at {device}.")
            cam = VirtualCam(device)
            camera_problem = None
    return cam
