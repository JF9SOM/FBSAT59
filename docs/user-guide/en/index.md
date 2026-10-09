# FBSAT59 User Guide — Overview

> This guide is written for end users and is the knowledge base for the in-app
> "AI Help". If something is not covered here, say so honestly instead of guessing,
> and suggest opening an Issue: https://github.com/JF9SOM/fbsat59/issues

## What FBSAT59 is

FBSAT59 is satellite tracking and communications software for radio amateurs
(a modern successor to GPredict). It runs on Windows (8.1 or later; 10/11 recommended),
macOS and Linux. It provides:

- Satellite tracking: world map, sky radar, pass chart, upcoming passes, Dashboard.
- Automatic TLE and transponder (SatNOGS) updates in the background.
- Radio control via built-in Hamlib (no separate rigctld needed): Doppler correction of
  frequency, mode and CTCSS tone, and rotator control.
- SDR support (RTL-SDR, HackRF and others): spectrum, demodulation, IQ recording.
- Digital modes in the **Communications** menu: APRS, Message Box/Digipeater, FT4, Q65,
  Telemetry, CW Decoder, SSTV/SSDV, METEOR/HRPT.
- **Autotrack/Record**: automatic tracking, rig/rotator connection and recording from AOS to LOS.
- Phone/tablet access: open the URL shown in the status bar (port 8080) from a browser on the same LAN.

## Menu map

| Menu | Main contents |
|---|---|
| File | Set QTH (your location), General Settings, Exit |
| Satellite | Add/Edit/Delete Transmitter, Hide Satellite, Manual TLE, Update TLE, Fetch Transmitter Database |
| Radio | Rig Settings, Rotator Settings |
| Communications | APRS, Message Box/Digipeater, FT4, Q65, Telemetry, CW Decoder, SSTV/SSDV, METEOR/HRPT |
| Autotrack/Record | Opens the Autotrack/Record dialog |
| Tools | Your registered web sites (editable in General Settings > Tools) |
| View | Language (restart required), Time Zone (UTC/Local), Appearance |
| Help | AI Help (ask Claude about FBSAT59), Auto Fetch Rules, Check for Updates, installers for SDR drivers, Hamlib update, ft8lib, Direwolf, SatDump, gr-satellites, CW model, About, GitHub |

## Typical workflow

1. Set your location (File > Set QTH).
2. Wait for the first TLE/transponder download (automatic).
3. Pick a satellite in the list; the map, radar and passes update.
4. Radio > Rig Settings to choose your radio (or SDR) and port; then press **Connect Rig 1**
   in Radio Control. Pick a transponder; Doppler correction starts.
5. Optional: Radio > Rotator Settings, then **Connect Rotator**.
6. For unattended passes use Autotrack/Record.

## Where things are stored

- Log file `fbsat59.log` (attach it when reporting a problem):
  - macOS: `~/Library/Logs/fbsat59/fbsat59.log`
  - Windows: `%LOCALAPPDATA%\fbsat59\fbsat59\Logs\fbsat59.log`
  - Linux: `~/.local/state/fbsat59/log/fbsat59.log` (older setups: `~/.cache/fbsat59/log/`)
- Database (settings, satellites, TLE): `fbsat59.db` in the per-user data folder
  (macOS `~/Library/Application Support/fbsat59`, Windows `%LOCALAPPDATA%\fbsat59\fbsat59`,
  Linux `~/.local/share/fbsat59`).

## Automatic updates (you normally need no manual action)

| Data | Interval |
|---|---|
| Space stations (ISS...) | 1 hour |
| Amateur satellites | 2 hours |
| CubeSats | 4 hours |
| Weather satellites | 6 hours |
| Earth observation / science | 12 hours |
| AMSAT operational status | 24 hours |
| SatNOGS transmitter database | refreshed at startup when older than 7 days |

Help > Auto Fetch Rules shows the same table. Use Satellite > Update TLE or
Satellite > Fetch Transmitter Database only if you need fresh data right now
(for example, a newly launched satellite).

## Related guides

- [getting-started.md](getting-started.md) — installation, first run, rig and SDR setup
- [ft4.md](ft4.md) — FT4 operation and ADC (Audio Doppler Correction)
- [troubleshooting.md](troubleshooting.md) — symptom-based fixes and how to report a problem
