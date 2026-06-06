import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_file_cache_analysis_recovers_from_empty_cache_file(tmp_path, monkeypatch):
    import board_service_chroma

    monkeypatch.setattr(board_service_chroma, "PROJECT_ROOT", str(tmp_path))

    cache_dir = tmp_path / "APP" / "backend" / "data_cache"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "analysis_cache.json"
    cache_file.write_text("", encoding="utf-8")

    worker = board_service_chroma.AIAnalysisWorker.__new__(board_service_chroma.AIAnalysisWorker)
    worker._file_cache_analysis(
        "MYPROJECT-TEST-2001",
        {
            "recommended_team": "云平台-流程中心",
            "recommended_role": "产品经理",
        },
    )

    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved["MYPROJECT-TEST-2001"]["recommended_team"] == "云平台-流程中心"
    assert saved["MYPROJECT-TEST-2001"]["recommended_role"] == "产品经理"
    assert saved["MYPROJECT-TEST-2001"]["cache_type"] == "file"
