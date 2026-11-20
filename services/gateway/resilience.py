"""Patrones de resiliencia y aislamiento del gateway: Circuit Breaker + Semaphore Bulkhead (uno por servicio)."""
import threading
import time


class CircuitOpen(Exception):
    pass


class BulkheadFull(Exception):
    pass


class CircuitBreaker:
    """CLOSED -> (FAIL_THRESHOLD fallos seguidos) -> OPEN -> (OPEN_SECONDS) -> HALF_OPEN -> 1 éxito cierra / 1 fallo reabre."""

    def __init__(self, fail_threshold: int = 5, open_seconds: float = 15, clock=time.monotonic):
        self.fail_threshold, self.open_seconds, self.clock = fail_threshold, open_seconds, clock
        self.failures, self.opened_at = 0, None
        self.lock = threading.Lock()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self.clock() - self.opened_at >= self.open_seconds else "OPEN"

    def before(self):
        with self.lock:
            if self.state == "OPEN":
                raise CircuitOpen()  # falla rápido: no se espera el timeout de un servicio caído

    def success(self):
        with self.lock:
            self.failures, self.opened_at = 0, None

    def failure(self):
        with self.lock:
            self.failures += 1
            if self.state == "HALF_OPEN" or self.failures >= self.fail_threshold:
                self.opened_at = self.clock()


class Bulkhead:
    """Semaphore Bulkhead: máximo `size` llamadas simultáneas a un servicio. Si uno se pone lento solo agota
    SU cupo; los hilos del gateway quedan libres para los otros servicios."""

    def __init__(self, size: int = 10, wait_seconds: float = 1):
        self.sem, self.wait_seconds = threading.BoundedSemaphore(size), wait_seconds

    def __enter__(self):
        if not self.sem.acquire(timeout=self.wait_seconds):
            raise BulkheadFull()

    def __exit__(self, *exc):
        self.sem.release()
