# DEADPLUG // XVF3800 AUTOTUNE
# Records from the XVF3800, watches voice levels, and adjusts device DSP
# parameters on the fly to tune in voices (whispers included).
#
# Windows GUI:  pythonw autotune.py            (or "XVF3800 Autotune.cmd")
# Pi / server:  python3 autotune.py --headless --serve 8380
# Web remote:   add --serve PORT in GUI mode too; open http://<host>:PORT
# Smoke test:   set XVF_AUTOTUNE_SMOKETEST=1 -> headless 2 s capture + dry-run
# tuning decision, prints a summary, exits.

import os
import sys
import json
import math
import time
import wave
import queue
import platform
import argparse
import threading
import subprocess
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import xvf_usb as _xvf_usb  # native USB control; no binary needed
except ImportError:
    _xvf_usb = None


def _xvf_host_path():
    if sys.platform == "win32":
        sub, exe = "win32", "xvf_host.exe"
    elif sys.platform == "darwin":
        sub, exe = "mac_arm64", "xvf_host"
    else:
        mach = platform.machine().lower()
        sub = "rpi_64bit" if mach in ("aarch64", "arm64") else "linux_x86_64"
        exe = "xvf_host"
    return os.path.join(HERE, "host_control", sub, exe)


XVF_HOST = _xvf_host_path()
REC_DIR = os.path.join(HERE, "recordings")
PREROLL_S = 2.0        # audio kept before REC/VOX trigger
VOX_HANGOVER_S = 2.5   # keep recording this long after voice stops

# LED scanner (host-driven ring animation; needs the native USB backend)
LED_COUNT = 12
LED_AZ_OFFSET = 0.0    # degrees; rotate if the pointer looks misaligned
LED_DIR = 1            # -1 if the pointer runs the wrong way round
SCAN_FPS = 20
SCAN_CHASE_DPS = 120.0  # idle sweep speed, degrees/s
SCAN_SLEW_DPS = 360.0   # how fast it darts to a voice
SCAN_HOLD_S = 1.5       # keep focus this long after voice stops
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

BLOCK_S = 0.1  # analysis block duration; sample rate is negotiated per-device
RATE_CANDIDATES = (48000, 16000)

# Control-loop targets and bounds. The device AGC is an inner feedback loop;
# this outer loop must stay slow (one small step per cycle) or they fight.
TARGET_DBFS_DEFAULT = -28.0
DEADBAND_DB = 3.0
CYCLE_S = 3.0
MAXGAIN_LO, MAXGAIN_HI = 64.0, 300.0
DESIRED_LO, DESIRED_HI = 0.003, 0.015
VAD_MARGIN_DB = 12.0
VAD_ABS_MIN_DBFS = -50.0  # below this it's never "voice", whatever the floor
SILENCE_DBFS = -80.0      # startup/underrun zeros; excluded from floor tracking
OVERSHOOT_PEAK_DB = -6.0
OVERSHOOT_DROP_DB = 12.0

# tuning presets, selectable in-app
PRESETS = {
    # factory behavior
    "STOCK": {
        "AUDIO_MGR_MIC_GAIN": 90, "PP_AGCONOFF": 1, "PP_AGCMAXGAIN": 64,
        "PP_AGCDESIREDLEVEL": 0.0045, "PP_AGCTIME": 0.9,
        "PP_AGCFASTTIME": 0.1, "PP_MIN_NS": 0.15, "PP_MIN_NN": 0.51,
        "AEC_HPFONOFF": 2,
    },
    # the tuned quiet-voice baseline
    "WHISPER": {
        "AUDIO_MGR_MIC_GAIN": 90, "PP_AGCONOFF": 1, "PP_AGCMAXGAIN": 160,
        "PP_AGCDESIREDLEVEL": 0.007, "PP_AGCTIME": 0.9,
        "PP_AGCFASTTIME": 0.2, "PP_MIN_NS": 0.35, "PP_MIN_NN": 0.6,
        "AEC_HPFONOFF": 2,
    },
    # loud rooms: little gain, aggressive noise suppression, higher HPF
    "LOUD": {
        "AUDIO_MGR_MIC_GAIN": 90, "PP_AGCONOFF": 1, "PP_AGCMAXGAIN": 16,
        "PP_AGCDESIREDLEVEL": 0.003, "PP_AGCTIME": 0.9,
        "PP_AGCFASTTIME": 0.1, "PP_MIN_NS": 0.1, "PP_MIN_NN": 0.4,
        "AEC_HPFONOFF": 3,
    },
    # maximum whisper reach; expect audible room noise and pumping
    "EXPERIMENTAL": {
        "AUDIO_MGR_MIC_GAIN": 90, "PP_AGCONOFF": 1, "PP_AGCMAXGAIN": 300,
        "PP_AGCDESIREDLEVEL": 0.01, "PP_AGCTIME": 1.5,
        "PP_AGCFASTTIME": 0.3, "PP_MIN_NS": 0.5, "PP_MIN_NN": 0.7,
        "AEC_HPFONOFF": 1,
    },
}
DEFAULTS = PRESETS["WHISPER"]

# user-tunable device params: name -> (label, lo, hi, step)
TUNABLES = {
    "AUDIO_MGR_MIC_GAIN": ("MIC GAIN", 0, 255, 1),
    "PP_AGCMAXGAIN": ("AGC MAX GAIN", 1, 500, 1),
    "PP_AGCDESIREDLEVEL": ("AGC TARGET LVL", 0.001, 0.02, 0.0005),
    "PP_MIN_NS": ("NS FLOOR / STATIONARY (lower = cleaner, riskier)", 0.0, 1.0, 0.01),
    "PP_MIN_NN": ("NS FLOOR / NON-STAT (lower = cleaner, riskier)", 0.0, 1.0, 0.01),
    "AEC_HPFONOFF": ("HIGH-PASS  0=off 1=70Hz 2=125Hz 3=150Hz 4=180Hz", 0, 4, 1),
}

BG = "#050505"
PANEL = "#0a0a0a"
RED = "#ff2222"
DIMRED = "#7a0f0f"
TEXT = "#c9c9c9"
AMBER = "#ffaa00"
GRAY = "#555555"


def dbfs(x):
    return 20.0 * math.log10(max(float(x), 1e-9))


