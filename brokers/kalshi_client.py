"""
brokers/kalshi_client.py
------------------------
Shared Kalshi API client — M-7/RF-4

Features:
  - Private key loaded once at startup and cached (no per-call disk I/O)
  - Thread-safe token-bucket rate limiter (default 10 req/s, burst 20)
  - 429 exponential backoff: 1s → 2s → 4s → 8s → 16s (max 30s)
  - Per-scan stats: request count, effective rate, throttle events, retries
"""

import os
import time
import base64
import threading
import requests
from typing import Optional

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID          = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.getenv(
    "KALSHI_PRIVATE_KEY_PATH",
    "/Users/samuel/Desktop/AI_System/kalshi_private_key.pem",
)

RATE_LIMIT  = float(os.getenv("KALSHI_RATE_LIMIT", "10.0"))  # req/s
BURST_SIZE  = int(os.getenv("KALSHI_BURST", "20"))            # burst tokens
MAX_RETRIES = 4
BACKOFF_BASE = 1.0  # seconds; doubles each retry, capped at 30s


# ─────────────────────────────────────────
# TOKEN BUCKET
# ─────────────────────────────────────────

class _TokenBucket:
    """
    Thread-safe token bucket.

    Threads call wait() before issuing a request.  If a token is available
    it returns immediately; otherwise it sleeps the minimum time needed for
    one token to accumulate, then retries.  Sleeping happens *outside* the
    lock so other threads can reserve tokens concurrently.
    """

    def __init__(self, rate: float, capacity: float):
        self._rate     = rate      # tokens added per second
        self._capacity = capacity  # max token count
        self._tokens   = capacity  # start full
        self._last     = time.monotonic()
        self._lock     = threading.Lock()

    def wait(self) -> float:
        """Block until a token is available.  Returns actual wait time (s)."""
        t0 = time.monotonic()
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return time.monotonic() - t0

                wait_s = (1.0 - self._tokens) / self._rate

            time.sleep(wait_s)


# ─────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────

class KalshiClient:
    """
    Shared Kalshi REST client.

    Use the module-level kalshi_get() for ordinary call sites.
    Use get_client().start_scan() / get_client().scan_stats() for timing.
    """

    def __init__(self, rate: float = RATE_LIMIT, burst: int = BURST_SIZE):
        self._rate_limiter = _TokenBucket(rate, burst)

        # Key cache — loaded once, then reused
        self._private_key  = None
        self._key_lock     = threading.Lock()

        # Per-scan stats (reset by start_scan)
        self._requests_total    = 0
        self._requests_throttled = 0
        self._requests_retried  = 0
        self._throttle_ms_total = 0.0
        self._scan_start: Optional[float] = None

    # ── private key (cached) ──────────────────────────────────────

    def _load_key(self):
        """Return cached private key, loading from disk only on first call."""
        if self._private_key is not None:
            return self._private_key
        with self._key_lock:
            if self._private_key is not None:
                return self._private_key
            try:
                with open(PRIVATE_KEY_PATH, "rb") as fh:
                    self._private_key = serialization.load_pem_private_key(
                        fh.read(), password=None
                    )
                print(f"[API] Private key loaded ({PRIVATE_KEY_PATH})")
            except Exception as exc:
                print(f"[AUTH ERROR] Cannot load private key: {exc}")
                return None
        return self._private_key

    def _build_headers(self, method: str, path: str) -> dict:
        key = self._load_key()
        if not key:
            return {}
        ts  = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY":       KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type":            "application/json",
        }

    # ── public API ────────────────────────────────────────────────

    def get(self, path: str, silent: bool = False) -> Optional[dict]:
        """Rate-limited GET with 429 exponential backoff."""
        for attempt in range(MAX_RETRIES + 1):
            # Throttle
            wait_s = self._rate_limiter.wait()
            if wait_s > 0.005:
                self._requests_throttled += 1
                self._throttle_ms_total  += wait_s * 1000

            headers = self._build_headers("GET", path)
            if not headers:
                return None

            try:
                r = requests.get(
                    KALSHI_API_BASE + path,
                    headers=headers,
                    timeout=10,
                )
                self._requests_total += 1

                if r.status_code == 200:
                    return r.json()

                if r.status_code == 429:
                    backoff = min(BACKOFF_BASE * (2 ** attempt), 30.0)
                    self._requests_retried += 1
                    if not silent:
                        print(
                            f"[API] 429 rate-limited — backoff {backoff:.1f}s"
                            f" (attempt {attempt + 1}/{MAX_RETRIES})"
                        )
                    time.sleep(backoff)
                    continue

                if not silent:
                    print(f"[API] {path} → {r.status_code}")
                return None

            except Exception as exc:
                if not silent:
                    print(f"[API ERROR] {path}: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE * (2 ** attempt))
                    continue
                return None

        return None

    # ── scan timing ───────────────────────────────────────────────

    def start_scan(self):
        """Reset per-scan counters.  Call at the top of each scan."""
        self._scan_start         = time.monotonic()
        self._requests_total     = 0
        self._requests_throttled = 0
        self._requests_retried   = 0
        self._throttle_ms_total  = 0.0

    def scan_stats(self) -> dict:
        """Return timing/rate stats for the scan started by start_scan()."""
        elapsed = (
            time.monotonic() - self._scan_start
            if self._scan_start is not None
            else 0.0
        )
        rate = self._requests_total / elapsed if elapsed > 0 else 0.0
        return {
            "requests":    self._requests_total,
            "elapsed_s":   round(elapsed, 1),
            "rate_rps":    round(rate, 1),
            "throttled":   self._requests_throttled,
            "retried":     self._requests_retried,
            "wait_ms_total": round(self._throttle_ms_total),
        }


# ─────────────────────────────────────────
# MODULE-LEVEL SHARED INSTANCE
# ─────────────────────────────────────────

_client = KalshiClient(rate=RATE_LIMIT, burst=BURST_SIZE)


def kalshi_get(path: str, silent: bool = False) -> Optional[dict]:
    """Drop-in replacement for the old inline kalshi_get()."""
    return _client.get(path, silent=silent)


def get_client() -> KalshiClient:
    """Return the shared client (for start_scan / scan_stats)."""
    return _client
