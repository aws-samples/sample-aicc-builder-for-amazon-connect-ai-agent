"""S3 Files store: history writes must survive lone surrogates (cancelled streams)."""
import json
from pathlib import Path

from context.s3files_store import S3FilesContextStore


def test_write_json_survives_lone_surrogate(tmp_path: Path):
    store = S3FilesContextStore.__new__(S3FilesContextStore)
    target = tmp_path / "context" / "conversation_history.json"
    data = [{"role": "assistant", "content": [{"text": "cut mid-emoji \ud83d"}]}]

    assert store._write_json(target, data) is True
    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded[0]["role"] == "assistant"
    assert "cut mid-emoji" in loaded[0]["content"][0]["text"]
