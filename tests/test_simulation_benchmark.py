"""Evidence files must be refused before the compiled benchmark is acquired."""
from pathlib import Path
import pytest
from tools import benchmark_simulation as benchmark


@pytest.mark.parametrize("occupied", ["report", "dangling-assets"])
def test_existing_evidence_is_not_overwritten(tmp_path, occupied):
    report = tmp_path / "run.json"
    if occupied == "report":
        report.write_bytes(b"prior measurement")
    else:
        (tmp_path / "run-assets").symlink_to(tmp_path / "unavailable")
    with pytest.raises(FileExistsError):
        benchmark.main(["--case", "all", "--report", str(report)])
    if occupied == "report":
        assert report.read_bytes() == b"prior measurement"
    else:
        assert not report.exists()
        assert (tmp_path / "run-assets").readlink() == tmp_path / "unavailable"
