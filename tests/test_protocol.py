"""Hardware-independent regression tests for received command validation."""

import unittest

from herc.protocol import KissDecoder, ProtocolError, parse_sequence


def address(callsign, ssid=0, last=False, repeated=False):
    return bytes(ord(c) << 1 for c in callsign.ljust(6)) + bytes(
        [0x60 | (ssid << 1) | int(last) | (0x80 if repeated else 0)]
    )


def ax25(
    information=b">AAA B E F D H C G D ZZZ_1",
    source="TEST1",
    ssid=6,
    destination="APTEST",
    repeaters=(),
    control=0x03,
    pid=0xF0,
):
    fields = [(destination, 0), (source, ssid)] + list(repeaters)
    return (
        b"".join(
            address(call, sub, last=i == len(fields) - 1)
            for i, (call, sub) in enumerate(fields)
        )
        + bytes([control, pid])
        + information
    )


def kiss(payload, port=0, command=0):
    encoded = bytearray([0xC0])
    for byte in bytes([(port << 4) | command]) + payload:
        if byte == 0xC0:
            encoded.extend(b"\xdb\xdc")
        elif byte == 0xDB:
            encoded.extend(b"\xdb\xdd")
        else:
            encoded.append(byte)
    encoded.append(0xC0)
    return bytes(encoded)


