"""Pi 5 state, GPIO and PWM register tests with explicit device substitutes."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from herc.hardware import HardwareConfig, PiIO, PiMotion, open_rp1
from herc.rc_input import RcInput


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now


class FakeIO:
    def __init__(self, bench=False):
        self.jumper = bench
        self.enabled = False
        self.events = []
        self.on_pulse = None

    def bench(self):
        return self.jumper

    def enable(self):
        self.enabled = True
        self.events.append("enable")

    def disable(self):
        self.enabled = False
        self.events.append("disable")

    def pulse(self, value):
        self.events.append(("pulse", value))
        if self.on_pulse:
            self.on_pulse()

    def close(self):
        self.disable()
        self.events.append("close")


class PiMotionTests(unittest.TestCase):
    def device(self, bench=False):
        self.clock, self.io = Clock(), FakeIO(bench)
        motion = PiMotion(
            {}, io_factory=lambda cfg, edge: self.io, clock=self.clock, monitor=False
        )
        self.addCleanup(motion.close)
        return motion

    def frames(self, motion, width, count=20):
        for _ in range(count):
            self.clock.now += 20_000_000
            motion._edge(0, 24, 1, self.clock.now)
            self.clock.now += width * 1000
            motion._edge(0, 24, 0, self.clock.now)
            motion.check()

    def armed(self):
        motion = self.device()
        self.frames(motion, 1000)
        self.frames(motion, 2000)
        self.assertEqual(motion.state, "ARMED")
        return motion

    def test_boot_high_cannot_arm(self):
        motion = self.device()
        self.frames(motion, 2000, 50)
        self.assertEqual(motion.state, "DISARMED")
        self.assertFalse(self.io.enabled)

    def test_short_low_cannot_arm(self):
        motion = self.device()
        self.frames(motion, 1000, 5)
        self.frames(motion, 2000, 50)
        self.assertEqual(motion.state, "DISARMED")

    def test_live_sequence_enables_only_after_valid_pulse_write(self):
        motion = self.armed()
        motion.begin()
        self.assertFalse(self.io.enabled)
        motion.move(1500)
        self.assertEqual(self.io.events[-2:], [("pulse", 1500), "enable"])
        motion.finish()
        self.assertFalse(self.io.enabled)
        self.assertEqual(motion.state, "DONE")
        with self.assertRaises(RuntimeError):
            motion.begin()

    def test_disarmed_movement_is_rejected(self):
        motion = self.device()
        with self.assertRaises(RuntimeError):
            motion.move(1500)
        self.assertEqual(self.io.events, [])

    def test_invalid_pulse_never_reaches_driver(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        for value in [True, 899, 2101, 1500.0, "1500"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                motion.move(value)
        self.assertFalse(self.io.enabled)

    def test_receiver_loss_latches_fault(self):
        motion = self.armed()
        motion.begin()
        motion.move(1500)
        self.clock.now += 251_000_000
        with self.assertRaisesRegex(RuntimeError, "PWM lost"):
            motion.check()
        self.assertFalse(self.io.enabled)
        self.assertEqual(motion.state, "FAULT")

    def test_live_disarm_disables_output(self):
        motion = self.armed()
        motion.begin()
        motion.move(1500)
        with self.assertRaisesRegex(RuntimeError, "permission"):
            self.frames(motion, 1000, 1)
        self.assertFalse(self.io.enabled)

    def test_live_refuses_bench_arm(self):
        motion = self.device()
        with self.assertRaises(RuntimeError):
            motion.arm_bench()

    def test_jumper_change_disables_output(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        motion.move(1500)
        self.io.jumper = False
        with self.assertRaisesRegex(RuntimeError, "jumper changed"):
            motion.check()
        self.assertFalse(self.io.enabled)

    def test_expired_progress_cannot_be_revived(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        motion.move(1500)
        self.clock.now += 15_000_000_001
        with self.assertRaisesRegex(RuntimeError, "progress"):
            motion.check()
        self.assertFalse(self.io.enabled)

    def test_total_runtime_limit_is_separate_from_progress(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        for _ in range(17):
            self.clock.now += 10_000_000_000
            motion.check()
        self.clock.now += 10_000_000_000
        with self.assertRaisesRegex(RuntimeError, "180 seconds"):
            motion.check()

    def test_i2c_failure_disables_output(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        self.io.on_pulse = Mock(side_effect=OSError("bus failed"))
        with self.assertRaises(OSError):
            motion.move(1500)
        self.assertFalse(self.io.enabled)
        self.assertEqual(motion.state, "FAULT")

    def test_stop_during_i2c_write_is_not_reenabled(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        self.io.on_pulse = motion.stop
        with self.assertRaises(RuntimeError):
            motion.move(1500)
        self.assertNotIn("enable", self.io.events)

    def test_close_disables_and_releases(self):
        motion = self.device(bench=True)
        motion.arm_bench()
        motion.begin()
        motion.move(1500)
        motion.close()
        self.assertFalse(self.io.enabled)
        self.assertEqual(self.io.events[-1], "close")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            motion.check()


class RcTests(unittest.TestCase):
    def test_stale_or_future_events_do_not_establish_permission(self):
        for tick, now in [(1, 200_000_000), (200_000_000, 1)]:
            rc = RcInput()
            rc.edge(1, tick, now)
            rc.edge(0, tick + 2_000_000, now)
            self.assertEqual(rc.sample(now), 0)

    def test_invalid_width_or_period_clears_permission(self):
        for width, period in [
            (700, 20_000_000),
            (2300, 20_000_000),
            (2000, 100_000_000),
        ]:
            rc = RcInput()
            rc.edge(1, 1_000_000_000, 1_000_000_000)
            rise = 1_000_000_000 + period
            rc.edge(1, rise, rise)
            fall = rise + width * 1000
            rc.edge(0, fall, fall)
            self.assertEqual(rc.sample(fall), 0)


class DriverTests(unittest.TestCase):
    def fake_lg(self):
        lg = Mock()
        lg.BOTH_EDGES, lg.SET_PULL_UP, lg.SET_PULL_DOWN = 3, 32, 64
        registers = {0: 1}
        lg.i2c_open.return_value = 7
        lg.i2c_write_byte_data.side_effect = lambda h, r, v: registers.update({r: v})
        lg.i2c_read_byte_data.side_effect = lambda h, r: registers.get(r, 0)
        return lg

    def test_register_setup_pulse_math_and_cleanup(self):
        lg = self.fake_lg()
        with (
            patch.dict("sys.modules", {"lgpio": lg}),
            patch("herc.hardware.open_rp1", return_value=4),
        ):
            io = PiIO(HardwareConfig(), Mock())
            lg.gpio_claim_output.assert_called_once_with(4, 17, 1)
            self.assertEqual(io.prescale, 121)
            io.pulse(1500)
            lg.i2c_write_i2c_block_data.assert_called_with(7, 6, [0, 0, 51, 1])
            io.close()
            lg.gpio_write.assert_called_with(4, 17, 1)
            lg.gpiochip_close.assert_called_once_with(4)

    def test_initialization_error_releases_disabled_gpio(self):
        lg = self.fake_lg()
        lg.i2c_open.side_effect = OSError("not connected")
        with (
            patch.dict("sys.modules", {"lgpio": lg}),
            patch("herc.hardware.open_rp1", return_value=4),
        ):
            with self.assertRaises(OSError):
                PiIO(HardwareConfig(), Mock())
        lg.gpio_write.assert_called_with(4, 17, 1)
        lg.gpiochip_close.assert_called_once_with(4)

    def test_rp1_discovery_uses_label_not_chip_number(self):
        lg = Mock()
        lg.gpiochip_open.side_effect = [20, 21]
        lg.gpio_get_chip_info.side_effect = [
            [0, 32, "gpiochip0", "other"],
            [0, 54, "gpiochip4", "pinctrl-rp1"],
        ]
        with (
            patch.object(
                Path, "read_bytes", return_value=b"Raspberry Pi 5 Model B Rev 1.0\0"
            ),
            patch.object(
                Path,
                "glob",
                return_value=[Path("/dev/gpiochip0"), Path("/dev/gpiochip4")],
            ),
        ):
            self.assertEqual(open_rp1(lg), 21)
        lg.gpiochip_close.assert_called_once_with(20)

    def test_invalid_config_is_rejected_before_io(self):
        for values in [
            {"oe_gpio": 24},
            {"rc_gpio": True},
            {"pan_channel": 16},
            {"pwm_hz": 60},
            {"i2c_bus": 0},
            {"pca_address": 0x70},
            {"progress_timeout_seconds": float("nan")},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                HardwareConfig(**values)


if __name__ == "__main__":
    unittest.main()
