"""Foresail-1p (NORAD 66778) Skylink digipeater: receive and transmit.

See docs/communications.md ("Foresail-1p digipeater"). Modules: ``skylink`` (Skylink
frames and the AX.25 UI frames of the repeater channel), ``rx`` (SDR GMSK receiver),
``tx`` (uplink waveform), ``tdd`` (the satellite's transmit windows).
"""
