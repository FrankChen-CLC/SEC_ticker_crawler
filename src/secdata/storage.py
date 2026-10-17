"""Local storage helpers shared by every pipeline module.

Production writes into MySQL; the open-source edition keeps the same
*shapes* (row dicts) but lands them in plain files:

- ``out/<pipeline>/…`` JSONL — append-friendly, grep-able, pandas-able;
- ``out/<pipeline>/docs/…`` raw documents (XML/HTML/SGML), mirroring the
  EDGAR paths so links stay reconstructible;
- resume checkpoints (done/failed line files + a small JSON state), the
  pattern the production daily crons rely on.

No database server required; JSONL is enough to feed anything downstream.
"""
import json
import os
import time
from pathlib import Path


def ensure_dir(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_root(sub=None, base=None) -> Path:
    """Default output dir: $SEC_EDGAR_OUT or ./out, plus optional subfolder."""
    root = Path(base or os.environ.get("SEC_EDGAR_OUT", "out"))
    if sub:
        root = root / sub
    return ensure_dir(root)


def today() -> str:
    return time.strftime("%Y-%m-%d")


class JsonlWriter:
    """Append JSONL rows; optional in-process key dedupe."""

    def __init__(self, path, key=None):
        self.path = Path(path)
        ensure_dir(self.path.parent)
        self.key = key
        self._seen = set()
        if key:
            for line in _read_lines(self.path):
                try:
                    self._seen.add(json.loads(line).get(key))
                except Exception:
                    pass
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, row: dict) -> bool:
        if self.key and row.get(self.key) in self._seen:
            return False
        self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        if self.key:
            self._seen.add(row.get(self.key))
        return True

    def write_many(self, rows) -> int:
        return sum(1 for r in rows if self.write(r))

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def write_jsonl(path, rows):
    with JsonlWriter(path) as w:
        n = w.write_many(rows)
    return n


def read_jsonl(path):
    for line in _read_lines(Path(path)):
        try:
            yield json.loads(line)
        except Exception:
            continue


def _read_lines(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


class Checkpoint:
    """done/failed/resume bookkeeping, one line per key (production pattern)."""

    def __init__(self, dirpath):
        self.dir = ensure_dir(dirpath)
        self.done_file = self.dir / "done.txt"
        self.failed_file = self.dir / "failed.txt"

    def done(self) -> set:
        return set(_read_lines(self.done_file))

    def failed(self) -> set:
        return set(_read_lines(self.failed_file))

    def mark_done(self, key: str):
        with self.done_file.open("a", encoding="utf-8") as f:
            f.write(key + "\n")

    def mark_failed(self, key: str):
        with self.failed_file.open("a", encoding="utf-8") as f:
            f.write(key + "\n")


class State:
    """Small mutable JSON state file (watermarks, cursor, last run info)."""

    def __init__(self, path, default=None):
        self.path = Path(path)
        ensure_dir(self.path.parent)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}
        if default:
            for k, v in default.items():
                self.data.setdefault(k, v)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)


def save_doc(client_dir, name, content: bytes) -> Path:
    """Write a raw document under a pipeline's docs dir, atomically."""
    d = ensure_dir(client_dir)
    target = d / name
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(content)
    os.replace(tmp, target)
    return target
