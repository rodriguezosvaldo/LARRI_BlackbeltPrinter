"""Upload firmware to an ESP32 over WiFi (POST /update, see src/web.cpp). Stdlib only.

Normally run by PlatformIO:   pio run -e p1_ota -t upload   (or -e p2_ota)
Other IP:                     pio run -e p1_ota -t upload --upload-port 192.168.1.50
By hand:                      python ota_upload.py 192.168.1.132 .pio/build/p1/firmware.bin

The password is OTA_PASS from include/secrets.h (or the OTA_PASS environment variable).
Works while printing: monitoring pauses for the few seconds of the reboot.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ota_pass() -> str:
    if os.environ.get("OTA_PASS"):
        return os.environ["OTA_PASS"]
    secrets = HERE / "include" / "secrets.h"
    m = re.search(r'^\s*#define\s+OTA_PASS\s+"([^"]*)"', secrets.read_text(), re.M)
    if not m or not m.group(1):
        sys.exit(f"OTA_PASS is not defined in {secrets} (see secrets.example.h)")
    return m.group(1)


def base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith("http") else f"http://{host}"


def running_build(url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{url}/api/spools", timeout=5) as r:
            return json.load(r).get("fw")
    except Exception:
        return None


def upload(url: str, firmware: Path, password: str) -> None:
    data = firmware.read_bytes()
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="firmware"; filename="{firmware.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{url}/update",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": "Basic " + base64.b64encode(f"ota:{password}".encode()).decode(),
        },
    )
    print(f"Uploading {firmware} ({len(data) / 1024:.0f} KB) to {url} ...")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read()).get("error")
        except Exception:
            error = None
        sys.exit(f"Update rejected ({e.code}): {error or e.reason}")
    except OSError as e:
        sys.exit(f"Upload failed: {e}")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    url, firmware = base_url(sys.argv[1]), Path(sys.argv[2])
    if not firmware.is_file():
        sys.exit(f"Firmware not found: {firmware}")
    password = ota_pass()
    before = running_build(url)
    if before is None:
        sys.exit(f"ESP32 not reachable at {url} (check the IP: it comes from DHCP)")
    print(f"Running build: {before}")
    upload(url, firmware, password)
    print("Uploaded, waiting for the reboot ...")
    time.sleep(4)
    deadline = time.time() + 60
    while time.time() < deadline:
        after = running_build(url)
        if after:
            print(f"OK, running build: {after}")
            if after == before:
                print("Warning: same build string as before (was the firmware rebuilt?)")
            return
        time.sleep(2)
    sys.exit("The ESP32 did not come back within 60 s: check it over USB")


if __name__ == "__main__":
    main()
