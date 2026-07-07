# Native USB control for the XVF3800 -- no xvf_host binary required.
# Works anywhere libusb does, including armv6 (Pi Zero W), which has no
# prebuilt xvf_host.
#
# Protocol (per Seeed's python_control reference implementation): vendor
# control transfers to the device; wIndex = resource id, wValue = command id
# (bit 7 set for reads), payload is little-endian typed values, first byte
# of a read response is a status code (0 = ok, 64 = retry).
#
# CLI parity with xvf_host:  python xvf_usb.py PP_AGCMAXGAIN [160]

import sys
import time
import struct

VID, PID = 0x2886, 0x001A
_SUCCESS, _RETRY = 0, 64

# name: (resid, cmdid, count, mode, ctype)
COMMANDS = {
    "VERSION":                        (48, 0, 3, "ro", "uint8"),
    "REBOOT":                         (48, 7, 1, "wo", "uint8"),
    "SAVE_CONFIGURATION":             (48, 9, 1, "wo", "uint8"),
    "CLEAR_CONFIGURATION":            (48, 10, 1, "wo", "uint8"),
    "AEC_AZIMUTH_VALUES":             (33, 75, 4, "ro", "float"),
    "AEC_SPENERGY_VALUES":            (33, 80, 4, "ro", "float"),
    "AEC_FIXEDBEAMSAZIMUTH_VALUES":   (33, 81, 2, "rw", "float"),
    "AEC_FIXEDBEAMSELEVATION_VALUES": (33, 82, 2, "rw", "float"),
    "AEC_FIXEDBEAMSGATING":           (33, 83, 1, "rw", "uint8"),
    "AEC_FIXEDBEAMSONOFF":            (33, 37, 1, "rw", "int32"),
    "AEC_FIXEDBEAMNOISETHR":          (33, 38, 2, "rw", "float"),
    "AUDIO_MGR_MIC_GAIN":             (35, 0, 1, "rw", "float"),
    "DOA_VALUE":                      (20, 18, 2, "ro", "uint16"),
    "LED_EFFECT":                     (20, 12, 1, "rw", "uint8"),
    "LED_BRIGHTNESS":                 (20, 13, 1, "rw", "uint8"),
    "LED_COLOR":                      (20, 16, 1, "rw", "uint32"),
    "LED_DOA_COLOR":                  (20, 17, 2, "rw", "uint32"),
    "LED_RING_COLOR":                 (20, 19, 12, "rw", "uint32"),
    "LED_GAMMIFY":                    (20, 14, 1, "rw", "uint8"),
    "PP_AGCONOFF":                    (17, 10, 1, "rw", "int32"),
    "PP_AGCMAXGAIN":                  (17, 11, 1, "rw", "float"),
    "PP_AGCDESIREDLEVEL":             (17, 12, 1, "rw", "float"),
    "PP_AGCGAIN":                     (17, 13, 1, "rw", "float"),
    "PP_AGCTIME":                     (17, 14, 1, "rw", "float"),
    "PP_AGCFASTTIME":                 (17, 15, 1, "rw", "float"),
    "PP_LIMITONOFF":                  (17, 19, 1, "rw", "int32"),
    "PP_LIMITPLIMIT":                 (17, 20, 1, "rw", "float"),
    "PP_MIN_NS":                      (17, 21, 1, "rw", "float"),
    "PP_MIN_NN":                      (17, 22, 1, "rw", "float"),
    "PP_ATTNS_MODE":                  (17, 32, 1, "rw", "int32"),
    "PP_ATTNS_NOMINAL":               (17, 33, 1, "rw", "float"),
    "PP_ATTNS_SLOPE":                 (17, 34, 1, "rw", "float"),
}

_FMT = {"uint8": ("B", 1), "uint16": ("H", 2), "uint32": ("I", 4),
        "int32": ("i", 4), "float": ("f", 4)}

try:
    import usb.core
    import usb.util
except ImportError:
    usb = None


def _backend():
    try:
        import libusb_package  # bundles libusb on Windows/mac
        return libusb_package.get_libusb1_backend()
    except ImportError:
        import usb.backend.libusb1 as libusb1
        return libusb1.get_backend()


def backend_available():
    if usb is None:
        return False
    try:
        return _backend() is not None
    except Exception:
        return False


class XvfUsb:
    def __init__(self):
        self._dev = None
        self._last_try = 0.0

    def _connect(self):
        if self._dev is not None:
            return True
        if time.monotonic() - self._last_try < 2.0:  # rate-limit rescans
            return False
        self._last_try = time.monotonic()
        try:
            self._dev = usb.core.find(idVendor=VID, idProduct=PID,
                                      backend=_backend())
        except Exception:
            self._dev = None
        return self._dev is not None

    def _drop(self):
        try:
            usb.util.dispose_resources(self._dev)
        except Exception:
            pass
        self._dev = None

    def get(self, name):
        """Read a command; returns a list of values or None."""
        cmd = COMMANDS.get(name)
        if cmd is None or cmd[3] == "wo" or not self._connect():
            return None
        resid, cmdid, cnt, _, ctype = cmd
        ch, size = _FMT[ctype]
        length = cnt * size + 1
        try:
            for _ in range(100):
                resp = self._dev.ctrl_transfer(
                    0xC0, 0, 0x80 | cmdid, resid, length, 5000)
                if resp[0] == _SUCCESS:
                    return list(struct.unpack("<" + ch * cnt,
                                              resp.tobytes()[1:length]))
                if resp[0] != _RETRY:
                    return None
                time.sleep(0.01)
        except Exception:
            self._drop()
        return None

    def set(self, name, *values):
        """Write a command; returns True on success."""
        cmd = COMMANDS.get(name)
        if cmd is None or cmd[3] == "ro" or not self._connect():
            return False
        resid, cmdid, cnt, _, ctype = cmd
        if len(values) != cnt:
            return False
        ch, _ = _FMT[ctype]

        def num(v):
            if isinstance(v, str):
                try:
                    return int(v, 0)  # handles hex like 0xFF0000
                except ValueError:
                    return float(v)
            return v

        try:
            if ctype == "float":
                vals = [float(num(v)) for v in values]
            else:
                vals = [int(num(v)) for v in values]
            payload = struct.pack("<" + ch * cnt, *vals)
            self._dev.ctrl_transfer(0x40, 0, cmdid, resid, payload, 5000)
            return True
        except Exception:
            self._drop()
            return False

    def close(self):
        if self._dev is not None:
            self._drop()


def main():
    if len(sys.argv) < 2 or sys.argv[1].upper() not in COMMANDS:
        names = "\n  ".join(sorted(COMMANDS))
        print(f"usage: {sys.argv[0]} COMMAND [values...]\ncommands:\n  {names}")
        return 1
    if not backend_available():
        print("no libusb backend (pip install pyusb, plus libusb-package "
              "on Windows/mac or libusb-1.0-0 via apt)")
        return 1
    name = sys.argv[1].upper()
    x = XvfUsb()
    if len(sys.argv) > 2:
        ok = x.set(name, *sys.argv[2:])
        print(f"{name} {'OK' if ok else 'FAILED'}")
        return 0 if ok else 1
    vals = x.get(name)
    if vals is None:
        print(f"{name} FAILED (device connected? permissions?)")
        return 1
    print(name, " ".join(f"{v:g}" if isinstance(v, float) else str(v)
                         for v in vals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
