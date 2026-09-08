import pytest

from sleeper.ratelimit import WINDOW, RateLimiter


class Clock:
    def __init__(self):
        self.t = 1_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(tmp_path, host, cap, clock=None, notify=None):
    clock = clock or Clock()
    rl = RateLimiter(host, cap=cap, state_dir=tmp_path, now=clock.now, sleep=clock.sleep, notify=notify or (lambda m: None))
    return rl, clock


def test_501st_sleeper_call_waits_for_the_oldest_to_expire(tmp_path):
    rl, clock = make(tmp_path, "sleeper", 500)
    for _ in range(500):
        rl.acquire()
        clock.t += 0.01
    assert clock.slept == []
    rl.acquire()
    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(WINDOW - 500 * 0.01, abs=0.02)


def test_second_fantasypros_call_waits_a_minute(tmp_path):
    rl, clock = make(tmp_path, "fantasypros", 1)
    rl.acquire()
    clock.t += 1.5
    rl.acquire()
    assert clock.slept == [pytest.approx(WINDOW - 1.5)]


def test_block_from_one_instance_pauses_another(tmp_path):
    a, clock = make(tmp_path, "sleeper", 500)
    b = RateLimiter("sleeper", cap=500, state_dir=tmp_path, now=clock.now, sleep=clock.sleep, notify=lambda m: None)
    a.block(30)
    b.acquire()
    assert clock.slept == [pytest.approx(30)]
    assert a.status()["paused_for_s"] == 0


def test_entries_older_than_the_window_are_dropped(tmp_path):
    rl, clock = make(tmp_path, "fantasypros", 1)
    rl.acquire()
    clock.t += WINDOW + 1
    rl.acquire()
    assert clock.slept == []
    assert rl.status()["sent_last_minute"] == 1


def test_long_waits_are_announced(tmp_path):
    seen = []
    rl, clock = make(tmp_path, "fantasypros", 1, notify=seen.append)
    rl.acquire()
    rl.acquire()
    assert seen == ["rate limit (fantasypros 1/min): waiting 60 s"]


def test_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / "ratelimit.sleeper.json").write_text("{not json")
    rl, clock = make(tmp_path, "sleeper", 500)
    rl.acquire()
    assert rl.status()["sent_last_minute"] == 1


def test_wrong_shaped_and_infinite_state_is_ignored(tmp_path):
    (tmp_path / "ratelimit.sleeper.json").write_text('{"sent": null, "blocked_until": Infinity}')
    rl, clock = make(tmp_path, "sleeper", 500)
    rl.acquire()
    assert clock.slept == [] and rl.status()["sent_last_minute"] == 1
    (tmp_path / "ratelimit.sleeper.json").write_text("[]")
    rl.acquire()
    assert rl.status()["sent_last_minute"] == 1


def test_pause_notice_names_the_429(tmp_path):
    seen = []
    rl, clock = make(tmp_path, "sleeper", 500, notify=seen.append)
    rl.block(30)
    rl.acquire()
    assert seen == ["paused after a 429 from sleeper: waiting 30 s"]