class Xvf:
    """Serialised device control: native USB (pyusb) when available,
    otherwise the vendored xvf_host binary. Native is required on
    platforms with no prebuilt binary (e.g. armv6 / Pi Zero W)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._native = None
        if _xvf_usb is not None and _xvf_usb.backend_available():
            self._native = _xvf_usb.XvfUsb()

    def _run(self, *args):
        with self._lock:
            try:
                out = subprocess.run(
                    [XVF_HOST, *[str(a) for a in args]],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                if out.returncode != 0:
                    return None
                return out.stdout
            except Exception:
                return None

    def get(self, name):
        if self._native is not None:
            with self._lock:
                vals = self._native.get(name)
            return [float(v) for v in vals] if vals is not None else None
        out = self._run(name)
        if not out:
            return None
        for line in out.splitlines():
            if line.startswith(name):
                try:
                    return [float(v) for v in line.split()[1:]]
                except ValueError:
                    return None
        return None

    def get1(self, name):
        vals = self.get(name)
        return vals[0] if vals else None

    def set(self, name, value):
        if self._native is not None:
            with self._lock:
                return self._native.set(name, value)
        return self._run(name, value) is not None

    def set_multi(self, name, *values):
        if self._native is not None:
            with self._lock:
                return self._native.set(name, *values)
        return self._run(name, *[f"{v:.6f}" for v in values]) is not None

    def save_to_flash(self):
        if self._native is not None:
            with self._lock:
                return self._native.set("SAVE_CONFIGURATION", 1)
        return self._run("save_configuration", "1") is not None


class Engine(threading.Thread):
    """Audio capture + telemetry + the autotune control loop."""

    def __init__(self, arm_vox=False):
        super().__init__(daemon=True)
        self.xvf = Xvf()
        self.log_q = queue.Queue()
        self.lock = threading.Lock()
        self.blocks = collections.deque(maxlen=int(10.0 / BLOCK_S))
        self.state = {
            "audio_ok": False, "device_ok": False,
            "rms_db": -90.0, "peak_db": -90.0,
            "noise_floor_db": -70.0, "vad": False,
            "agc_gain": 0.0, "doa_deg": None, "beam_energy": 0.0,
            "params": {}, "auto": False, "target_dbfs": TARGET_DBFS_DEFAULT,
            "last_action": "--",
            "recording": False, "vox": bool(arm_vox), "rec_source": None,
            "rec_file": "", "rec_started": 0.0,
            "beam_lock": False, "locked_az_deg": None, "doa_rad": None,
            "scan": False, "leds_on": True,
        }
        self._voice_until = 0.0
        self._voice_az = None
        self.doa_hist = collections.deque(maxlen=30)  # (t, azimuth) w/ voice
        self._stop = threading.Event()
        self.sample_rate = None
        self.log_lines = collections.deque(maxlen=200)
        self.preroll = collections.deque(maxlen=int(PREROLL_S / BLOCK_S))
        self.rec_q = queue.Queue()
        self._wav = None
        self._wav_lock = threading.Lock()
        self._last_voice = 0.0
        self._last_cb = 0.0  # last audio-callback time, for the watchdog
        self._last_status_log = 0.0
        self.listeners = set()  # per-client queues for /live.pcm

    # ---- logging ----
    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_q.put(line)
        self.log_lines.append(line)

    # ---- audio ----
    def _find_devices(self):
        """Ranked candidate input devices: WASAPI first, then DirectSound.
        Refreshes PortAudio's device snapshot first — indices go stale after
        the XVF3800 re-enumerates, which can silently remap to WDM-KS."""
        # Windows only: WASAPI/WDM indices go stale and need a full PortAudio
        # reinit. On Linux/ALSA this reinit can deadlock while a stream handle
        # is still open, and hw:X,0 device names are stable, so skip it.
        if sys.platform == "win32":
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        ranked = []
        for i, d in enumerate(sd.query_devices()):
            if "XVF3800" not in d["name"] or d["max_input_channels"] < 1:
                continue
            api = sd.query_hostapis(d["hostapi"])["name"]
            rank = 0 if "WASAPI" in api else 1 if "DirectSound" in api else 2
            ranked.append((rank, i))
        return [i for _, i in sorted(ranked)]

    @staticmethod
    def _close_stream_async(stream):
        """Tear down a (possibly dead) stream without blocking the run loop —
        abort()/close() on a re-enumerated ALSA handle can hang."""
        def worker():
            for fn in (lambda: stream.abort(ignore_errors=True),
                       lambda: stream.close(ignore_errors=True)):
                try:
                    fn()
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _open_stream(self):
        """Try candidate devices and sample rates (device default first —
        ALSA exposes the XVF3800's raw 16 kHz endpoint, WASAPI resamples
        to 48 kHz). Returns (started_stream, description_or_error)."""
        last_err = "no XVF3800 input device found"
        for idx in self._find_devices():
            d = sd.query_devices(idx)
            ch = min(2, int(d["max_input_channels"])) or 1
            rates = []
            default = int(d.get("default_samplerate") or 0)
            for r in (default,) + RATE_CANDIDATES:
                if r > 0 and r not in rates:
                    rates.append(r)
            for sr in rates:
                try:
                    s = sd.InputStream(
                        device=idx, samplerate=sr, channels=ch,
                        blocksize=int(sr * BLOCK_S), dtype="float32",
                        latency="high",  # bigger ALSA buffers; fewer xruns
                        callback=self._audio_cb)
                    s.start()
                    self.sample_rate = sr
                    api = sd.query_hostapis(d["hostapi"])["name"]
                    return s, f"{d['name']} [{api}] {sr} Hz {ch}ch"
                except Exception as e:
                    last_err = e
        return None, last_err

    def _audio_cb(self, indata, frames, t, status):
        self._last_cb = time.monotonic()
        if status and self._last_cb - self._last_status_log > 2.0:
            self._last_status_log = self._last_cb
            self.log(f"audio status: {status}")  # xruns etc. from PortAudio
        mono = indata[:, 0].copy()
        rms = dbfs(np.sqrt(np.mean(mono ** 2)))
        peak = dbfs(np.max(np.abs(mono)))
        self.preroll.append(mono)
        with self.lock:
            if self.state["recording"]:
                self.rec_q.put(mono)
            listeners = list(self.listeners)
        if listeners:
            data = self._to_i16(mono)
            for q in listeners:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass  # slow client: drop rather than lag
            nf = self.state["noise_floor_db"]
            if rms > SILENCE_DBFS:
                # fast to fall, slow to rise: tracks the quiet between words
                nf = 0.9 * nf + 0.1 * rms if rms < nf else nf + 0.1
                nf = max(SILENCE_DBFS, min(nf, -20.0))
            vad = rms > max(nf + VAD_MARGIN_DB, VAD_ABS_MIN_DBFS)
            self.state.update(rms_db=rms, peak_db=peak,
                              noise_floor_db=nf, vad=vad)
            self.blocks.append((time.monotonic(), rms, peak, vad))

    # ---- recording ----
    @staticmethod
    def _to_i16(block):
        return (np.clip(block, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    def _drain_rec(self):
        with self._wav_lock:
            if self._wav is None:
                while not self.rec_q.empty():
                    self.rec_q.get_nowait()
                return
            while not self.rec_q.empty():
                self._wav.writeframes(self._to_i16(self.rec_q.get_nowait()))

    def start_recording(self, source="manual"):
        with self.lock:
            if self.state["recording"]:
                return
        os.makedirs(REC_DIR, exist_ok=True)
        path = os.path.join(REC_DIR,
                            time.strftime("xvf_%Y%m%d_%H%M%S") + ".wav")
        w = wave.open(path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(self.sample_rate or 48000)
        for blk in list(self.preroll):
            w.writeframes(self._to_i16(blk))
        with self._wav_lock:
            self._wav = w
        with self.lock:
            self.state["recording"] = True
            self.state["rec_source"] = source
            self.state["rec_file"] = os.path.basename(path)
            self.state["rec_started"] = time.monotonic()
        self.log(f"REC start ({source}): {os.path.basename(path)} "
                 f"(+{PREROLL_S:g} s pre-roll)")

    def stop_recording(self):
        with self.lock:
            if not self.state["recording"]:
                return
            self.state["recording"] = False
            started = self.state["rec_started"]
            name = self.state["rec_file"]
        self._drain_rec()
        with self._wav_lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
        self.log(f"REC stop: {name} "
                 f"({time.monotonic() - started + PREROLL_S:.1f} s)")

    # ---- device params ----
    PARAM_NAMES = ("PP_AGCMAXGAIN", "PP_AGCDESIREDLEVEL", "PP_AGCFASTTIME",
                   "PP_MIN_NS", "PP_MIN_NN", "PP_AGCONOFF",
                   "AUDIO_MGR_MIC_GAIN", "AEC_HPFONOFF")

    def read_params(self):
        p = {}
        for name in self.PARAM_NAMES:
            v = self.xvf.get1(name)
            if v is None:
                with self.lock:
                    self.state["device_ok"] = False
                return False
            p[name] = v
        with self.lock:
            self.state["params"] = p
            self.state["device_ok"] = True
        return True

    def set_param(self, name, value, reason, tag="AUTO"):
        value = round(float(value), 6)
        if value == int(value):
            value = int(value)  # int32 params reject "1.0"
        if self.xvf.set(name, value):
            with self.lock:
                self.state["params"][name] = value
                self.state["last_action"] = f"{name} -> {value:g}"
            self.log(f"{tag}: {name} -> {value:g}  ({reason})")
        else:
            self.log(f"{tag}: {name} write failed (device offline?)")

    def add_listener(self):
        q = queue.Queue(maxsize=50)  # ~5 s backlog cap
        with self.lock:
            self.listeners.add(q)
        return q

    def remove_listener(self, q):
        with self.lock:
            self.listeners.discard(q)

    def apply_preset(self, preset):
        for name, value in PRESETS[preset].items():
            self.set_param(name, value, f"preset {preset}", "SET")
        self.log(f"Preset applied: {preset}.")

    def apply_defaults(self):
        self.apply_preset("WHISPER")

    def set_leds(self, on):
        """LED ring on (flashed DOA effect) or fully dark."""
        if not on:
            with self.lock:
                self.state["scan"] = False  # scanner would relight it
        if self.xvf.set("LED_EFFECT", 4 if on else 0):
            with self.lock:
                self.state["leds_on"] = bool(on)
            self.log("LEDs " + ("on (DOA mode)." if on else "off."))
        else:
            self.log("LED write failed (device offline?)")

    # ---- beam locking ----
    def lock_beam(self):
        """Fix both focused beams on the voice's bearing (circular mean of
        DOA samples taken while voice was active in the last 15 s)."""
        cutoff = time.monotonic() - 15.0
        with self.lock:
            angles = [a for t, a in self.doa_hist if t > cutoff]
            fallback = self.state.get("doa_rad")
        if not angles:
            if fallback is None:
                self.log("LOCK: no voice bearing yet - speak first, then lock.")
                return False
            angles = [fallback]
        az = math.atan2(sum(math.sin(a) for a in angles) / len(angles),
                        sum(math.cos(a) for a in angles) / len(angles))
        az %= 2 * math.pi
        ok = (self.xvf.set_multi("AEC_FIXEDBEAMSAZIMUTH_VALUES", az, az)
              and self.xvf.set("AEC_FIXEDBEAMSGATING", 0)  # never mute a beam
              and self.xvf.set("AEC_FIXEDBEAMSONOFF", 1))
        if ok:
            deg = math.degrees(az) % 360
            with self.lock:
                self.state["beam_lock"] = True
                self.state["locked_az_deg"] = deg
            self.log(f"LOCK: beams fixed at {deg:.0f} deg "
                     f"({len(angles)} voice bearing(s) averaged).")
        else:
            self.log("LOCK failed: control interface not responding.")
        return ok

    def unlock_beam(self):
        if self.xvf.set("AEC_FIXEDBEAMSONOFF", 0):
            with self.lock:
                self.state["beam_lock"] = False
                self.state["locked_az_deg"] = None
            self.log("LOCK released: auto beam tracking restored.")
            return True
        self.log("UNLOCK failed: control interface not responding.")
        return False

    # ---- LED scanner ----
    @staticmethod
    def _angdiff(d):
        return ((d + 180.0) % 360.0) - 180.0

    def _led_loop(self):
        """Knight-Rider chase around the ring; darts to and pulses on the
        bearing of detected speech. Drives LED_RING_COLOR frame by frame,
        so it needs the native USB backend (subprocess would be too slow)."""
        center = 0.0
        was_on = False
        last = time.monotonic()
        while not self._stop.is_set():
            with self.lock:
                enabled = (self.state["scan"] and self.state["device_ok"])
                vad = self.state["vad"]
            if not enabled:
                if was_on:
                    self.xvf.set("LED_EFFECT", 4)  # saved DOA look
                    was_on = False
                time.sleep(0.5)
                last = time.monotonic()
                continue
            if not was_on:
                was_on = self.xvf.set("LED_EFFECT", 5)  # host-driven ring
                if not was_on:
                    time.sleep(1.0)
                    continue
                self.log("Scanner engaged.")
            now = time.monotonic()
            dt = min(now - last, 0.2)
            last = now

            doa = self.xvf.get("DOA_VALUE")  # [degrees, speech_detected]
            if doa and (doa[1] or vad):
                self._voice_until = now + SCAN_HOLD_S
                self._voice_az = float(doa[0])

            focused = False
            if now < self._voice_until and self._voice_az is not None:
                err = self._angdiff(self._voice_az - center)
                step = max(-SCAN_SLEW_DPS * dt,
                           min(SCAN_SLEW_DPS * dt, err))
                center = (center + step) % 360.0
                focused = abs(err) < 12.0
            else:
                center = (center + SCAN_CHASE_DPS * dt) % 360.0

            width = 16.0 if focused else 26.0
            boost = 0.75 + 0.25 * math.sin(now * 4 * math.pi) \
                if focused else 1.0
            colors = []
            for i in range(LED_COUNT):
                led_az = (LED_DIR * i * (360.0 / LED_COUNT)
                          + LED_AZ_OFFSET) % 360.0
                d = abs(self._angdiff(led_az - center))
                inten = math.exp(-(d / width) ** 2) * boost
                colors.append(int(255 * min(1.0, inten) ** 1.8) << 16)
            self.xvf.set_multi("LED_RING_COLOR", *colors)
            time.sleep(max(0.0, 1.0 / SCAN_FPS - (time.monotonic() - now)))
        if was_on:
            self.xvf.set("LED_EFFECT", 4)

    # ---- the control loop ----
    def autotune_step(self, dry_run=False):
        with self.lock:
            blocks = [b for b in self.blocks
                      if b[0] > time.monotonic() - CYCLE_S]
            p = dict(self.state["params"])
            target = self.state["target_dbfs"]
            agc_gain = self.state["agc_gain"]
        if not blocks or not p:
            return "no data"

        speech = [b for b in blocks if b[3]]
        # overshoot: a near-full-scale peak followed shortly by a big RMS drop
        # while speech continues = AGC slamming into the limiter
        overshoot = False
        for i, (t0, rms0, peak0, _) in enumerate(blocks):
            if peak0 <= OVERSHOOT_PEAK_DB:
                continue
            for t1, rms1, _, vad1 in blocks[i + 1:i + 6]:
                if vad1 and rms1 < rms0 - OVERSHOOT_DROP_DB:
                    overshoot = True
                    break
            if overshoot:
                break

        decision = None
        if p.get("PP_AGCONOFF", 1) < 1:
            decision = ("PP_AGCONOFF", 1,
                        "AGC was disabled; autotune requires it")
        elif overshoot:
            new = max(MAXGAIN_LO, p["PP_AGCMAXGAIN"] * 0.8)
            if new < p["PP_AGCMAXGAIN"] - 0.5:
                decision = ("PP_AGCMAXGAIN", new, "overshoot: gain collapse after peak")
        elif speech:
            med = float(np.median([b[1] for b in speech]))
            maxpeak = max(b[2] for b in speech)
            if med < target - DEADBAND_DB and maxpeak < OVERSHOOT_PEAK_DB:
                if agc_gain > 0.85 * p["PP_AGCMAXGAIN"]:
                    new = min(MAXGAIN_HI, p["PP_AGCMAXGAIN"] * 1.25)
                    if new > p["PP_AGCMAXGAIN"] + 0.5:
                        decision = ("PP_AGCMAXGAIN", new,
                                    f"voice {med:.0f} dBFS below target, AGC pegged")
                else:
                    new = min(DESIRED_HI, p["PP_AGCDESIREDLEVEL"] * 1.25)
                    if new > p["PP_AGCDESIREDLEVEL"] * 1.01:
                        decision = ("PP_AGCDESIREDLEVEL", new,
                                    f"voice {med:.0f} dBFS below target")
            elif med > target + DEADBAND_DB:
                new = max(DESIRED_LO, p["PP_AGCDESIREDLEVEL"] * 0.8)
                if new < p["PP_AGCDESIREDLEVEL"] * 0.99:
                    decision = ("PP_AGCDESIREDLEVEL", new,
                                f"voice {med:.0f} dBFS above target")

        if decision is None:
            return "hold"
        if dry_run:
            return f"would set {decision[0]} -> {decision[1]:g} ({decision[2]})"
        self.set_param(*decision)
        return "adjusted"

    # ---- main thread loop ----
    def run(self):
        self.log("Control backend: " +
                 ("native USB" if self.xvf._native is not None
                  else "xvf_host binary"))
        if self.xvf._native is not None:
            threading.Thread(target=self._led_loop, daemon=True).start()
        else:
            with self.lock:
                self.state["scan"] = False
        self.read_params()
        if self.state["device_ok"]:
            self.log("Device online. " + "  ".join(
                f"{k.replace('PP_', '')}={v:g}"
                for k, v in self.state["params"].items()))
            led = self.xvf.get1("LED_EFFECT")
            if led is not None:
                with self.lock:
                    self.state["leds_on"] = led != 0
            if self.xvf.get1("AEC_FIXEDBEAMSONOFF"):
                azv = self.xvf.get("AEC_FIXEDBEAMSAZIMUTH_VALUES")
                with self.lock:
                    self.state["beam_lock"] = True
                    if azv:
                        self.state["locked_az_deg"] = \
                            math.degrees(azv[0]) % 360
                self.log("Device already in fixed-beam mode.")
        else:
            self.log("ERROR: control interface not responding.")

        stream = None
        last_cycle = last_telem = 0.0
        while not self._stop.is_set():
            if stream is None:
                stream, info = self._open_stream()
                if stream is not None:
                    self._last_cb = time.monotonic()
                    with self.lock:
                        self.state["audio_ok"] = True
                    self.log(f"Capture started: {info}")
                else:
                    with self.lock:
                        self.state["audio_ok"] = False
                    self.log(f"Capture failed ({info}); retrying in 5 s")
                    time.sleep(5)
                    continue

            now = time.monotonic()

            # watchdog: a USB re-enumeration leaves the stream open but silent
            # (callback stops firing). Tear it down so it reopens on the
            # refreshed device.
            if now - self._last_cb > 3.0:
                self.log("Capture stalled (device re-enumerated?); reopening.")
                with self.lock:
                    self.state["audio_ok"] = False
                self._close_stream_async(stream)
                stream = None
                time.sleep(1.0)
                continue

            self._drain_rec()
            with self.lock:
                vad = self.state["vad"]
                vox = self.state["vox"]
                rec = self.state["recording"]
                rec_src = self.state["rec_source"]
            if vad:
                self._last_voice = now
            if vox and not rec and vad:
                self.start_recording("vox")
            elif rec and rec_src == "vox" and (
                    not vox or now - self._last_voice > VOX_HANGOVER_S):
                self.stop_recording()

            if now - last_telem >= 1.0:
                last_telem = now
                gain = self.xvf.get1("PP_AGCGAIN")
                az = self.xvf.get("AEC_AZIMUTH_VALUES")
                sp = self.xvf.get("AEC_SPENERGY_VALUES")
                with self.lock:
                    if gain is not None:
                        self.state["agc_gain"] = gain
                        self.state["device_ok"] = True
                    else:
                        self.state["device_ok"] = False
                    if az and len(az) >= 4:
                        self.state["doa_deg"] = math.degrees(az[3]) % 360
                        self.state["doa_rad"] = az[3]
                        if self.state["vad"]:
                            self.doa_hist.append((now, az[3]))
                    if sp and len(sp) >= 4:
                        self.state["beam_energy"] = sp[3]
            if self.state["auto"] and now - last_cycle >= CYCLE_S:
                last_cycle = now
                self.autotune_step()
            time.sleep(0.1)

        self.stop_recording()
        if stream is not None:
            stream.stop()
            stream.close()

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------- web remote
WEB_PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>DEADPLUG // XVF3800</title>
<style>
  body { background:#050505; color:#c9c9c9; font-family:ui-monospace,Consolas,monospace;
         margin:0; padding:14px; max-width:640px; margin-inline:auto; }
  h1 { color:#ff2222; font-size:18px; margin:0 0 4px; }
  #status { color:#666; font-size:11px; float:right; margin-top:6px; }
  hr { border:0; border-top:1px solid #7a0f0f; }
  canvas { width:100%; height:64px; background:#0a0a0a; border:1px solid #7a0f0f;
           display:block; margin:10px 0 6px; }
  #telem { font-size:12px; white-space:pre-wrap; color:#c9c9c9; min-height:3em; }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; align-items:center; }
  button { background:#120606; color:#ff4444; border:1px solid #7a0f0f;
           font-family:inherit; font-size:14px; padding:10px 16px; cursor:pointer; }
  button.on { background:#ff2222; color:#050505; }
  button.amber { color:#ffaa00; border-color:#aa6600; }
  input[type=range] { flex:1; accent-color:#ff2222; }
  #log { background:#0a0a0a; border:1px solid #3a0a0a; color:#9a3d3d; font-size:11px;
         padding:8px; height:130px; overflow-y:auto; white-space:pre-wrap; }
  #recs div { display:flex; align-items:center; gap:8px; margin:6px 0; font-size:12px; }
  #recs audio { height:28px; flex:1; min-width:0; }
  a { color:#ff4444; }
</style></head><body>
<span id="status">...</span><h1>DEADPLUG // XVF3800</h1><hr>
<canvas id="meter" width="640" height="64"></canvas>
<div id="telem"></div>
<div class="row">
  <button id="rec">REC</button>
  <button id="vox">VOX OFF</button>
  <button id="scan">SCAN</button>
  <button id="leds">LEDS</button>
  <button id="listen">LISTEN</button>
  <button id="auto">AUTO OFF</button>
  <button id="lock">LOCK</button>
  <button id="agc">AGC</button>
  <button id="save" class="amber">SAVE TO FLASH</button>
</div>
<details style="margin:10px 0">
  <summary style="color:#ff4444;cursor:pointer">TUNING</summary>
  <div id="tuners"></div>
  <div class="row" id="presets" style="margin-top:6px"></div>
</details>
<div class="row"><span>TARGET dBFS <b id="tval">-28</b></span>
  <input type="range" id="target" min="-40" max="-15" step="1" value="-28">
</div>
<div id="log"></div>
<h1 style="font-size:14px;margin-top:14px">RECORDINGS</h1>
<div id="recs"></div>
<script>
const $ = id => document.getElementById(id);
let state = null;
function post(url, body) {
  return fetch(url, {method:'POST', body: JSON.stringify(body||{})});
}
$('rec').onclick  = () => post('/api/rec', {on: !(state && state.recording)});
$('vox').onclick  = () => post('/api/toggle', {what:'vox'});
$('scan').onclick = () => post('/api/toggle', {what:'scan'});
$('auto').onclick = () => post('/api/toggle', {what:'auto'});
$('lock').onclick = () => post('/api/lock', {on: !(state && state.beam_lock)});
$('agc').onclick = () => post('/api/param', {name: 'PP_AGCONOFF',
  value: (state && state.params && state.params.PP_AGCONOFF >= 1) ? 0 : 1});
$('save').onclick = () => post('/api/save');
const TUNABLES = {
  'AUDIO_MGR_MIC_GAIN': ['MIC GAIN', 0, 255, 1],
  'PP_AGCMAXGAIN': ['AGC MAX GAIN', 1, 500, 1],
  'PP_AGCDESIREDLEVEL': ['AGC TARGET LVL', 0.001, 0.02, 0.0005],
  'PP_MIN_NS': ['NS FLOOR STAT', 0, 1, 0.01],
  'PP_MIN_NN': ['NS FLOOR NON-STAT', 0, 1, 0.01],
  'AEC_HPFONOFF': ['HIGH-PASS 0=off 1..4=70/125/150/180Hz', 0, 4, 1],
};
$('tuners').innerHTML = Object.entries(TUNABLES).map(([n, t]) =>
  '<div class="row" style="margin:6px 0"><span style="font-size:11px">' + t[0] +
  ' <b id="v_' + n + '" style="color:#ff4444"></b></span>' +
  '<input type="range" data-p="' + n + '" min="' + t[1] + '" max="' + t[2] +
  '" step="' + t[3] + '" style="width:100%"></div>').join('');
document.querySelectorAll('input[data-p]').forEach(el => {
  el.oninput = () => { $('v_' + el.dataset.p).textContent = el.value; };
  el.onchange = () => post('/api/param', {name: el.dataset.p, value: +el.value});
});
let tsync = false;
$('leds').onclick = () => post('/api/leds', {on: !(state && state.leds_on)});

// live listen: stream raw PCM, schedule through Web Audio
let audioCtx = null, listenAbort = null;
function setListenUi(on) {
  $('listen').textContent = on ? 'LISTENING' : 'LISTEN';
  $('listen').className = on ? 'on' : '';
}
function stopListen() {
  if (listenAbort) listenAbort.abort();
  listenAbort = null;
  if (audioCtx) audioCtx.close();
  audioCtx = null;
  setListenUi(false);
}
async function startListen() {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  listenAbort = new AbortController();
  setListenUi(true);
  try {
    const resp = await fetch('/live.pcm', {signal: listenAbort.signal});
    if (!resp.ok) throw new Error('no audio');
    const sr = +resp.headers.get('X-Sample-Rate') || 16000;
    const reader = resp.body.getReader();
    let playT = 0, carry = new Uint8Array(0);
    while (audioCtx) {
      const {done, value} = await reader.read();
      if (done) break;
      let bytes = new Uint8Array(carry.length + value.length);
      bytes.set(carry); bytes.set(value, carry.length);
      const usable = bytes.length - (bytes.length % 2);
      carry = bytes.slice(usable);
      if (!usable) continue;
      const i16 = new Int16Array(bytes.buffer.slice(0, usable));
      const f32 = new Float32Array(i16.length);
      for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
      const buf = audioCtx.createBuffer(1, f32.length, sr);
      buf.getChannelData(0).set(f32);
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      if (playT < audioCtx.currentTime + 0.05)
        playT = audioCtx.currentTime + 0.2;  // jitter buffer
      src.start(playT);
      playT += buf.duration;
    }
  } catch (e) { /* aborted or stream ended */ }
  stopListen();
}
$('listen').onclick = () => { audioCtx ? stopListen() : startListen(); };
$('presets').innerHTML = ['STOCK', 'WHISPER', 'LOUD', 'EXPERIMENTAL'].map(n =>
  '<button data-preset="' + n + '">' + n + '</button>').join('');
document.querySelectorAll('button[data-preset]').forEach(el => {
  el.onclick = async () => {
    await post('/api/preset', {name: el.dataset.preset});
    tsync = false;  // re-sync sliders from device state on next tick
  };
});
$('target').oninput = e => { $('tval').textContent = e.target.value; };
$('target').onchange = e => post('/api/target', {dbfs: +e.target.value});
function x(db, w) { return Math.max(0, Math.min(w, (db + 70) / 70 * w)); }
function draw(s) {
  const c = $('meter'), g = c.getContext('2d'), w = c.width, h = c.height;
  g.clearRect(0, 0, w, h);
  g.fillStyle = '#ff2222'; g.fillRect(0, 8, x(s.rms_db, w), 22);
  g.fillStyle = '#ff8888'; g.fillRect(x(s.peak_db, w) - 2, 8, 4, 22);
  g.strokeStyle = '#555';
  g.beginPath(); g.moveTo(x(s.noise_floor_db, w), 4);
  g.lineTo(x(s.noise_floor_db, w), 34); g.stroke();
  g.strokeStyle = '#ffaa00'; g.setLineDash([3, 2]);
  g.beginPath(); g.moveTo(x(s.target_dbfs, w), 4);
  g.lineTo(x(s.target_dbfs, w), 34); g.stroke(); g.setLineDash([]);
  g.font = 'bold 16px monospace';
  g.fillStyle = s.vad ? '#ff2222' : '#555';
  g.fillText(s.vad ? 'VOICE' : '.....', 6, 54);
  g.fillStyle = '#c9c9c9'; g.font = '12px monospace'; g.textAlign = 'right';
  g.fillText(s.rms_db.toFixed(1) + ' dBFS  pk ' + s.peak_db.toFixed(1), w - 6, 54);
  g.textAlign = 'left';
}
async function tick() {
  try {
    const s = await (await fetch('/api/state')).json();
    state = s;
    draw(s);
    const p = s.params || {};
    const doa = s.doa_deg == null ? '--' : s.doa_deg.toFixed(0) + ' deg';
    $('telem').textContent =
      'AGC GAIN ' + s.agc_gain.toFixed(2) + '   DOA ' + doa + '\\n' +
      'MAXGAIN ' + (p.PP_AGCMAXGAIN||0) + '   TARGET LVL ' + (p.PP_AGCDESIREDLEVEL||0) +
      '   NS ' + (p.PP_MIN_NS||0) + '/' + (p.PP_MIN_NN||0) + '\\n' +
      'LAST: ' + s.last_action +
      (s.recording ? '   REC ' + s.rec_file : '');
    $('rec').textContent = s.recording ? 'STOP' : 'REC';
    $('rec').className = s.recording ? 'on' : '';
    $('vox').textContent = 'VOX ' + (s.vox ? 'ON' : 'OFF');
    $('vox').className = s.vox ? 'on' : '';
    $('scan').textContent = 'SCAN ' + (s.scan ? 'ON' : 'OFF');
    $('scan').className = s.scan ? 'on' : '';
    $('leds').textContent = 'LEDS ' + (s.leds_on ? 'ON' : 'OFF');
    $('leds').className = s.leds_on ? 'on' : '';
    const agcOn = (p.PP_AGCONOFF || 0) >= 1;
    $('agc').textContent = 'AGC ' + (agcOn ? 'ON' : 'OFF');
    $('agc').className = agcOn ? 'on' : '';
    if (!tsync && p.PP_AGCMAXGAIN !== undefined) {
      document.querySelectorAll('input[data-p]').forEach(el => {
        const v = p[el.dataset.p];
        if (v !== undefined) {
          el.value = v;
          $('v_' + el.dataset.p).textContent = v;
        }
      });
      tsync = true;
    }
    $('auto').textContent = 'AUTO ' + (s.auto ? 'ON' : 'OFF');
    $('auto').className = s.auto ? 'on' : '';
    $('lock').textContent = s.beam_lock
      ? 'LOCK' + (s.locked_az_deg == null ? '' : ' ' + s.locked_az_deg.toFixed(0) + '\\u00b0')
      : 'LOCK';
    $('lock').className = s.beam_lock ? 'on' : '';
    $('status').textContent =
      (s.device_ok ? 'CTRL ONLINE' : 'CTRL OFFLINE') + ' | ' +
      (s.audio_ok ? 'AUDIO OK' : 'NO AUDIO');
    $('status').style.color = (s.device_ok && s.audio_ok) ? '#ff2222' : '#666';
    const lg = $('log'), atEnd = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 4;
    lg.textContent = (s.log || []).join('\\n');
    if (atEnd) lg.scrollTop = lg.scrollHeight;
  } catch (e) {
    $('status').textContent = 'LINK LOST'; $('status').style.color = '#666';
  }
}
async function recs() {
  try {
    const list = await (await fetch('/api/recordings')).json();
    $('recs').innerHTML = list.slice(0, 12).map(r =>
      '<div><a href="/rec/' + r.name + '" download>' + r.name + '</a>' +
      '<audio controls preload="none" src="/rec/' + r.name + '"></audio>' +
      '<span>' + (r.size/1024|0) + ' kB</span></div>').join('');
  } catch (e) {}
}
setInterval(tick, 400); tick();
setInterval(recs, 5000); recs();
</script></body></html>
"""


