"""Environment-driven configuration for secdata.

SEC's Fair Access policy: identify yourself with a declared User-Agent that
contains contact information, stay at or below 10 requests/second, and back
off on 429/503. Every knob here has a sane default; only the UA is required.

    SEC_EDGAR_UA="Your Name you@example.com"   (required)
    SEC_EDGAR_SLEEP=0.15                       seconds between uncached requests
    SEC_EDGAR_TIMEOUT=60                       per-request timeout (s)
    SEC_EDGAR_RETRIES=4                        attempts on transient errors
    SEC_EDGAR_CACHE=<path>                     response cache dir (default ~/.cache/secdata)
    SEC_EDGAR_OFFLINE=0                        1 = serve cache only, never hit network
    SEC_EDGAR_OUT=<path>                       default output dir (default ./out)
    SEC_PROXY=http://host:port                 optional http(s) proxy for EDGAR fetches
"""
import os
from pathlib import Path


def ua() -> str:
    v = os.environ.get("SEC_EDGAR_UA", "").strip()
    if not v:
        raise RuntimeError(
            "SEC_EDGAR_UA is not set. SEC requires a declared User-Agent "
            'with contact info, e.g. SEC_EDGAR_UA="Jane Doe jane@example.com"'
        )
    return v


def sleep_interval() -> float:
    return float(os.environ.get("SEC_EDGAR_SLEEP", "0.15"))


def timeout() -> float:
    return float(os.environ.get("SEC_EDGAR_TIMEOUT", "60"))


def retries() -> int:
    return int(os.environ.get("SEC_EDGAR_RETRIES", "4"))


def proxy() -> str:
    return os.environ.get("SEC_PROXY", "").strip()


def offline() -> bool:
    return os.environ.get("SEC_EDGAR_OFFLINE", "0") == "1"


def cache_dir() -> Path:
    p = Path(os.environ.get("SEC_EDGAR_CACHE",
                            Path.home() / ".cache" / "secdata"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir() -> Path:
    return Path(os.environ.get("SEC_EDGAR_OUT", "out"))
