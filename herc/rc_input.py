"""Pulse validation and deliberate low-to-high task arming."""


class RcInput:
    def __init__(self):
        self.rise = self.previous_rise = self.last_tick = None
        self.period_ok = False
        self.width_us = 0
        self.low_since = self.high_since = None
        self.low_seen = False

    def invalidate(self):
        self.width_us = 0
        self.last_tick = None
        self.low_since = self.high_since = None
        self.low_seen = False

    def edge(self, level, tick, now_ns):
        # Current RP1 events use CLOCK_MONOTONIC. Reject stale queued edges.
        if level not in (0, 1) or not 0 <= now_ns - tick <= 100_000_000:
            self.invalidate()
            self.rise = self.previous_rise = None
            return
        if level == 1:
            self.period_ok = (
                self.previous_rise is not None
                and 5_000_000 <= tick - self.previous_rise <= 40_000_000
            )
            self.previous_rise = self.rise = tick
            return
        rise, self.rise = self.rise, None
        if rise is None or not self.period_ok:
            self.invalidate()
            return
        width = (tick - rise) / 1000
        if not 800 <= width <= 2200:
            self.invalidate()
            return
        self.last_tick = tick
        self.width_us = round(width)

    def sample(self, now_ns):
        if self.last_tick is None or not 0 <= now_ns - self.last_tick <= 250_000_000:
            self.invalidate()
        return self.width_us

    def arm_ready(self, now_ns):
        pulse = self.sample(now_ns)
        if 800 <= pulse <= 1250:
            self.high_since = None
            if self.low_since is None:
                self.low_since = now_ns
            if now_ns - self.low_since >= 300_000_000:
                self.low_seen = True
        elif 1750 <= pulse <= 2200 and self.low_seen:
            if self.high_since is None:
                self.high_since = now_ns
            return now_ns - self.high_since >= 300_000_000
        else:
            self.low_since = self.high_since = None
            self.low_seen = False
        return False
