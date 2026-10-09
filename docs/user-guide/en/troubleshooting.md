# Troubleshooting

First collect: app version (Help > About), OS, radio/SDR model, what you did, what you
expected and what happened, and the log file `fbsat59.log` (locations in
[index.md](index.md)). The status bar at the bottom shows short error messages,
for example `RIG: ...`.

## The app will not start

- **Windows, "DLL not found"**: if the DLL is `api-ms-win-crt-*.dll`, the OS is older
  than Windows 8.1 and cannot run FBSAT59 — upgrade Windows. If the DLL is
  `VCRUNTIME140.dll` or `MSVCP140.dll`, install the Microsoft Visual C++ Redistributable (x64).
- **Linux AppImage on non-Ubuntu distributions**: keyboard input may not work in text
  fields (known problem). Run from the terminal and send the output with your report.

## Satellite list or TLE is empty / old

- Check the Internet connection, wait a few minutes after first start, then try
  Satellite > Update TLE. Help > Auto Fetch Rules shows the schedule.
- If your connection works for other sites but CelesTrak/SatNOGS time out, the
  server may be blocking your IP after too many requests. Try again later or from a
  different network (for example a mobile hotspot).
- A satellite with no TLE cannot be tracked. You can add one with Satellite > Add Manual TLE...
  (the app warns when it gets old).

## A satellite has no transponders

Run Satellite > Fetch Transmitter Database. If SatNOGS has no entry, add one with
Satellite > Add Transmitter... Manual entries are never overwritten by automatic syncs.

## Pass times or positions look wrong

Check File > Set QTH (latitude sign, longitude sign, grid) and View > Time Zone
(UTC or Local), and that the PC clock is correct.

## The radio does not connect or does not follow Doppler

- Red "Not connected": check cable, power, correct serial port, baud rate matching the
  radio's CAT setting, and CI-V address (Icom). Close other software using the port
  (WSJT-X, rigctld, ...).
- Pick the radio model carefully (for example FTX-1 and FT-991A are different models).
- After a rig disconnect, press Connect Rig again. Closing Rig Settings with OK keeps the connection.
- Satellites without an uplink (telemetry-only) may not set the rig frequency correctly
  on some radios (known limitation).
- During FT4 transmit periods the app deliberately sends no CAT commands to the rig.

## SDR is not detected or Connect does nothing

- **Windows**: apply the WinUSB driver with Zadig (see getting-started.md) and restart.
  Being listed in Enumerate does not prove the driver is correct. Do not use libusbK.
- Try another USB port or cable and avoid unpowered hubs; check the cable first
  when detection is intermittent.
- **Linux**: install SoapySDR modules and the udev rule from `scripts/99-fbsat59.rules`.
- Open Help > SDR Device Installation for a scan and guidance.

## Rotator shows red "Not connected"

The rotator does not answer. Check power, cable, port and model. The software
cannot detect a rotator that is switched off before connecting; power it on first.
The first move after connecting may take a while because the rotator catches up to
the target; this is normal.

## A digital mode tab shows "not installed" or does not decode

- Install the component from the Help menu (ft8lib, Q65 Library, Direwolf, SatDump,
  gr-satellites, CW Model) and restart if asked.
- FT4/APRS/CW with a rig need audio routed to the sound card selected in Rig Settings
  and a suitable signal level; check the radio's audio output and data/packet mode settings.
- METEOR/HRPT: SatDump must be installed (Help > SatDump...). Set the gain manually;
  automatic gain (AGC) is not offered because it gives false locks. Weak signal or the
  wrong satellite in Autotrack gives no image.
- Windows: if ft8lib installation fails with a "locked file" error, close the FT4 tab
  and restart the app, then reinstall.

## Language, display and misc.

- Some interface strings remain in English; this is intentional.
- GNOME on Wayland ignores requested window positions; this cannot be fixed by the app.

## How to report a problem

1. Reproduce it once if you can.
2. Open https://github.com/JF9SOM/fbsat59/issues and create an Issue.
3. Include: version, OS, radio/SDR/rotator models, steps, expected vs actual behavior,
   and the relevant part of `fbsat59.log`. Do not post passwords or personal data.

When the AI Help finishes without solving the problem, ask it to write this
summary in a form ready to paste into an Issue.
