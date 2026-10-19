"""python3 -m phone_mic_cam"""
import sys

try:
    import aiohttp  # noqa: F401
    import qrcode  # noqa: F401
except ImportError as e:
    sys.exit(f"Missing Python package '{e.name}'. Install with:  "
             "sudo apt install python3-aiohttp python3-qrcode  (or pip install -r requirements.txt)")

from .app import main

main()
