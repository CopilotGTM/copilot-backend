import os

from src.infra.observability import process_memory


def test_get_current_rss_bytes_reads_proc_statm(tmp_path, monkeypatch) -> None:
    statm_path = tmp_path / "statm"
    statm_path.write_text("100 25 0 0 0 0 0\n")
    monkeypatch.setattr(os, "sysconf", lambda name: 4096, raising=False)

    assert process_memory.get_current_rss_bytes(statm_path) == 25 * 4096
    assert process_memory.get_current_rss_mb(statm_path) == (25 * 4096) / (1024 * 1024)


def test_get_current_rss_bytes_returns_none_for_missing_or_invalid_proc_file(tmp_path) -> None:
    missing_path = tmp_path / "missing"
    invalid_path = tmp_path / "statm"
    invalid_path.write_text("not-a-statm-file\n")

    assert process_memory.get_current_rss_bytes(missing_path) is None
    assert process_memory.get_current_rss_bytes(invalid_path) is None


def test_get_current_rss_bytes_never_raises(tmp_path, monkeypatch) -> None:
    statm_path = tmp_path / "statm"
    statm_path.write_text("100 25 0 0 0 0 0\n")

    def _raise_sysconf(name: str) -> int:
        raise OSError("sysconf unavailable")

    monkeypatch.setattr(os, "sysconf", _raise_sysconf, raising=False)

    assert process_memory.get_current_rss_bytes(statm_path) is None
