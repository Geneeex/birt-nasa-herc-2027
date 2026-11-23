"""Integration tests with synthetic images and fake motion, never hardware."""

import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from herc.controller import execute
from herc.evidence import Evidence, claim_attempt
from herc.pan import PanMap
from herc.protocol import parse_sequence
from herc.review import make_handler
from herc import commissioning
from tools.simulate import (
    SimulationCamera,
    SimulationMotion,
    simulation_packet,
    simulation_pan,
    run,
)

HAS_CV = importlib.util.find_spec("cv2") is not None


def parsed(sequence):
    return parse_sequence(
        simulation_packet(sequence), source_callsign="SIMUL", source_ssids=[0]
    )


@unittest.skipUnless(HAS_CV, "OpenCV required for executor integration tests")
class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.motion = SimulationMotion()
        self.camera = SimulationCamera(self.motion)
        self.wait = patch(
            "herc.controller.checked_wait", lambda seconds, motion: motion.check()
        )
        self.wait.start()

    def tearDown(self):
        self.wait.stop()
        self.temp.cleanup()

    def evidence(self, sequence):
        return Evidence(self.root, sequence, mode="SIMULATION", excursion_id="test")

    def test_order_filter_overwrite_range_and_timestamped_images(self):
        import cv2

        sequence = parsed("AAA B E F D C H G D ZZZ")
        evidence = self.evidence(sequence)
        execute(
            sequence,
            simulation_pan(),
            self.motion,
            self.camera,
            self.camera.calibration,
            evidence,
        )
        manifest = json.loads((evidence.path / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "DONE")
        self.assertEqual(manifest["mode"], "SIMULATION")
        self.assertEqual(
            [x["command"] for x in manifest["results"]], list(sequence.tokens)
        )
        self.assertEqual(
            self.motion.events,
            [["begin"], ["move", 1500], ["move", 1700], ["move", 1500], ["finish"]],
        )
        pictures = [x for x in manifest["results"] if x["command"] == "D"]
        self.assertEqual([x["filter"] for x in pictures], ["blue", "none"])
        result = cv2.imread(str(evidence.path / pictures[0]["result"]["file"]))[:960]
        original = cv2.imread(str(evidence.path / pictures[0]["original"]["file"]))
        self.assertGreater(int(original[:, :, 2].sum()), 0)
        self.assertEqual(int(result[:, :, 1:].sum()), 0)
        self.assertGreater(int(result[:, :, 0].sum()), 0)
        encoded = (evidence.path / pictures[0]["original"]["file"]).read_bytes()
        self.assertIn(b"CaptureTimeUTC\0", encoded)
        self.assertIn(b"EvidenceMode\0SIMULATION", encoded)
        distance = next(x["range"] for x in manifest["results"] if x["command"] == "H")
        self.assertTrue(distance["available"], distance)
        self.assertAlmostEqual(distance["distance_ft"], 20.0, delta=0.5)

    def test_capture_fault_stops_and_records_fault(self):
        sequence = parsed("AAA D B D ZZZ")
        evidence = self.evidence(sequence)
        self.camera.capture = lambda: (_ for _ in ()).throw(
            RuntimeError("camera disconnected")
        )
        with self.assertRaisesRegex(RuntimeError, "camera disconnected"):
            execute(
                sequence,
                simulation_pan(),
                self.motion,
                self.camera,
                self.camera.calibration,
                evidence,
            )
        self.assertEqual(evidence.record["status"], "FAULT")
        self.assertEqual(self.motion.events[-1], ["stop"])
        self.assertNotIn(["finish"], self.motion.events)
        self.assertNotIn(["move", 1700], self.motion.events)

    def test_unavailable_range_is_partial_and_later_picture_executes(self):
        sequence = parsed("AAA H D ZZZ")
        evidence = self.evidence(sequence)
        self.camera.blank = True
        execute(
            sequence,
            simulation_pan(),
            self.motion,
            self.camera,
            self.camera.calibration,
            evidence,
        )
        self.assertEqual(evidence.record["status"], "PARTIAL")
        self.assertEqual([x["command"] for x in evidence.record["results"]], ["H", "D"])
        self.assertFalse(evidence.record["results"][0]["range"]["available"])

    def test_range_camera_metadata_fault_is_not_partial(self):
        sequence = parsed("AAA H D ZZZ")
        evidence = self.evidence(sequence)
        capture = self.camera.capture

        def changed_focus():
            frame = capture()
            frame.metadata["LensPosition"] = 3.0
            return frame

        self.camera.capture = changed_focus
        with self.assertRaisesRegex(ValueError, "focus differs"):
            execute(
                sequence,
                simulation_pan(),
                self.motion,
                self.camera,
                self.camera.calibration,
                evidence,
            )
        self.assertEqual(evidence.record["status"], "FAULT")
        self.assertEqual(self.motion.events[-1], ["stop"])

    def test_actual_pan_bounds_rechecked_before_begin(self):
        sequence = parsed("AAA B B D ZZZ")
        narrow = PanMap(
            {
                "measured": True,
                "points": [[-30, 1300], [0, 1500], [30, 1700]],
                "settle_seconds": 0.2,
            }
        )
        evidence = self.evidence(sequence)
        with self.assertRaisesRegex(ValueError, "safe travel"):
            execute(
                sequence,
                narrow,
                self.motion,
                self.camera,
                self.camera.calibration,
                evidence,
            )
        self.assertNotIn(["begin"], self.motion.events)
        self.assertFalse(any(event[0] == "move" for event in self.motion.events))

    def test_complete_offline_demo_labels_simulation(self):
        folder = run(self.root / "demo")
        record = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(record["mode"], "SIMULATION")
        self.assertEqual(record["source"], "SIMUL-0")
        self.assertEqual(record["status"], "DONE")
        self.assertIn("No NASA transmission", record["simulation_notice"])


class PersistentAndViewerTests(unittest.TestCase):
    def test_commissioning_requires_two_distinct_named_people(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("herc.commissioning.fingerprint", return_value={"test": "value"}),
        ):
            record = Path(folder) / "commissioning.json"
            data = {
                "fingerprint": {"test": "value"},
                "operators": ["A", "B"],
                "tests": {
                    name: {"passed": True, "record": "measured-record"}
                    for name in commissioning.REQUIRED_TESTS
                },
            }
            record.write_text(json.dumps(data))
            self.assertEqual(
                commissioning.verify("unused", record)["operators"], ["A", "B"]
            )
            for operators in ("ab", ["A", "a"], ["A", ""], ["A", None]):
                with self.subTest(operators=operators), self.assertRaises(ValueError):
                    data["operators"] = operators
                    record.write_text(json.dumps(data))
                    commissioning.verify("unused", record)

    def test_attempt_claim_survives_new_call(self):
        with tempfile.TemporaryDirectory() as folder:
            path = claim_attempt(folder, "excursion_1")
            self.assertTrue(path.is_file())
            with self.assertRaises(FileExistsError):
                claim_attempt(Path(folder), "excursion_1")
            with self.assertRaises(ValueError):
                claim_attempt(folder, "../escape")

    def test_invalid_pan_calibrations(self):
        for points in (
            [[-30, 1500], [0, 1500], [30, 1600]],
            [[-30, 1300], [10, 1500], [30, 1700]],
            [[-30, 800], [0, 1500], [30, 1700]],
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                PanMap({"measured": True, "points": points, "settle_seconds": 0.2})

    def test_viewer_traversal_and_mutation_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "runs"
            root.mkdir()
            (base / "secret.json").write_text('{"secret":true}')
            (root / "sample").mkdir()
            manifest = root / "sample" / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "mode": "SIMULATION",
                        "status": "DONE",
                        "commands": ["D"],
                        "results": [],
                    }
                )
            )
            before = manifest.read_bytes()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ("/%2e%2e/secret.json", "/..%5csecret.json", "/%00.json"):
                    with self.subTest(path=path):
                        client = http.client.HTTPConnection(
                            *server.server_address, timeout=2
                        )
                        client.request("GET", path)
                        response = client.getresponse()
                        body = response.read()
                        client.close()
                        self.assertEqual(response.status, 404)
                        self.assertNotIn(b'"secret":true', body)
                client = http.client.HTTPConnection(*server.server_address, timeout=2)
                client.request("POST", "/sample/manifest.json", body=b"changed")
                response = client.getresponse()
                response.read()
                client.close()
                self.assertEqual(response.status, 501)
                self.assertEqual(manifest.read_bytes(), before)
                client = http.client.HTTPConnection(*server.server_address, timeout=2)
                client.request("GET", "/")
                response = client.getresponse()
                body = response.read()
                client.close()
                self.assertEqual(response.status, 200)
                self.assertIn(b"SIMULATION", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
