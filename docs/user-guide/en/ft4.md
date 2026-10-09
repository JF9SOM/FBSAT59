# FT4

FT4 is a fast digital mode (7.5-second periods) used on amateur satellites such as RS-44,
JO-97 and MO-122. Open it from **Communications > FT4**. It auto-opens when you select an
FT4 transponder. FT4 encoding/decoding is built in; WSJT-X is not required.

## Before you start

- The **ft8lib** component must be installed (Help > ft8lib Installation…). Without it
  the tab shows "ft8lib is not installed" and FT4 TX/RX is disabled.
- Optional: Help > FT4 Enhanced Decoder Installation… adds a stronger decoder (receive only).
- Set **My Call** and **Grid** in the tab. TX is refused until My Call is set.
- Choose the audio input: **Rig Soundcard** (set in Radio > Rig Settings > Sound Card) or **SDR**.
- Connect Rig 1 and select the satellite and an FT4 transponder (frequencies are set for you
  with Doppler correction). PTT is keyed through the rig.

## Operating

1. **Decoded Messages** lists what is received each period (time, dB, DT, frequency, message).
   Double-click a station to start a reply; the TX message is prepared automatically.
2. **TX Slot**: Even or Odd period for your transmissions (usually the opposite of the station you answer).
3. **TX Audio** (Hz): the audio frequency of your transmission.
4. **TX Enable** arms transmission; the app transmits in the next period. **Halt TX** stops at once.
   **Call CQ** prepares a CQ message.
5. **Auto-progress** (Yes/No): when another station calls you and no QSO is running, the app
   starts the QSO and prepares the reply. Transmitting still needs TX Enable.
6. The QSO is logged automatically when RR73 is received, and the app sends 73. **Log QSO** saves
   manually; **Clear** resets the current QSO.
7. **Export ADIF…** writes the log in a LoTW-compatible format.

## Levels and tests

- **TX Level** (dB below full scale): lower it if the rig's ALC reacts or the audio is distorted.
  Many rigs/sound cards need about 20 dB of reduction.
- **RX Level** shows the input level in dBFS; keep it from clipping.
- **Tune** transmits a steady test tone to set levels. Press it again to stop.
  It cannot be used while a transmission is running.

## Waterfall

The Waterfall window shows the received audio. Click it to set the TX audio frequency.
It opens above the main window the first time and then stays where you put it.

## ADC (Audio Doppler Correction)

Satellite Doppler shift changes faster than a rig's frequency can be updated. At high
elevations (for example around the closest approach of RS-44) the error within one 7.5-second
period is large enough that FT4 cannot decode. ADC moves part of the correction from the
radio into the audio, in software. The ADC row in the FT4 tab has two checkboxes:

- **TX**: during each transmission the uplink frequency is held fixed and the Doppler drift is
  followed by moving the transmit audio tone instead. Turn it off to send a fixed tone like WSJT-X.
- **RX** (experimental, off by default): the rig is tuned once per period instead of every
  second, and the received audio is shifted by the remaining difference before decoding.
  The waterfall then shows the corrected audio (a straight line means the correction works).
  If the app says RX cannot be enabled in your setup, leave it off.

Use ADC when strong Doppler rate makes decoding fail. At very high Doppler rates
(about 55 Hz/s or more) it has limits. Details are logged in `ft4_decode.log`
(lines starting with `adc_rx`) next to `fbsat59.log`.

## Troubleshooting FT4

- **Nothing decodes**: check the audio input level (RX Level), the radio's data/USB mode, that
  the PC clock is accurate (DT should be small), and that ft8lib is installed.
- **ALC / distorted TX**: lower TX Level.
- **PTT fails**: check the Rig 1 connection ("PTT command failed — check Rig 1 connection").
  If the radio may still be transmitting, check the radio itself.
- **Sound card in use**: another tab is using the same sound card; close it.
- For problems that remain, attach `fbsat59.log` and `ft4_decode.log` to a GitHub Issue.
