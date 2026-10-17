"""EDGAR HTTP client: throttled, cached, retrying.

Ported from the production executive-intel client (2026-10 build),
with identity/proxy/storage settings moved to environment variables.

Behaviour preserved from production:
- one global throttle (>= SEC_EDGAR_SLEEP between *uncached* requests;
  cache hits cost nothing);
- every response cached to disk keyed by URL sha1 — re-runs are free and
  polite to SEC (filings are immutable: cache forever by default);
- retries with linear backoff on transient errors; 404 is raised at once;
- keep-alive via a shared requests.Session when `requests` is importable,
  transparent urllib fallback otherwise;
- optional HTTP(S) proxy through SEC_PROXY (some hosting IPs are rate
  limited by SEC; a proxy in front keeps bulk runs alive).
"""
import gzip
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

from . import config

try:
    import requests as _rq
except Exception:  # pragma: no cover - requests is a hard dep, but keep fallback
    _rq = None


class EdgarClient:
    def __init__(self, ua=None, sleep=None, timeout=None, retries=None,
                 proxy=None, offline=None, cache_dir=None):
        self.ua = ua or config.ua()
        self.sleep = sleep if sleep is not None else config.sleep_interval()
        self.timeout = timeout or config.timeout()
        self.retries = retries or config.retries()
        self.proxy = proxy if proxy is not None else config.proxy()
        self.offline = offline if offline is not None else config.offline()
        self.cache_dir = cache_dir or config.cache_dir()
        self._lock = threading.Lock()
        self._last = 0.0
        self._session = None

    # ------------------------------------------------------------------ http
    def _throttle(self):
        # global gate: with the default 0.15 s this stays under 7 req/s even
        # with concurrent workers sharing one client
        with self._lock:
            wait = self._last + self.sleep - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _get_session(self):
        if self._session is None:
            s = _rq.Session()
            s.headers.update({"User-Agent": self.ua,
                              "Accept-Encoding": "gzip, deflate"})
            if self.proxy:
                s.proxies = {"http": self.proxy, "https": self.proxy}
            self._session = s
        return self._session

    def _fetch(self, url: str, timeout=None) -> bytes:
        timeout = timeout or self.timeout
        if _rq is not None:
            r = self._get_session().get(url, timeout=timeout)
            if r.status_code >= 400:
                raise urllib.error.HTTPError(url, r.status_code,
                                             r.reason or "HTTP error",
                                             r.headers, None)
            return r.content
        req = urllib.request.Request(
            url, headers={"User-Agent": self.ua,
                          "Accept-Encoding": "gzip, deflate"})
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
        return data

    # ----------------------------------------------------------------- cache
    def _cache_paths(self, url: str):
        key = hashlib.sha1(url.encode()).hexdigest()[:24]
        return (self.cache_dir / (key + ".bin"),
                self.cache_dir / (key + ".meta.json"))

    def get_bytes(self, url: str, max_age_hours=None, min_cache_bytes=0,
                  timeout=None) -> bytes:
        """Cached GET. max_age_hours=None -> cache forever (immutable docs).

        min_cache_bytes: bodies smaller than this are returned but NOT cached
        — SEC sometimes serves tiny HTTP-200 error pages that must not poison
        the cache (production quirk).
        timeout: per-call override of SEC_EDGAR_TIMEOUT.
        """
        body, meta = self._cache_paths(url)
        if body.exists():
            if max_age_hours is None:
                return body.read_bytes()
            try:
                age = time.time() - json.loads(meta.read_text())["ts"]
                if age < max_age_hours * 3600:
                    return body.read_bytes()
            except Exception:
                pass
        if self.offline:
            raise RuntimeError(f"offline mode and no cache for {url}")
        last_err = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                data = self._fetch(url, timeout=timeout)
                if len(data) >= (min_cache_bytes or 0):
                    body.write_bytes(data)
                    meta.write_text(json.dumps({"ts": time.time(), "url": url}))
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                last_err = e
                # 429/503: SEC asks for exponential backoff
                time.sleep(min(60.0, (2 ** attempt) * (5.0 if e.code in (429, 503) else 1.5)))
            except Exception as e:  # transient network error
                last_err = e
                time.sleep(min(20.0, 1.5 * (attempt + 1)))
        raise last_err

    def get_text(self, url: str, max_age_hours=None, min_cache_bytes=0,
                 timeout=None) -> str:
        return self.get_bytes(url, max_age_hours, min_cache_bytes,
                              timeout).decode("utf-8", "replace")

    def get_json(self, url: str, max_age_hours=None, min_cache_bytes=0,
                 timeout=None):
        return json.loads(self.get_text(url, max_age_hours, min_cache_bytes,
                                        timeout))

    def post_json(self, url: str, payload, headers=None, timeout=None):
        """POST JSON (EDGAR submissions REST API). Returns parsed response."""
        if self.offline:
            raise RuntimeError("offline mode: POST disabled")
        self._throttle()
        h = {"User-Agent": self.ua, "Content-Type": "application/json"}
        if headers:
            h.update(headers)
        if _rq is not None:
            r = self._get_session().post(url, json=payload, headers=h,
                                         timeout=timeout or self.timeout)
            r.raise_for_status()
            return r.json()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))


_default = None
_default_lock = threading.Lock()


def default_client() -> EdgarClient:
    global _default
    with _default_lock:
        if _default is None:
            _default = EdgarClient()
    return _default
