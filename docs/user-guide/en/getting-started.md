# Getting Started

## 1. Install

Download the installer from https://github.com/JF9SOM/fbsat59/releases/latest

- **Windows**: run `FBSAT59-Setup.exe`. Windows 8.1 or later is required
  (plain Windows 8 is not supported and fails with a missing-DLL error).
- **macOS**: open `FBSAT59.dmg` and drag the app to Applications.
- **Linux**: download the AppImage, `chmod +x FBSAT59-*.AppImage`, and run it.
  For USB radios add yourself to the `dialout` group and log in again.

Later versions can be installed from Help > Check for Updates.

## 2. First launch

1. A splash screen appears; the first start takes longer because data is downloaded.
2. **File > Set QTH...** and enter your location. You can use latitude/longitude/elevation
   or the **Grid Locator** tab (Maidenhead). Without a correct QTH, pass times are wrong.
3. TLE and SatNOGS transponder data download automatically. If the satellite list is empty,
   wait a few minutes with an Internet connection, then try Satellite > Update TLE.
4. View > Time Zone switches between UTC and Local time. View > Language changes the
   language (restart required).

## 3. Connect a radio (Hamlib)

1. **Radio > Rig Settings...** has one tab per rig (Rig 1, Rig 2) plus SDR Settings, Sound Card and PTT.
   In the Rig 1 tab choose the connection type — **Direct (Hamlib built-in)**, **NET (rigctld compatible)**
   for an existing rigctld, or **SDR** — then select your radio model, serial port and baud rate
   (CI-V address for Icom rigs).
2. Press OK. Closing the dialog keeps an existing connection.
3. In the Radio Control tab press **Connect Rig 1**. The status shows a green
   "Connected: <radio model>" on success (cyan "SDR: Connected" for an SDR) and a red "Not connected" on failure.
4. Select a satellite, then a transponder. Frequency, mode and tone are set and
   Doppler correction follows. Use the Cycle dropdown to change how often the rig is updated.
5. Rig 2 works the same way (for example an SDR as Rig 2 for receiving).

Rotator: **Radio > Rotator Settings...**, then **Connect Rotator** in Radio Control.
A red "Not connected" means the rotator does not answer (check power, cable, port).

## 4. Use an SDR

1. Plug in the SDR (RTL-SDR, HackRF, ...).
2. **Radio > Rig Settings... > SDR Settings**, press **Enumerate**, select the device,
   set sample rate and gain, and assign it to Rig 1 or Rig 2.
3. Connect it from Radio Control; the **SDR Control** tab becomes active
   (spectrum, demodulation, IQ recording).

Platform notes:

- **Windows**: RTL-SDR and HackRF need a one-time **WinUSB driver via Zadig**
  (https://zadig.akeo.ie/): plug in the device, Zadig > Options > List All Devices,
  select the device (RTL-SDR: Bulk-In, Interface 0 / HackRF: HackRF One), driver
  **WinUSB**, Install Driver, then restart FBSAT59. Do not choose libusbK.
  A device can appear in the list without the driver; it only fails when opened.
  See Help > SDR Device Installation. Airspy, Airspy HF+ and ADALM-Pluto are
  not supported on Windows.
- **macOS**: RTL-SDR, HackRF, Airspy and Remote SDR are bundled; nothing to install.
- **Linux**: install SoapySDR modules, e.g.
  `sudo apt install python3-soapysdr soapysdr-module-rtlsdr soapysdr-module-hackrf`.
- SDRplay and ADALM-Pluto are not bundled (extra software required; see README).

## 5. Digital modes

Open from the **Communications** menu; each opens as a closable tab.

- Some need an extra component, installed from the **Help** menu:
  ft8lib (FT4), FT4 Enhanced Decoder, Q65 Library, Direwolf (APRS/Telemetry with
  sound card), SatDump (METEOR/HRPT), gr-satellites (Telemetry, 330+ satellites),
  CW Model (CW Decoder).
- Audio from a radio uses the input/output chosen in the **Sound Card** tab of Rig Settings.

## 6. Autotrack/Record

The **Autotrack/Record** menu opens a dialog to build lists of satellites and
transponders, enable automatic tracking (rig and rotator connect at AOS and disconnect
at LOS), IQ recording, METEOR/HRPT reception, and a start/stop timer. Choose the
**same satellite** you want to receive: AOS/LOS timing comes from it.
Untick "Use Rotator" if you have none.

## 7. Phone or tablet access

The status bar shows a URL (and a QR-code button). Open it in a browser on the
same LAN. Port 8080 must not be blocked by a firewall.
