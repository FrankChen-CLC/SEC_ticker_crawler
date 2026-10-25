"""Offline checks for the historical delisting workflow integration."""
import csv
import os
import subprocess
import sys
from pathlib import Path

from secdata.delisted_universe.scripts import workflow


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def test_check_accepts_updated_universe_sizes(tmp_path, monkeypatch):
    out = tmp_path / "out"
    write_csv(out / "delist_universe.csv", ["cik", "name", "is_delisted"],
              [["1", "First", "1"], ["2", "Second", "0"]])
    write_csv(out / "delist_companies.csv",
              ["cik", "entity_class", "delist_nature", "merger_ev_tier"],
              [["1", "company", "went_dark", ""]])
    write_csv(out / "master_universe.csv",
              ["cik", "was_exchange_listed", "delist_nature"],
              [["1", "1", "went_dark"], ["2", "1", ""]])
    write_csv(out / "filings_last.csv", ["cik", "deregistered", "form15_types"],
              [["1", "0", ""], ["2", "0", ""]])
    write_csv(out / "tickers_all_merged.csv",
              ["cik", "ticker", "first_seen", "last_seen"],
              [["1", "OLD", "2016-01-01", "2017-01-01"]])
    monkeypatch.setattr(workflow, "BASE", str(tmp_path))
    assert workflow.cmd_check() == 0
    write_csv(out / "filings_last.csv", ["cik", "deregistered", "form15_types"],
              [["1", "0", ""]])
    assert workflow.cmd_check() == 1


def test_cli_dry_run_uses_packaged_scripts(tmp_path):
    env = {**os.environ, "SEC_DELISTED_ROOT": str(tmp_path)}
    script = Path(workflow.__file__).resolve()
    result = subprocess.run([sys.executable, "-I", str(script), "run", "--only", "B1",
                             "--dry-run"], env=env, capture_output=True, text=True,
                            check=True)
    assert "merge_master.py" in result.stdout
    assert not (tmp_path / "out" / "workflow.log").exists()
