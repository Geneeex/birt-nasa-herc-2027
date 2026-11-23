"""BENCH gates, real executor use and service consistency without hardware."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from herc.controller import live, validate_live_configuration
from herc.pan import PanMap
from herc.hardware import HardwareConfig
from herc.protocol import parse_sequence
from tools import bench
from tools.services import generate
from tools.simulate import SimulationCamera, SimulationMotion, simulation_packet


class BenchMotion(SimulationMotion):
    def __init__(self, mode="BENCH"):
        super().__init__()
        self.mode, self.state = mode, "DISARMED"
        self.commands = []
        self.closed = False

    def status(self):
        return {"mode": self.mode, "state": self.state, "pulse_us": 0}

    def arm_bench(self):
        self.commands.append("BENCHARM")
        self.state = "ARMED"

    def begin(self):
        if self.mode != "BENCH" or self.state != "ARMED":
            raise RuntimeError("Bench gate failed")
        self.state = "RUNNING"
        super().begin()

    def close(self):
        self.closed = True
        self.stop()


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.motion = BenchMotion()
        self.camera = SimulationCamera(self.motion)
        self.camera.start = Mock(return_value=self.camera)
        self.camera.close = Mock()
        self.cfg = {
            "schema": 2,
            "hardware": HardwareConfig().__dict__,
            "pan_calibration": "pan.json",
            "camera_calibration": "camera.json",
            "camera": self.camera.calibration.camera_config,
            "execution_limit_seconds": 180,
        }
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(self.cfg))
        # The calibration loader is substituted for synthetic tests; retain a
        # file for the real evidence fingerprinting path.
        (self.root / "camera.json").write_text('{"synthetic_test_only":true}')
        (self.root / "pan.json").write_text(
            json.dumps(
                {
                    "measured": True,
                    "points": [[-30, 1300], [0, 1500], [30, 1700]],
                    "settle_seconds": 0.2,
                }
            )
        )
        self.packet = parse_sequence(
            simulation_packet("AAA B F D C H ZZZ"),
            source_callsign="SIMUL",
            source_ssids=[0],
        )
        self.patches = [
            patch("tools.bench.PiMotion", return_value=self.motion),
            patch("tools.bench.Camera", return_value=self.camera),
            patch("tools.bench.load_calibration", return_value=self.camera.calibration),
            patch("tools.bench.wait_for_packet", return_value=self.packet),
            patch(
                "herc.controller.checked_wait", lambda seconds, motion: motion.check()
            ),
            patch.dict(
                os.environ,
                {
                    "WATCHDOG_USEC": "15000000",
                    "WATCHDOG_PID": str(os.getpid()),
                    "NOTIFY_SOCKET": "TEST_ONLY_NO_REAL_SUPERVISOR",
                },
            ),
            patch("tools.bench.notify"),
            patch("herc.controller.notify"),
        ]
        self.mocks = [item.start() for item in self.patches]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def run_bench(self, **kwargs):
        return bench.run(self.config, "SIMUL", [0], confirm_motion=True, **kwargs)

    def test_explicit_motion_confirmation_required_before_opening_hardware(self):
        with self.assertRaisesRegex(ValueError, "confirm-motion"):
            bench.run(self.config, "SIMUL", [0])
        self.mocks[0].assert_not_called()

    def test_missing_physical_jumper_blocks_arm_and_camera(self):
        self.motion.mode = "LIVE"
        with self.assertRaisesRegex(RuntimeError, "jumper"):
            self.run_bench()
        self.assertEqual(self.motion.commands, [])
        self.assertNotIn(["begin"], self.motion.events)
        self.mocks[1].assert_not_called()
        self.assertTrue(self.motion.closed)

    def test_unsupervised_bench_refused_before_opening_hardware(self):
        with (
            patch.dict(os.environ, {"WATCHDOG_USEC": "0"}),
            self.assertRaisesRegex(RuntimeError, "watchdog"),
        ):
            self.run_bench()
        self.mocks[0].assert_not_called()

    def test_bench_uses_actual_executor_and_never_produces_live_evidence(self):
        folder = self.run_bench()
        data = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(data["mode"], "BENCH")
        self.assertEqual(data["status"], "DONE")
        self.assertIn("not competition", data["bench_notice"])
        self.assertTrue(data["excursion_id"].startswith("BENCH_"))
        self.assertEqual(self.motion.commands, ["BENCHARM"])
        self.assertIn(["move", 1700], self.motion.events)
        self.assertTrue(
            next(x["range"] for x in data["results"] if x["command"] == "H")[
                "available"
            ]
        )
        image = next(x for x in data["results"] if x["command"] == "D")
        self.assertIn(
            b"EvidenceMode\0BENCH", (folder / image["result"]["file"]).read_bytes()
        )
        with self.assertRaisesRegex(ValueError, "NASA configuration"):
            validate_live_configuration(self.cfg)

    def test_post_arm_mode_change_blocks_execution(self):
        self.mocks[3].side_effect = lambda *args: (
            setattr(self.motion, "mode", "LIVE") or self.packet
        )
        with self.assertRaisesRegex(RuntimeError, "jumper"):
            self.run_bench()
        self.assertNotIn(["begin"], self.motion.events)
        self.assertTrue(self.motion.closed)

    def test_measured_pan_bounds_are_mandatory_before_hardware(self):
        (self.root / "pan.json").write_text(
            json.dumps(
                {
                    "measured": False,
                    "points": [[-30, 1300], [0, 1500], [30, 1700]],
                    "settle_seconds": 0.2,
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "measured"):
            self.run_bench()
        self.mocks[0].assert_not_called()

    def test_live_rejects_bench_jumper_even_with_commissioning_mocked(self):
        self.cfg.update(
            {
                "results_directory": "live_runs",
                "excursion_id": "test",
                "commissioning": "fake.json",
            }
        )
        self.config.write_text(json.dumps(self.cfg))
        with (
            patch("herc.controller.validate_live_configuration"),
            patch("herc.commissioning.verify"),
            patch("herc.hardware.PiMotion", return_value=self.motion),
            patch("herc.vision.load_calibration", return_value=self.camera.calibration),
            patch("herc.camera.Camera") as camera_factory,
        ):
            with self.assertRaisesRegex(RuntimeError, "live operation refused"):
                live(self.config)
        camera_factory.assert_not_called()
        self.assertNotIn(["begin"], self.motion.events)


class BenchInputAndServicesTests(unittest.TestCase):
    def test_fresh_kiss_stream_rejects_wrong_sender_before_valid_packet(self):
        motion = BenchMotion()
        motion.state = "ARMED"
        pan = PanMap(
            {
                "measured": True,
                "points": [[-30, 1300], [0, 1500], [30, 1700]],
                "settle_seconds": 0.2,
            }
        )
        wrong = simulation_packet("AAA B D ZZZ").replace(
            bytes(ord(c) << 1 for c in "SIMUL "), bytes(ord(c) << 1 for c in "OTHER ")
        )
        correct = simulation_packet("AAA D ZZZ")
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.side_effect = [
            b"\xc0\x00" + wrong + b"\xc0",
            b"\xc0\x00" + correct + b"\xc0",
        ]
        with patch(
            "tools.bench.socket.create_connection", return_value=connection
        ) as create:
            result = bench.wait_for_packet(motion, pan, "SIMUL", [0], 8001, 2.0)
        self.assertEqual(result.tokens, ("D",))
        self.assertEqual(connection.recv.call_count, 2)
        self.assertEqual(create.call_args.args[0], ("127.0.0.1", 8001))

    def test_services_use_configured_evidence_root_and_reject_unescaped_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.json"
            config.write_text(json.dumps({"results_directory": "qualified_results"}))
            out = generate(root)
            service = (out / "herc-review.service").read_text()
            self.assertIn(
                "--root " + (root / "qualified_results").as_posix() + " --host", service
            )
            config.write_text(json.dumps({"results_directory": "unsafe%path"}))
            with self.assertRaisesRegex(ValueError, "Service paths"):
                generate(root)


if __name__ == "__main__":
    unittest.main()
