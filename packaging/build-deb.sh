#!/bin/sh
# Build dist/phone-mic-cam_<version>_all.deb from this checkout.
# Usage: packaging/build-deb.sh
set -eu

cd "$(dirname "$0")/.."
pkg=phone-mic-cam
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' phone_mic_cam.py)
[ -n "$version" ] || { echo "can't find __version__ in phone_mic_cam.py" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/root"

install -D -m 755 packaging/phone-mic-cam          "$root/usr/bin/phone-mic-cam"
install -D -m 644 phone_mic_cam.py                 "$root/usr/share/$pkg/phone_mic_cam.py"
install -D -m 644 packaging/phone-mic-cam.desktop  "$root/usr/share/applications/$pkg.desktop"
install -D -m 644 packaging/phone-mic-cam.svg      "$root/usr/share/icons/hicolor/scalable/apps/$pkg.svg"
install -D -m 644 packaging/modprobe.conf          "$root/usr/lib/modprobe.d/$pkg.conf"
install -D -m 644 packaging/modules-load.conf      "$root/usr/lib/modules-load.d/$pkg.conf"
install -D -m 644 LICENSE                          "$root/usr/share/doc/$pkg/copyright"
install -D -m 644 README.md                        "$root/usr/share/doc/$pkg/README.md"
install -D -m 755 packaging/postinst               "$root/DEBIAN/postinst"

installed_size=$(du -sk --exclude=DEBIAN "$root" | cut -f1)
cat > "$root/DEBIAN/control" <<EOF
Package: $pkg
Version: $version
Architecture: all
Maintainer: khadgachy07 <khadgachy07@gmail.com>
Installed-Size: $installed_size
Depends: python3 (>= 3.10), python3-aiohttp (>= 3.8), python3-qrcode (>= 7.3), pulseaudio-utils, openssl, ffmpeg, v4l2loopback-dkms
Recommends: libnotify-bin, mokutil
Section: sound
Priority: optional
Homepage: https://github.com/khadgachy07/phone-mic-cam
Description: use your phone as this computer's microphone and webcam
 Phone Mic Cam turns a phone into this computer's microphone and webcam
 over Wi-Fi, with no app on the phone. Scan a QR code and the phone's
 browser streams its mic and camera to the computer, which shows them to
 every app as "Phone Mic" and "Phone Cam".
EOF

mkdir -p dist
dpkg-deb --root-owner-group --build "$root" "dist/${pkg}_${version}_all.deb"
