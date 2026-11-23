"""Receive-only KISS/AX.25 parsing and HERC sequence preflight.

No hardware, networking, file writes, or actuator calls occur in this module.
The caller must explicitly configure the confirmed sender and allowed SSIDs.
Source filtering is not authentication. Dire Wolf is responsible for the radio
frame check; KISS delivers an AX.25 frame without its on-air flags or FCS.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Collection

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD
_CALLSIGN = re.compile(r"[A-Z0-9]{1,6}\Z")
_WHITESPACE = b" \t\r\n\v\f"
_ALPHANUMERIC = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class ProtocolError(ValueError):
    """An input frame or required configuration is unacceptable for execution."""


@dataclass(frozen=True)
class ParsedSequence:
    """A completely validated sequence, ready for an independently armed caller.

    ``tokens`` contains only operational B-H tokens, in received order; AAA and
    ZZZ are omitted. ``source`` always includes the SSID, including ``-0``.
    ``information`` and ``raw_frame`` retain exact bytes for the evidence log.
    Pan preflight assumes relative B/C turns of 30 degrees and the caller's
    declared initial angle. Those are configurable team assumptions, not a
    claim about NASA's final sequence format or permitted travel.
    """

    source: str
    source_callsign: str
    source_ssid: int
    information: bytes
    tokens: tuple[str, ...]
    initial_pan: float
    final_pan: float
    raw_frame: bytes


class KissDecoder:
    """Incrementally extract unescaped AX.25 data frames for one KISS port.

    Feed arbitrary TCP chunks, including partial or multiple frames. Only KISS
    command 0 (data) on ``port`` is emitted; the command/port byte is removed.
    ``max_frame_bytes`` bounds the decoded frame including that type byte.
    Malformed escapes and oversized frames are discarded until the next FEND.
    This deliberately rejects an entire malformed frame rather than attempting
    the permissive recovery described by the original KISS specification.
    Call ``reset`` on disconnect so data from different connections cannot join.
    """

    def __init__(self, port: int = 0, max_frame_bytes: int = 512) -> None:
        if type(port) is not int or not 0 <= port <= 15:
            raise ValueError("KISS port must be an integer from 0 through 15")
        if type(max_frame_bytes) is not int or max_frame_bytes < 2:
            raise ValueError("max_frame_bytes must be an integer of at least 2")
        self.port = port
        self.max_frame_bytes = max_frame_bytes
        self.discarded_frames = 0
        self.ignored_frames = 0
        self.reset()

    @property
    def buffered_bytes(self) -> int:
        """Decoded bytes retained for an incomplete frame (bounded)."""
        return len(self._buffer)

    def reset(self) -> None:
        """Discard incomplete connection data; retain diagnostic counters."""
        self._buffer = bytearray()
        self._started = False
        self._escaped = False
        self._discarding = False

    def _reject(self) -> None:
        self.discarded_frames += 1
        self._buffer.clear()
        self._escaped = False
        self._discarding = True

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[bytes]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("KISS input must be bytes-like")
        frames: list[bytes] = []
        for byte in bytes(chunk):
            if byte == FEND:
                if self._started and not self._discarding:
                    if self._escaped:
                        self.discarded_frames += 1
                    elif self._buffer:
                        type_byte = self._buffer[0]
                        if (type_byte & 0x0F) == 0 and (type_byte >> 4) == self.port:
                            if len(self._buffer) > 1:
                                frames.append(bytes(self._buffer[1:]))
                            else:
                                self.discarded_frames += 1
                        else:
                            self.ignored_frames += 1
                self._buffer.clear()
                self._started = True
                self._escaped = False
                self._discarding = False
                continue
            if not self._started or self._discarding:
                continue
            if self._escaped:
                self._escaped = False
                if byte == TFEND:
                    byte = FEND
                elif byte == TFESC:
                    byte = FESC
                else:
                    self._reject()
                    continue
            elif byte == FESC:
                self._escaped = True
                continue
            if len(self._buffer) >= self.max_frame_bytes:
                self._reject()
                continue
            self._buffer.append(byte)
        return frames


def _address(raw: bytes) -> tuple[str, int, bool]:
    if len(raw) != 7:
        raise ProtocolError("Truncated AX.25 address")
    if any(byte & 1 for byte in raw[:6]):
        raise ProtocolError("AX.25 callsign characters must be shifted left one bit")
    text = "".join(chr(byte >> 1) for byte in raw[:6])
    callsign = text.rstrip(" ")
    if not _CALLSIGN.fullmatch(callsign) or text != callsign.ljust(6):
        raise ProtocolError("Invalid AX.25 callsign or address padding")
    if raw[6] & 0x60 != 0x60:
        raise ProtocolError("Invalid AX.25 reserved address bits")
    return callsign, (raw[6] >> 1) & 0x0F, bool(raw[6] & 1)


def _commands(information: bytes, max_commands: int) -> tuple[str, ...]:
    if information.count(b"AAA") != 1 or information.count(b"ZZZ") != 1:
        raise ProtocolError("Exactly one AAA and one ZZZ marker are required")
    start = information.index(b"AAA")
    end = information.index(b"ZZZ")
    if start >= end:
        raise ProtocolError("AAA must precede ZZZ")
    if start and information[start - 1] in _ALPHANUMERIC:
        raise ProtocolError("AAA is embedded in another word")
    if end + 3 < len(information) and information[end + 3] in _ALPHANUMERIC:
        raise ProtocolError("ZZZ is embedded in another word")
    region = information[start + 3 : end]
    if not region or region[0] not in _WHITESPACE or region[-1] not in _WHITESPACE:
        raise ProtocolError(
            "Sequence markers and commands must be whitespace separated"
        )
    raw_tokens = region.split()
    if not raw_tokens:
        raise ProtocolError("A sequence must contain at least one operational command")
    if len(raw_tokens) > max_commands:
        raise ProtocolError("Sequence exceeds the configured command limit")
    tokens: list[str] = []
    for token in raw_tokens:
        if len(token) != 1 or token[0] not in b"BCDEFGH":
            raise ProtocolError(
                "Only whitespace-separated uppercase B-H commands are accepted"
            )
        tokens.append(token.decode("ascii"))
    return tuple(tokens)


def parse_sequence(
    frame: bytes | bytearray | memoryview,
    *,
    source_callsign: str | None = None,
    source_ssids: Collection[int] | None = None,
    pan_min: float = -90.0,
    pan_max: float = 90.0,
    initial_pan: float = 0.0,
    max_commands: int = 64,
    max_repeaters: int = 8,
    max_information_bytes: int = 256,
) -> ParsedSequence:
    """Validate a KISS-decoded AX.25 UI/F0 frame and all command movements.

    This is fail-closed without both an explicitly configured base callsign and
    nonempty SSID collection. To permit all SSIDs deliberately, supply
    ``tuple(range(16))``; no sender is hardcoded. A source such as TEST1 cannot
    match TEST12, the destination, or a callsign mentioned in the payload.

    Accepts UI control 0x03 and 0x13 (the latter sets the poll/final bit) and PID
    0xF0 only. APRS-compatible paths can have up to eight repeaters, configurable
    downward. The input excludes on-air flags and FCS. Prefix/suffix bytes in
    the information field are preserved but never executed. Markers must be
    unique, and B-H commands must be individual whitespace-separated tokens.

    A returned object does not authorize execution: the controller must still
    check arming, attempt latches, timeouts and current hardware state.
    """
    if not isinstance(source_callsign, str) or not _CALLSIGN.fullmatch(
        source_callsign.upper()
    ):
        raise ProtocolError("Configure a confirmed 1-6 character source_callsign first")
    expected_source = source_callsign.upper()
    if source_ssids is None:
        raise ProtocolError("Configure source_ssids explicitly first")
    allowed_ssids = tuple(source_ssids)
    if not allowed_ssids or any(
        type(ssid) is not int or not 0 <= ssid <= 15 for ssid in allowed_ssids
    ):
        raise ProtocolError("source_ssids must contain integers from 0 through 15")
    for name, value, lower, upper in (
        ("max_commands", max_commands, 1, 256),
        ("max_repeaters", max_repeaters, 0, 8),
        ("max_information_bytes", max_information_bytes, 1, 256),
    ):
        if type(value) is not int or not lower <= value <= upper:
            raise ProtocolError(
                f"{name} must be an integer from {lower} through {upper}"
            )
    angles = (pan_min, pan_max, initial_pan)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in angles
    ):
        raise ProtocolError("Pan limits and initial angle must be finite numbers")
    if not pan_min <= initial_pan <= pan_max:
        raise ProtocolError("Initial pan must lie inside ordered pan limits")
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise TypeError("AX.25 input must be bytes-like")
    raw_frame = bytes(frame)
    maximum_length = 7 * (2 + max_repeaters) + 2 + max_information_bytes
    if not 16 <= len(raw_frame) <= maximum_length:
        raise ProtocolError("AX.25 frame length is outside configured limits")

    addresses: list[tuple[str, int, bool]] = []
    offset = 0
    for _ in range(2 + max_repeaters):
        address = _address(raw_frame[offset : offset + 7])
        addresses.append(address)
        offset += 7
        if address[2]:
            break
    else:
        raise ProtocolError(
            "AX.25 address field has no terminator within the repeater limit"
        )
    if len(addresses) < 2:
        raise ProtocolError("AX.25 frame must contain destination and source addresses")
    if len(raw_frame) < offset + 2:
        raise ProtocolError("Missing AX.25 control or PID")
    if raw_frame[offset] not in (0x03, 0x13):
        raise ProtocolError("Only AX.25 UI frames are accepted")
    if raw_frame[offset + 1] != 0xF0:
        raise ProtocolError("Only AX.25 PID F0 information is accepted")
    callsign, ssid, _ = addresses[1]
    if callsign != expected_source or ssid not in allowed_ssids:
        raise ProtocolError("AX.25 source is not in the configured sender allowlist")
    information = raw_frame[offset + 2 :]
    if len(information) > max_information_bytes:
        raise ProtocolError("AX.25 information exceeds the configured length limit")
    tokens = _commands(information, max_commands)

    angle = float(initial_pan)
    for number, token in enumerate(tokens, start=1):
        if token == "B":
            angle += 30.0
        elif token == "C":
            angle -= 30.0
        if not pan_min <= angle <= pan_max:
            raise ProtocolError(
                f"Command {number} would exceed the calibrated pan limits"
            )
    return ParsedSequence(
        source=f"{callsign}-{ssid}",
        source_callsign=callsign,
        source_ssid=ssid,
        information=information,
        tokens=tokens,
        initial_pan=float(initial_pan),
        final_pan=angle,
        raw_frame=raw_frame,
    )
