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
| `get_xvf_host.sh` / `.ps1` | Fetch the platform-matched `xvf_host` control binary from [Seeed's repo](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY) |
| `pi/setup.sh` | One-shot Raspberry Pi installer (deps, udev rule, systemd service) |

## Quickstart — Windows

```powershell
.\get_xvf_host.ps1        # fetch the control tool (needs git)
pip install numpy sounddevice
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
- `AEC_AZIMUTH_VALUES` / `AEC_SPENERGY_VALUES` telemetry (voice direction,
  per-beam energy) is displayed; beam-locking onto the detected direction is
  the planned next phase.
