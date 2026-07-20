# DEADPLUG // XVF3800

Whisper-tuned voice capture rig for the **Seeed reSpeaker XVF3800 USB 4-Mic
Array**. Records from the device, watches voice levels, and adjusts the
XVF3800's on-chip DSP parameters on the fly to tune in quiet voices —
whispers included.

Runs on **Windows** (GUI) and **Raspberry Pi** (headless + phone web remote).

## What's in here

| File | What it is |
|---|---|
| `autotune.py` | The main app: capture, VAD, WAV recording (manual + VOX), closed-loop tuning, tkinter GUI, and a built-in web remote |
| `xvf-tuner.ps1` | Windows-only WPF slider panel for manual parameter tweaking |
| `xvf_usb.py` | Native USB control backend (pyusb) — no `xvf_host` binary needed; also a standalone CLI |
| `get_xvf_host.sh` / `.ps1` | Fetch the platform-matched `xvf_host` control binary from [Seeed's repo](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY) (optional fallback) |
| `pi/setup.sh` | One-shot Raspberry Pi installer (deps, udev rule, systemd service) |

## Device control backends

The apps talk to the XVF3800's USB control interface two ways, preferring
the first:

1. **Native USB** (`xvf_usb.py`, via pyusb) — works on any platform with
   libusb, including **32-bit ARM (Pi Zero W, armv6l)** for which no
   prebuilt `xvf_host` exists. Also avoids a subprocess per control call.
2. **`xvf_host` binary** — Seeed's prebuilt tool, fetched by
   `get_xvf_host.(sh|ps1)` (win32 / linux_x86_64 / rpi_64bit / mac_arm64).

`python xvf_usb.py PP_AGCMAXGAIN` reads a parameter from the CLI;
add a value to write it.

## Quickstart — Windows

```powershell
pip install -r requirements.txt
python autotune.py        # GUI
python autotune.py --serve 8380   # GUI + phone web remote
```

`xvf_host.exe` needs the x86 VC++ 2015–2022 redistributable
([vc_redist.x86.exe](https://aka.ms/vs/17/release/vc_redist.x86.exe)).

## Quickstart — Raspberry Pi (64-bit OS)

```bash
git clone <this repo> && cd <repo>
./pi/setup.sh
```

That installs everything, starts a systemd service
(`journalctl -u xvf-autotune -f` for logs), and prints the web remote URL.
Open it on your phone (same network) and Add to Home Screen — REC/VOX/AUTO
buttons, live meter, recordings with in-browser playback.

**PipeWire desktops (Bookworm etc.):** if capture repeatedly stalls with
`Input/output error` while WirePlumber runs, it's fighting the app for the
device. `pi/setup.sh` handles this automatically; to apply it by hand later,
run `./pi/fix-pipewire.sh` (as your login user, not sudo) — it tells
WirePlumber to leave the XVF3800 alone so the app's raw-ALSA capture works.

**Pi Zero W (armv6) notes:** works via the native USB backend (no prebuilt
`xvf_host` exists for armv6 — `pi/setup.sh` handles this automatically).
32-bit Raspberry Pi OS pulls numpy from piwheels. Two caveats: the single
micro-USB OTG port is marginal for powering the array — use a good 5 V/2 A+
supply or a powered hub — and the single ARM11 core means the web remote is
usable but not snappy. A **Pi Zero 2 W** (same footprint, aarch64) is the
smoother choice if you're buying new.

## The whisper profile

Whispers are ~30 dB quieter than speech and spectrally noise-like, so the
XVF3800's stock settings both under-amplify them and actively suppress them.
Profile that works (stock values in parens):

| Param | Value | Why |
|---|---|---|
| `PP_AGCMAXGAIN` | 160 (64) | headroom to lift a whisper to speech level |
| `PP_AGCDESIREDLEVEL` | 0.007 (0.0045) | hotter AGC target |
| `PP_AGCFASTTIME` | 0.2 (0.1) | gentler gain back-off after peaks |
| `PP_MIN_NS` | 0.35 (0.15) | less stationary noise suppression |
| `PP_MIN_NN` | 0.6 (0.51) | less non-stationary suppression |

Values above 160/0.008 max gain/target tend to slam the output limiter on
whispered plosives and the AGC abruptly collapses — if you hear pickup that
cuts out, lower gain, don't raise it.

**Settings are volatile.** The device silently reverts to stock every time it
re-enumerates on USB (which happens more often than you'd think). Once a
profile works, persist it: `xvf_host save_configuration 1`, or the
SAVE TO FLASH button in either app. A flashed profile also travels with the
device — plugged into a USB-C phone it records with the tuned DSP, no app
needed.

## Autotune loop

With AUTO on, every 3 s the app makes at most one bounded adjustment based on
what it recorded: raises the AGC target when voices land below the target
loudness, escalates max gain only when the AGC is pegged, and backs gain off
when it sees the overshoot signature (near-full-scale peak followed by an
abrupt level collapse). Deliberately slow so it doesn't fight the device's
own AGC. It also re-enables `PP_AGCONOFF` if something switched it off.

## Beam locking

The 4-mic array continuously estimates where the voice is coming from
(`AEC_AZIMUTH_VALUES`). The **LOCK** button (GUI and web remote) averages the
voice bearings heard over the last 15 s and fixes both focused beams on that
direction (`AEC_FIXEDBEAMSAZIMUTH_VALUES` + `AEC_FIXEDBEAMSONOFF`), with beam
gating disabled so a quiet whisper is never muted. The array then physically
focuses on the speaker instead of steering toward whatever is loudest —
useful when a whisperer competes with a TV or fan. Speak (or whisper) first
so there's a bearing to lock to; LOCK again releases back to auto tracking.

## Recording

Timestamped mono 48 kHz WAVs in `recordings/`. Every recording includes a 2 s
pre-roll so the first syllable is never clipped. VOX mode records
automatically per-utterance with a 2.5 s hangover.

## Smoke test

```
XVF_AUTOTUNE_SMOKETEST=1 python autotune.py
```

Headless: verifies control interface, opens capture, cuts a 3 s WAV, and
prints a dry-run tuning decision.

## Notes

- The web remote has no auth — LAN use only.
