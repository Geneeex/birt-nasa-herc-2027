"""Pi 5 RP1 GPIO and a dedicated PCA9685 camera-servo board.

No propulsion outputs are implemented. OE must have an external pull-up;
remove the driver's default OE pull-down before using this circuit.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time

from .rc_input import RcInput


@dataclass(frozen=True)
class HardwareConfig:
    i2c_bus: int = 1
    pca_address: int = 0x40
    pan_channel: int = 0
    oe_gpio: int = 17
    rc_gpio: int = 24
    bench_gpio: int = 22
    reference_clock_hz: int = 25_000_000
    pwm_hz: int = 50
    progress_timeout_seconds: float = 15.0

    def __post_init__(self):
        for key, low, high in (
            ("i2c_bus", 1, 1),
            ("pca_address", 0x40, 0x77),
            ("pan_channel", 0, 15),
            ("reference_clock_hz", 20_000_000, 30_000_000),
            ("pwm_hz", 50, 50),
        ):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid hardware setting: " + key)
        pins = (self.oe_gpio, self.rc_gpio, self.bench_gpio)
        if (
            any(type(p) is not int or not 4 <= p <= 27 for p in pins)
            or len(set(pins)) != 3
        ):
            raise ValueError("Use three distinct header GPIO offsets from 4 through 27")
        value = self.progress_timeout_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 1 <= value <= 15
        ):
            raise ValueError("Progress timeout must be from 1 through 15 seconds")
        if self.pca_address == 0x70:
            raise ValueError("Do not use the PCA9685 all-call address")


def open_rp1(lg):
    model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode()
    if not model.startswith("Raspberry Pi 5 Model B"):
        raise RuntimeError("This backend requires Raspberry Pi 5 Model B")
    for path in sorted(Path("/dev").glob("gpiochip*")):
        if not path.name[8:].isdigit():
            continue
        handle = lg.gpiochip_open(int(path.name[8:]))
        try:
            info = lg.gpio_get_chip_info(handle)
            if len(info) >= 4 and info[0] == 0 and info[3] == "pinctrl-rp1":
                return handle
        except BaseException:
            lg.gpiochip_close(handle)
            raise
        lg.gpiochip_close(handle)
    raise RuntimeError("RP1 header GPIO controller was not found")


class PiIO:
    def __init__(self, config, edge_callback):
        import lgpio

        self.lg, self.cfg = lgpio, config
        self.chip = self.bus = self.callback = None
        self.oe_claimed = False
        try:
            self.chip = open_rp1(lgpio)
            lgpio.gpio_claim_output(self.chip, config.oe_gpio, 1)
            self.oe_claimed = True
            lgpio.gpio_claim_input(self.chip, config.bench_gpio, lgpio.SET_PULL_UP)
            self.bus = lgpio.i2c_open(config.i2c_bus, config.pca_address)
            self.setup_pwm()
            self.callback = lgpio.callback(
                self.chip, config.rc_gpio, lgpio.BOTH_EDGES, edge_callback
            )
            lgpio.gpio_claim_alert(
                self.chip, config.rc_gpio, lgpio.BOTH_EDGES, lgpio.SET_PULL_DOWN
            )
        except BaseException:
            self.close()
            raise

    def setup_pwm(self):
        lg, bus = self.lg, self.bus
        if lg.i2c_read_byte_data(bus, 0x00) & 0x40:
            raise RuntimeError(
                "PCA9685 external clock enabled; power-cycle the task board"
            )
        self.prescale = (
            round(self.cfg.reference_clock_hz / (4096 * self.cfg.pwm_hz)) - 1
        )
        lg.i2c_write_byte_data(bus, 0x00, 0x30)
        lg.i2c_write_byte_data(bus, 0x01, 0x04)  # OE high forces outputs low.
        lg.i2c_write_byte_data(bus, 0xFE, self.prescale)
        lg.i2c_write_i2c_block_data(bus, 0xFA, [0, 0, 0, 0x10])
        lg.i2c_write_byte_data(
            bus, 0x00, 0x20
        )  # Internal clock, no broadcast addresses.
        time.sleep(0.001)
        if (
            lg.i2c_read_byte_data(bus, 0xFE) != self.prescale
            or lg.i2c_read_byte_data(bus, 0x01) != 0x04
        ):
            raise RuntimeError("PCA9685 register verification failed")

    def disable(self):
        if self.oe_claimed:
            self.lg.gpio_write(self.chip, self.cfg.oe_gpio, 1)

    def enable(self):
        self.lg.gpio_write(self.chip, self.cfg.oe_gpio, 0)

    def bench(self):
        return self.lg.gpio_read(self.chip, self.cfg.bench_gpio) == 0

    def pulse(self, microseconds):
        ticks = round(
            microseconds
            * self.cfg.reference_clock_hz
            / ((self.prescale + 1) * 1_000_000)
        )
        self.lg.i2c_write_i2c_block_data(
            self.bus, 0x06 + 4 * self.cfg.pan_channel, [0, 0, ticks & 255, ticks >> 8]
        )

    def close(self):
        try:
            self.disable()
        finally:
            if self.callback is not None:
                self.callback.cancel()
                self.callback = None
            try:
                if self.bus is not None:
                    self.lg.i2c_close(self.bus)
                    self.bus = None
            finally:
                if self.chip is not None:
                    self.lg.gpiochip_close(self.chip)
                    self.chip = None
                    self.oe_claimed = False


class PiMotion:
    def __init__(
        self, config, *, io_factory=PiIO, clock=time.monotonic_ns, monitor=True
    ):
        self.cfg = (
            config if isinstance(config, HardwareConfig) else HardwareConfig(**config)
        )
        self.clock = clock
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.rc = RcInput()
        self.state = "DISARMED"
        self.reason = self.started = None
        self.progress = self.clock()
        self.closed = threading.Event()
        self.worker = None
        self.io = io_factory(self.cfg, self._edge)
        try:
            self.mode = "BENCH" if self.io.bench() else "LIVE"
            if monitor:
                self.worker = threading.Thread(target=self._monitor, daemon=True)
                self.worker.start()
        except BaseException:
            self.io.close()
            raise

    def _edge(self, chip, gpio, level, tick):
        with self.lock:
            self.rc.edge(level, tick, self.clock())

    def _fault(self, reason):
        self.state, self.reason = "FAULT", reason
        self.io.disable()  # Does not wait for an I2C transfer to finish.

    def _poll(self):
        now = self.clock()
        if self.io.bench() != (self.mode == "BENCH"):
            self._fault("Bench jumper changed; restart deliberately")
        if self.state in ("DONE", "FAULT"):
            return
        if now - self.progress > self.cfg.progress_timeout_seconds * 1e9:
            self._fault("Task execution stopped reporting progress")
            return
        pulse = self.rc.sample(now)
        if self.mode == "LIVE":
            if self.state in ("ARMED", "RUNNING") and not 1750 <= pulse <= 2200:
                self._fault("Receiver permission withdrawn or PWM lost")
            elif self.state == "DISARMED" and self.rc.arm_ready(now):
                self.state = "ARMED"
        if self.state == "RUNNING" and now - self.started >= 180_000_000_000:
            self._fault("Task execution exceeded 180 seconds")

    def _monitor(self):
        while not self.closed.wait(0.01):
            try:
                with self.lock:
                    self._poll()
            except Exception as exc:
                with self.lock:
                    self.state, self.reason = "FAULT", str(exc)
                    try:
                        self.io.disable()
                    except Exception:
                        pass
                return

    def check(self):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError("Task hardware is closed")
            self._poll()  # Expiry is checked before renewing progress.
            if self.state == "FAULT":
                raise RuntimeError(self.reason)
            self.progress = self.clock()

    def status(self):
        self.check()
        with self.lock:
            return {
                "state": self.state,
                "mode": self.mode,
                "pulse_us": self.rc.sample(self.clock()),
            }

    def arm_bench(self):
        self.check()
        with self.lock:
            if self.mode != "BENCH" or self.state != "DISARMED":
                raise RuntimeError(
                    "Bench arming requires the jumper and DISARMED state"
                )
            self.state = "ARMED"

    def begin(self):
        self.check()
        with self.lock:
            if self.state != "ARMED":
                raise RuntimeError("Task is not armed")
            self.started, self.state = self.clock(), "RUNNING"

    def move(self, pulse_us):
        if type(pulse_us) is not int or not 900 <= pulse_us <= 2100:
            raise ValueError(
                "Pulse must be an integer from 900 through 2100 microseconds"
            )
        self.check()
        with self.lock:
            if self.state != "RUNNING":
                raise RuntimeError("Servo movement requires RUNNING state")
        try:
            with self.io_lock:
                self.io.pulse(pulse_us)
            with self.lock:
                self.check()
                if self.state != "RUNNING":
                    raise RuntimeError("Servo permission changed during the I2C write")
                self.io.enable()
        except BaseException:
            with self.lock:
                self._fault("Servo update failed")
            raise

    def finish(self):
        self.check()
        with self.lock:
            if self.state != "RUNNING":
                raise RuntimeError("Only a running task can finish")
            self.io.disable()
            self.state = "DONE"

    def stop(self):
        with self.lock:
            self._fault("Stopped by operator or task error")

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            with self.lock:
                self.io.disable()
            if self.worker is not None:
                self.worker.join(timeout=1.0)
        finally:
            self.io.close()
