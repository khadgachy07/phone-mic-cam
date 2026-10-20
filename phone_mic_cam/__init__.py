"""Phone Mic Cam: use your phone as this Linux computer's microphone and webcam,
with no app on the phone.

Installed from the .deb package, start it from the app grid ("Phone Mic Cam")
or run `phone-mic-cam`. From source: pip install -r requirements.txt, then
python3 -m phone_mic_cam.

It opens a control page in this computer's browser with a QR code. Scan it
with the phone on the same Wi-Fi, accept the certificate warning, and tap
"Start mic" and/or "Start camera". The link contains a secret code that
changes every run.

While it runs, the computer has a microphone called "Phone Mic" set as the
default input, so every app (browser, YouTube, Claude, Discord...) uses it,
and, when the v4l2loopback driver is loaded, a webcam called "Phone Cam".
Stop it with the control page's Stop button or Ctrl+C; your previous
microphone is restored.

SPDX-License-Identifier: MIT
"""
__version__ = "0.5.0"
