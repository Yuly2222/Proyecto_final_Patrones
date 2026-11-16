"""Unit tests de la lógica SAGA y compensaciones (sin red ni DynamoDB)."""
from saga import StepFailed, run_saga


class MemoryLog:
    def __init__(self):
        self.saved = {}

    def start(self, s):
        self.saved[s["saga_id"]] = s

    def step(self, s, *a, **k):
        s["steps"].append({"step": a[0], "ok": a[1]})

    def save(self, s):
        pass


def fake_call(fail_on=None, fail_times=99):
    calls, failures = [], {"n": 0}

    def call(service, path, payload):
        calls.append(f"{service}{path}")
        if fail_on == f"{service}{path}" and failures["n"] < fail_times:
            failures["n"] += 1
            raise StepFailed("boom")
        return {"status": "ok", "amount": "10.50"}

    return call, calls


def test_happy_path_completes():
    call, calls = fake_call()
    saga = run_saga(call, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "COMPLETED"
    assert calls == ["reservation/reserve", "billing/charge", "dispatch/start", "reservation/confirm"]


def test_charger_fault_refunds_then_releases():
    call, calls = fake_call(fail_on="dispatch/start")
    saga = run_saga(call, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "COMPENSATED"
    assert calls[3:] == ["billing/refund", "reservation/release"]  # orden inverso


def test_insufficient_funds_only_releases():
    call, calls = fake_call(fail_on="billing/charge")
    saga = run_saga(call, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "COMPENSATED"
    assert calls[2:] == ["reservation/release"]


def test_reserve_failure_has_nothing_to_compensate():
    call, calls = fake_call(fail_on="reservation/reserve")
    saga = run_saga(call, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "FAILED"
    assert calls == ["reservation/reserve"]


def test_compensation_is_retried():
    call, calls = fake_call(fail_on="billing/refund", fail_times=2)
    call_dispatch_fails = lambda s, p, d: (_ for _ in ()).throw(StepFailed("hw")) if p == "/start" else call(s, p, d)
    saga = run_saga(call_dispatch_fails, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "COMPENSATED"
    assert calls.count("billing/refund") == 3


def test_compensation_gives_up_after_max_retries():
    call, _ = fake_call(fail_on="reservation/release")
    call_billing_fails = lambda s, p, d: (_ for _ in ()).throw(StepFailed("402")) if p == "/charge" else call(s, p, d)
    saga = run_saga(call_billing_fails, "C1", "F1", 30, MemoryLog())
    assert saga["status"] == "COMPENSATION_FAILED"