def make_handler(eng):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, WEB_PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                with eng.lock:
                    s = {k: v for k, v in eng.state.items() if k != "params"}
                    s["params"] = dict(eng.state["params"])
                s["log"] = list(eng.log_lines)[-30:]
                self._send(200, s)
            elif self.path == "/api/recordings":
                out = []
                if os.path.isdir(REC_DIR):
                    for n in os.listdir(REC_DIR):
                        if n.lower().endswith(".wav"):
                            st = os.stat(os.path.join(REC_DIR, n))
                            out.append({"name": n, "size": st.st_size,
                                        "mtime": st.st_mtime})
                out.sort(key=lambda r: r["mtime"], reverse=True)
                self._send(200, out)
            elif self.path == "/live.pcm":
                sr = eng.sample_rate
                if not sr:
                    self._send(503, {"error": "no audio"})
                    return
                q = eng.add_listener()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/octet-stream")
                    self.send_header("X-Sample-Rate", str(sr))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    idle = 0
                    while idle < 3:
                        try:
                            self.wfile.write(q.get(timeout=5))
                            idle = 0
                        except queue.Empty:
                            idle += 1  # capture stalled; give up after 15 s
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    eng.remove_listener(q)
            elif self.path.startswith("/rec/"):
                name = os.path.basename(self.path[len("/rec/"):])
                path = os.path.join(REC_DIR, name)
                if name.lower().endswith(".wav") and os.path.isfile(path):
                    with open(path, "rb") as f:
                        self._send(200, f.read(), "audio/wav")
                else:
                    self._send(404, {"error": "not found"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.path == "/api/toggle" and body.get("what") in ("auto", "vox", "scan"):
                what = body["what"]
                with eng.lock:
                    eng.state[what] = not eng.state[what]
                    on = eng.state[what]
                eng.log(f"{what.upper()} {'engaged' if on else 'off'} (web).")
            elif self.path == "/api/rec":
                if body.get("on"):
                    eng.start_recording("manual")
                else:
                    eng.stop_recording()
            elif self.path == "/api/target":
                try:
                    v = max(-40.0, min(-15.0, float(body.get("dbfs"))))
                    with eng.lock:
                        eng.state["target_dbfs"] = v
                except (TypeError, ValueError):
                    pass
            elif self.path == "/api/param":
                name = body.get("name")
                spec = TUNABLES.get(name)
                if spec is None and name == "PP_AGCONOFF":
                    spec = ("AGC", 0, 1, 1)
                try:
                    v = float(body.get("value"))
                except (TypeError, ValueError):
                    spec = None
                if spec is not None:
                    v = max(spec[1], min(spec[2], v))
                    eng.set_param(name, v, "web tuning", "SET")
                else:
                    self._send(400, {"error": "bad param"})
                    return
            elif self.path == "/api/lock":
                if body.get("on"):
                    eng.lock_beam()
                else:
                    eng.unlock_beam()
            elif self.path == "/api/preset":
                name = str(body.get("name", "")).upper()
                if name in PRESETS:
                    eng.apply_preset(name)
                else:
                    self._send(400, {"error": "unknown preset"})
                    return
            elif self.path == "/api/leds":
                eng.set_leds(bool(body.get("on")))
            elif self.path == "/api/defaults":
                eng.apply_defaults()
            elif self.path == "/api/save":
                eng.xvf.save_to_flash()
                eng.log("Configuration saved to device flash (web).")
            else:
                self._send(404, {"error": "not found"})
                return
            self._send(200, {"ok": True})

    return Handler


def serve(eng, port):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(eng))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    eng.log(f"Web remote listening on port {port}.")
    return httpd


# ---------------------------------------------------------------- smoke test
def smoke_test():
    eng = Engine()
    ok_dev = eng.read_params()
    print(f"device_params_ok={ok_dev} "
          f"audio_candidates={eng._find_devices()}")
    stream, info = eng._open_stream()
    if stream is None:
        print(f"SMOKETEST FAIL: {info}")
        return 1
    print(f"using {info}")
    try:
        time.sleep(2.2)
        eng.start_recording("smoketest")
        time.sleep(1.0)
        eng.stop_recording()
    finally:
        stream.stop()
        stream.close()
    s = eng.state
    print(f"rms={s['rms_db']:.1f} dBFS peak={s['peak_db']:.1f} dBFS "
          f"noise_floor={s['noise_floor_db']:.1f} vad={s['vad']} "
          f"blocks={len(eng.blocks)}")
    path = os.path.join(REC_DIR, s["rec_file"])
    with wave.open(path, "rb") as w:
        dur = w.getnframes() / w.getframerate()
    print(f"recording: {s['rec_file']} {dur:.2f} s "
          f"({os.path.getsize(path)} bytes)")
    print("dry-run decision:", eng.autotune_step(dry_run=True))
    print("SMOKETEST OK" if ok_dev else "SMOKETEST PARTIAL (audio ok, control offline)")
    return 0


# ------------------------------------------------------------------------ UI
def main(serve_port=None, arm_vox=False):
    import tkinter as tk

    eng = Engine(arm_vox=arm_vox)
    eng.start()
    if arm_vox:
        eng.log("VOX armed at startup.")
    if serve_port:
        serve(eng, serve_port)

    root = tk.Tk()
    root.title("DEADPLUG // XVF3800 AUTOTUNE")
    root.configure(bg=BG)
    root.geometry("700x920")

    mono = ("Consolas", 10)
    mono_b = ("Consolas", 14, "bold")

    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(head, text="DEADPLUG // XVF3800 AUTOTUNE", bg=BG, fg=RED,
             font=("Consolas", 15, "bold")).pack(side="left")
    status_lbl = tk.Label(head, text="...", bg=BG, fg=GRAY, font=mono)
    status_lbl.pack(side="right")
    tk.Frame(root, bg=DIMRED, height=1).pack(fill="x", padx=14)

    # level meter
    meter = tk.Canvas(root, height=64, bg=PANEL, highlightthickness=1,
                      highlightbackground=DIMRED)
    meter.pack(fill="x", padx=14, pady=(10, 4))

    telem_lbl = tk.Label(root, text="", bg=BG, fg=TEXT, font=mono, anchor="w",
                         justify="left")
    telem_lbl.pack(fill="x", padx=14)

    # controls
    ctl = tk.Frame(root, bg=BG)
    ctl.pack(fill="x", padx=14, pady=8)

    def styled_btn(parent, text, cmd, fg=RED):
        return tk.Button(parent, text=text, command=cmd, bg="#120606", fg=fg,
                         activebackground="#2a0a0a", activeforeground=fg,
                         relief="solid", bd=1, font=mono, padx=12, pady=4,
                         highlightbackground=DIMRED, cursor="hand2")

    auto_btn = None

    def toggle_auto():
        with eng.lock:
            eng.state["auto"] = not eng.state["auto"]
            on = eng.state["auto"]
        auto_btn.configure(text=f"AUTO {'ON' if on else 'OFF'}",
                           fg=BG if on else RED,
                           bg=RED if on else "#120606")
        eng.log(f"Autotune {'engaged' if on else 'disengaged'}.")

    auto_btn = styled_btn(ctl, "AUTO OFF", toggle_auto)
    auto_btn.pack(side="left", padx=(0, 8))

    lock_btn = None

    def toggle_lock():
        with eng.lock:
            locked = eng.state["beam_lock"]
        target_fn = eng.unlock_beam if locked else eng.lock_beam
        threading.Thread(target=target_fn, daemon=True).start()

    lock_btn = styled_btn(ctl, "LOCK", toggle_lock)
    lock_btn.pack(side="left", padx=(0, 8))

    agc_btn = None

    def toggle_agc():
        with eng.lock:
            cur = eng.state["params"].get("PP_AGCONOFF", 1)
        threading.Thread(target=eng.set_param,
                         args=("PP_AGCONOFF", 0 if cur >= 1 else 1,
                               "manual toggle", "SET"),
                         daemon=True).start()

    agc_btn = styled_btn(ctl, "AGC", toggle_agc)
    agc_btn.pack(side="left", padx=(0, 8))
    styled_btn(ctl, "SAVE TO FLASH",
               lambda: (eng.xvf.save_to_flash(),
                        eng.log("Configuration saved to device flash.")),
               fg=AMBER).pack(side="left", padx=(0, 16))

    tk.Label(ctl, text="TARGET dBFS", bg=BG, fg=TEXT, font=mono).pack(side="left")
    target_var = tk.DoubleVar(value=TARGET_DBFS_DEFAULT)

    def on_target(_=None):
        with eng.lock:
            eng.state["target_dbfs"] = target_var.get()

    tk.Scale(ctl, from_=-40, to=-15, resolution=1, orient="horizontal",
             variable=target_var, command=on_target, bg=BG, fg=RED,
             troughcolor=PANEL, highlightthickness=0, font=mono,
             activebackground=RED, length=180).pack(side="left", padx=8)

    # recording controls
    rec_row = tk.Frame(root, bg=BG)
    rec_row.pack(fill="x", padx=14, pady=(0, 4))

    rec_btn = None

    def toggle_rec():
        with eng.lock:
            rec = eng.state["recording"]
        if rec:
            eng.stop_recording()
        else:
            eng.start_recording("manual")

    rec_btn = styled_btn(rec_row, "REC", toggle_rec)
    rec_btn.pack(side="left", padx=(0, 8))

    vox_btn = None

    def toggle_vox():
        with eng.lock:
            eng.state["vox"] = not eng.state["vox"]
            on = eng.state["vox"]
        vox_btn.configure(text=f"VOX {'ON' if on else 'OFF'}",
                          fg=BG if on else RED,
                          bg=RED if on else "#120606")
        eng.log(f"VOX {'armed: recording on voice detect' if on else 'off'}.")

    vox_btn = styled_btn(rec_row, "VOX OFF", toggle_vox)
    vox_btn.pack(side="left", padx=(0, 8))

    scan_btn = None

    def toggle_scan():
        with eng.lock:
            eng.state["scan"] = not eng.state["scan"]

    scan_btn = styled_btn(rec_row, "SCAN", toggle_scan)
    scan_btn.pack(side="left", padx=(0, 8))

    leds_btn = None

    def toggle_leds():
        with eng.lock:
            cur = eng.state["leds_on"]
        threading.Thread(target=eng.set_leds, args=(not cur,),
                         daemon=True).start()

    leds_btn = styled_btn(rec_row, "LEDS", toggle_leds)
    leds_btn.pack(side="left", padx=(0, 8))
    def open_rec_dir():
        os.makedirs(REC_DIR, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(REC_DIR)
        else:
            subprocess.Popen(["xdg-open", REC_DIR])

    styled_btn(rec_row, "RECORDINGS", open_rec_dir,
               fg=TEXT).pack(side="left", padx=(0, 12))
    rec_lbl = tk.Label(rec_row, text="rec idle", bg=BG, fg=GRAY, font=mono)
    rec_lbl.pack(side="left")

    # tuning sliders
    tune_frame = tk.Frame(root, bg=BG)
    tune_frame.pack(fill="x", padx=14, pady=(2, 0))
    synced = {"done": False}

    tune_head = tk.Frame(tune_frame, bg=BG)
    tune_head.pack(fill="x")
    tk.Label(tune_head, text="TUNING", bg=BG, fg=RED,
             font=("Consolas", 11, "bold")).pack(side="left")

    def apply_preset_ui(name):
        def worker():
            eng.apply_preset(name)
            synced["done"] = False  # re-sync sliders from device state
        threading.Thread(target=worker, daemon=True).start()

    for pname in reversed(list(PRESETS)):
        styled_btn(tune_head, pname,
                   lambda n=pname: apply_preset_ui(n)).pack(side="right")

    scales = {}
    for name, (label, lo, hi, step) in TUNABLES.items():
        sc = tk.Scale(tune_frame, from_=lo, to=hi, resolution=step,
                      orient="horizontal", label=label,
                      font=("Consolas", 8), bg=BG, fg=RED,
                      troughcolor=PANEL, highlightthickness=0,
                      activebackground=RED, bd=0)
        sc.pack(fill="x")
        sc.bind("<ButtonRelease-1>",
                lambda e, n=name: threading.Thread(
                    target=eng.set_param,
                    args=(n, scales[n].get(), "manual", "SET"),
                    daemon=True).start())
        scales[name] = sc

    # log
    log_box = tk.Text(root, bg=PANEL, fg="#9a3d3d", font=("Consolas", 9),
                      relief="solid", bd=1, highlightbackground=DIMRED,
                      state="disabled", wrap="word")
    log_box.pack(fill="both", expand=True, padx=14, pady=(4, 14))

    def db_to_x(db, width):
        return max(0, min(width, (db + 70.0) / 70.0 * width))

    def refresh():
        with eng.lock:
            s = dict(eng.state)
        w = meter.winfo_width() or 600
        meter.delete("all")
        meter.create_rectangle(0, 8, db_to_x(s["rms_db"], w), 30,
                               fill=RED, width=0)
        px = db_to_x(s["peak_db"], w)
        meter.create_rectangle(px - 2, 8, px + 2, 30, fill="#ff8888", width=0)
        nx = db_to_x(s["noise_floor_db"], w)
        meter.create_line(nx, 4, nx, 34, fill=GRAY)
        tx = db_to_x(s["target_dbfs"], w)
        meter.create_line(tx, 4, tx, 34, fill=AMBER, dash=(3, 2))
        meter.create_text(6, 48, anchor="w", fill=RED if s["vad"] else GRAY,
                          font=mono_b, text="VOICE" if s["vad"] else "·····")
        meter.create_text(w - 6, 48, anchor="e", fill=TEXT, font=mono,
                          text=f"{s['rms_db']:6.1f} dBFS  pk {s['peak_db']:6.1f}")

        doa = f"{s['doa_deg']:.0f} deg" if s["doa_deg"] is not None else "--"
        p = s["params"]
        telem_lbl.configure(text=(
            f"AGC GAIN {s['agc_gain']:7.2f}   DOA {doa:>8}   "
            f"BEAM E {s['beam_energy']:.3g}\n"
            f"MAXGAIN {p.get('PP_AGCMAXGAIN', 0):g}   "
            f"TARGET LVL {p.get('PP_AGCDESIREDLEVEL', 0):g}   "
            f"NS {p.get('PP_MIN_NS', 0):g}/{p.get('PP_MIN_NN', 0):g}   "
            f"LAST: {s['last_action']}"))

        scan_btn.configure(text=f"SCAN {'ON' if s['scan'] else 'OFF'}",
                           fg=BG if s["scan"] else RED,
                           bg=RED if s["scan"] else "#120606")

        leds_btn.configure(text=f"LEDS {'ON' if s['leds_on'] else 'OFF'}",
                           fg=BG if s["leds_on"] else RED,
                           bg=RED if s["leds_on"] else "#120606")

        agc_on = p.get("PP_AGCONOFF", 1) >= 1
        agc_btn.configure(text=f"AGC {'ON' if agc_on else 'OFF'}",
                          fg=BG if agc_on else RED,
                          bg=RED if agc_on else "#120606")
        if not synced["done"] and p:
            for n in TUNABLES:
                if n in p:
                    scales[n].set(p[n])
            synced["done"] = True

        if s["beam_lock"]:
            az_txt = ("" if s["locked_az_deg"] is None
                      else f" {s['locked_az_deg']:.0f}°")
            lock_btn.configure(text=f"LOCK{az_txt}", fg=BG, bg=RED)
        else:
            lock_btn.configure(text="LOCK", fg=RED, bg="#120606")

        if s["recording"]:
            elapsed = time.monotonic() - s["rec_started"] + PREROLL_S
            rec_lbl.configure(
                text=f"REC ● {s['rec_file']}  {elapsed:.0f} s"
                     f"  [{s['rec_source']}]", fg=RED)
            rec_btn.configure(text="STOP", fg=BG, bg=RED)
        else:
            rec_lbl.configure(text="rec idle", fg=GRAY)
            rec_btn.configure(text="REC", fg=RED, bg="#120606")

        dev = "CTRL ONLINE" if s["device_ok"] else "CTRL OFFLINE"
        aud = "AUDIO OK" if s["audio_ok"] else "NO AUDIO"
        status_lbl.configure(
            text=f"{dev} | {aud}",
            fg=RED if s["device_ok"] and s["audio_ok"] else GRAY)

        try:
            while True:
                line = eng.log_q.get_nowait()
                log_box.configure(state="normal")
                log_box.insert("end", line + "\n")
                log_box.see("end")
                log_box.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, refresh)

    refresh()

    def on_close():
        eng.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main_headless(port, arm_vox=False):
    eng = Engine(arm_vox=arm_vox)
    eng.start()
    if arm_vox:
        eng.log("VOX armed at startup.")
    serve(eng, port)
    try:
        while True:
            try:
                print(eng.log_q.get(timeout=1.0), flush=True)
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        eng.stop()


if __name__ == "__main__":
    if os.environ.get("XVF_AUTOTUNE_SMOKETEST") == "1":
        sys.exit(smoke_test())
    ap = argparse.ArgumentParser(description="DEADPLUG // XVF3800 AUTOTUNE")
    ap.add_argument("--headless", action="store_true",
                    help="no GUI; run engine + web remote (for Raspberry Pi)")
    ap.add_argument("--serve", type=int, nargs="?", const=8380, default=None,
                    metavar="PORT", help="serve the web remote (default 8380)")
    ap.add_argument("--vox", action="store_true",
                    help="arm VOX (record on voice) at startup")
    args = ap.parse_args()
    if args.headless:
        main_headless(args.serve or 8380, arm_vox=args.vox)
    else:
        main(serve_port=args.serve, arm_vox=args.vox)