class KissDecoderTests(unittest.TestCase):
    def test_bytewise_fragmentation_and_multiple_frames(self):
        first, second = ax25(), ax25(b">AAA G D ZZZ")
        wire = b"unframed junk" + kiss(first) + kiss(second)
        decoder = KissDecoder()
        actual = []
        for byte in wire:
            actual.extend(decoder.feed(bytes([byte])))
        self.assertEqual(actual, [first, second])
        self.assertEqual(KissDecoder().feed(wire), [first, second])

    def test_every_possible_split_including_escaped_bytes(self):
        payload = b"a\xc0b\xdbc"
        wire = kiss(payload)
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                decoder = KissDecoder()
                result = decoder.feed(wire[:split]) + decoder.feed(wire[split:])
                self.assertEqual(result, [payload])

    def test_only_data_and_configured_port(self):
        decoder = KissDecoder(port=2)
        wire = (
            kiss(b"wrong-port")
            + kiss(b"control", port=2, command=1)
            + kiss(b"good", port=2)
        )
        self.assertEqual(decoder.feed(wire), [b"good"])
        self.assertEqual(decoder.ignored_frames, 2)

    def test_escaped_type_byte_for_port_twelve(self):
        self.assertEqual(KissDecoder(port=12).feed(kiss(b"test", port=12)), [b"test"])

    def test_bad_escape_discards_entire_frame_and_recovers(self):
        decoder = KissDecoder()
        bad = b"\xc0\x00valid-prefix\xdb\x00suffix\xc0"
        self.assertEqual(decoder.feed(bad + kiss(b"next")), [b"next"])
        self.assertEqual(decoder.discarded_frames, 1)

    def test_escape_followed_by_delimiter_discards_frame(self):
        decoder = KissDecoder()
        self.assertEqual(
            decoder.feed(b"\xc0\x00bad\xdb\xc0" + kiss(b"good")), [b"good"]
        )
        self.assertEqual(decoder.discarded_frames, 1)

    def test_buffer_limit_discards_overlong_frame_and_recovers(self):
        decoder = KissDecoder(max_frame_bytes=8)
        self.assertEqual(decoder.feed(b"\xc0\x00" + b"x" * 100_000), [])
        self.assertLessEqual(decoder.buffered_bytes, 8)
        self.assertEqual(decoder.discarded_frames, 1)
        self.assertEqual(decoder.feed(b"\xc0" + kiss(b"good")), [b"good"])

    def test_exact_size_limit_includes_type_byte(self):
        decoder = KissDecoder(max_frame_bytes=5)
        self.assertEqual(decoder.feed(kiss(b"1234") + kiss(b"12345")), [b"1234"])

    def test_empty_delimiters_ignored_but_type_only_rejected(self):
        decoder = KissDecoder()
        self.assertEqual(decoder.feed(b"\xc0\xc0\xc0\x00\xc0"), [])
        self.assertEqual(decoder.discarded_frames, 1)

    def test_reset_does_not_join_data_from_different_connections(self):
        decoder = KissDecoder()
        decoder.feed(b"\xc0\x00partial")
        decoder.reset()
        self.assertEqual(decoder.feed(b"rest\xc0" + kiss(b"fresh")), [b"fresh"])

    def test_configuration_validation(self):
        for kwargs in (
            {"port": -1},
            {"port": 16},
            {"port": True},
            {"max_frame_bytes": 1},
            {"max_frame_bytes": 1.5},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                KissDecoder(**kwargs)


class SequenceTests(unittest.TestCase):
    def parse(self, frame=None, **kwargs):
        options = {"source_callsign": "TEST1", "source_ssids": (6,)}
        options.update(kwargs)
        return parse_sequence(ax25() if frame is None else frame, **options)

    def test_valid_sequence_preserves_evidence_and_operational_tokens(self):
        raw = ax25()
        result = self.parse(raw)
        self.assertEqual(result.source, "TEST1-6")
        self.assertEqual(result.source_callsign, "TEST1")
        self.assertEqual(result.source_ssid, 6)
        self.assertEqual(result.information, b">AAA B E F D H C G D ZZZ_1")
        self.assertEqual(result.tokens, ("B", "E", "F", "D", "H", "C", "G", "D"))
        self.assertEqual(result.initial_pan, 0)
        self.assertEqual(result.final_pan, 0)
        self.assertEqual(result.raw_frame, raw)

    def test_no_implicit_sender_or_ssid_configuration(self):
        for options in (
            {},
            {"source_callsign": "TEST1"},
            {"source_ssids": (6,)},
            {"source_callsign": "TEST1", "source_ssids": ()},
        ):
            with self.subTest(options=options), self.assertRaises(ProtocolError):
                parse_sequence(ax25(), **options)

    def test_exact_source_match_not_prefix_or_payload(self):
        for raw in (
            ax25(source="TEST12"),
            ax25(source="OTHER", destination="TEST1"),
            ax25(b">TEST1 AAA D ZZZ", source="OTHER"),
        ):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                self.parse(raw)

    def test_ssid_is_explicitly_filtered(self):
        with self.assertRaises(ProtocolError):
            self.parse(ax25(ssid=7))
        self.assertEqual(
            self.parse(ax25(ssid=7), source_ssids=tuple(range(16))).source_ssid, 7
        )
        self.assertEqual(self.parse(ax25(ssid=0), source_ssids=(0,)).source, "TEST1-0")

    def test_variable_repeater_count_does_not_shift_payload_incorrectly(self):
        for count in range(9):
            repeaters = tuple((f"RPT{i}", i) for i in range(count))
            raw = ax25(b">AAA H ZZZ", repeaters=repeaters)
            with self.subTest(count=count):
                self.assertEqual(self.parse(raw).tokens, ("H",))
        with self.assertRaises(ProtocolError):
            self.parse(ax25(repeaters=(("RPT1", 0),)), max_repeaters=0)
        with self.assertRaises(ProtocolError):
            self.parse(ax25(repeaters=tuple(("RPT", 0) for _ in range(9))))

    def test_malformed_address_fields_are_rejected(self):
        valid = ax25()
        bad_shift = bytearray(valid)
        bad_shift[0] |= 1
        bad_padding = bytearray(valid)
        bad_padding[7:13] = bytes(ord(c) << 1 for c in "TE ST1")
        bad_reserved = bytearray(valid)
        bad_reserved[13] &= ~0x60
        premature_end = bytearray(valid)
        premature_end[6] |= 1
        no_terminator = address("APTEST") + address("TEST1", 6) + b"tiny"
        for raw in (bad_shift, bad_padding, bad_reserved, premature_end, no_terminator):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                self.parse(raw)

    def test_ui_poll_bit_allowed_but_other_control_and_pid_rejected(self):
        self.assertEqual(self.parse(ax25(control=0x13)).tokens[0], "B")
        for raw in (ax25(control=0x00), ax25(control=0x2F), ax25(pid=0xCF)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                self.parse(raw)

    def test_truncated_header_is_rejected(self):
        for length in (0, 1, 6, 7, 13, 14, 15):
            with self.subTest(length=length), self.assertRaises(ProtocolError):
                self.parse(ax25()[:length])

    def test_markers_must_be_single_complete_pair_in_order(self):
        bad_information = (
            b"nothing",
            b"AAA D",
            b"D ZZZ",
            b"ZZZ D AAA",
            b"AAA D ZZZ AAA H ZZZ",
            b"AAA AAA D ZZZ",
            b"AAA D ZZZ ZZZ",
            b"AAA ZZZ",
            b"XAAA D ZZZ",
            b"AAA D ZZZX",
            b"AAAD ZZZ",
            b"AAA DZZZ",
        )
        for information in bad_information:
            with (
                self.subTest(information=information),
                self.assertRaises(ProtocolError),
            ):
                self.parse(ax25(information))

    def test_only_individual_uppercase_commands_allowed(self):
        for information in (
            b"AAA I ZZZ",
            b"AAA b ZZZ",
            b"AAA BD ZZZ",
            b"AAA B,D ZZZ",
            b"AAA D\x00 H ZZZ",
            b"AAA \xff ZZZ",
        ):
            with (
                self.subTest(information=information),
                self.assertRaises(ProtocolError),
            ):
                self.parse(ax25(information))

    def test_ascii_whitespace_and_envelope_metadata(self):
        result = self.parse(ax25(b">status: AAA\tB\r\nD\vH\fZZZ_42"))
        self.assertEqual(result.tokens, ("B", "D", "H"))

    def test_command_and_information_limits(self):
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA D H D ZZZ"), max_commands=2)
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA D ZZZ" + b"_" * 250))
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA D ZZZ"), max_information_bytes=8)

    def test_full_path_preflight_rejects_intermediate_excursion(self):
        # Final angle would be 60 degrees, but the third B first reaches 90.
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA B B B C ZZZ"), pan_min=-60, pan_max=60)
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA C C C B ZZZ"), pan_min=-60, pan_max=60)

    def test_pan_boundary_and_initial_angle_are_respected(self):
        result = self.parse(ax25(b"AAA B B D ZZZ"), pan_min=-30, pan_max=60)
        self.assertEqual(result.final_pan, 60)
        result = self.parse(ax25(b"AAA C D ZZZ"), initial_pan=30)
        self.assertEqual(result.final_pan, 0)
        with self.assertRaises(ProtocolError):
            self.parse(ax25(b"AAA B ZZZ"), initial_pan=80)

    def test_invalid_limits_are_rejected(self):
        bad_options = (
            {"pan_min": 10, "pan_max": -10},
            {"initial_pan": 100},
            {"pan_max": float("nan")},
            {"initial_pan": float("inf")},
            {"pan_min": True},
            {"max_commands": 0},
            {"max_commands": 1.5},
            {"max_repeaters": 9},
            {"max_information_bytes": 257},
            {"source_ssids": (16,)},
            {"source_ssids": (True,)},
            {"source_callsign": "TEST1-6"},
        )
        for kwargs in bad_options:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProtocolError):
                self.parse(**kwargs)

    def test_end_to_end_chunks_produce_one_preflighted_sequence(self):
        raw = ax25(b">AAA B F D H ZZZ", repeaters=(("WIDE1", 1),))
        wire = kiss(raw)
        decoder = KissDecoder()
        outputs = []
        for start in range(0, len(wire), 3):
            for frame in decoder.feed(wire[start : start + 3]):
                outputs.append(self.parse(frame))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].tokens, ("B", "F", "D", "H"))
        self.assertEqual(outputs[0].final_pan, 30)


if __name__ == "__main__":
    unittest.main()
