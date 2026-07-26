from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest

from orchestune.json_state import read_json_with_recovery, write_json_atomic


class TestWriteJsonAtomic:
    def test_write_then_read_round_trips(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"foo": "bar", "n": 1})
        assert path.read_text(encoding="utf-8").strip().startswith("{")
        assert read_json_with_recovery(path, label="state.json") == {
            "foo": "bar",
            "n": 1,
        }

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.json"
        write_json_atomic(path, {"a": 1})
        assert path.exists()

    def test_no_leftover_tmp_file_after_success(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"a": 1})
        leftovers = list(tmp_path.glob("*.tmp.*"))
        assert leftovers == []

    def test_failure_during_replace_leaves_old_content_intact(self, tmp_path):
        """os.replace()到達前後どちらで失敗しても、読み取り側は旧完全版か新完全版
        のどちらかしか観測しない（部分書き込みが漏れない）ことを確認する。"""
        path = tmp_path / "state.json"
        write_json_atomic(path, {"version": "old"})

        with patch("orchestune.json_state.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                write_json_atomic(path, {"version": "new"})

        # 旧内容がそのまま読み取れる（新内容の部分書き込みは観測されない）。
        assert read_json_with_recovery(path, label="state.json") == {"version": "old"}
        # 失敗した一時ファイルは残置されない。
        leftovers = list(tmp_path.glob("*.tmp.*"))
        assert leftovers == []

    def test_failure_during_write_does_not_touch_existing_file(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"version": "old"})

        with patch("orchestune.json_state.json.dumps", side_effect=ValueError("boom")):
            with pytest.raises(ValueError):
                write_json_atomic(path, {"version": "new"})

        assert read_json_with_recovery(path, label="state.json") == {"version": "old"}

    def test_concurrent_writes_to_same_path_do_not_collide(self, tmp_path):
        """一時ファイル名はPIDだけでなく`tempfile.mkstemp`で毎回一意に採番される
        ため、同一プロセス内で同じパスへ並行書き込みが発生しても、片方の
        `os.replace()`後にもう片方が一時ファイルを見失って失敗する、あるいは
        内容が競合するということが起きないことを確認する。"""
        path = tmp_path / "state.json"
        write_json_atomic(path, {"version": "seed"})

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def writer(version: str) -> None:
            barrier.wait()
            try:
                write_json_atomic(path, {"version": version})
            except BaseException as exc:  # noqa: BLE001 - スレッド側の例外を集める
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=("a",)),
            threading.Thread(target=writer, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # 一時ファイルの取り違えが起きていれば衝突・残置が発生する。
        assert list(tmp_path.glob("*.tmp.*")) == []
        # 最終的な内容はどちらかの書き込み結果と完全一致し、破損・混在はない。
        final = json.loads(path.read_text(encoding="utf-8"))
        assert final in ({"version": "a"}, {"version": "b"})


class TestReadJsonWithRecovery:
    def test_missing_file_returns_none(self, tmp_path):
        assert read_json_with_recovery(tmp_path / "missing.json", label="x") is None

    def test_valid_file_returns_parsed_data(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"pending": []})
        assert read_json_with_recovery(path, label="x") == {"pending": []}

    def test_corrupted_file_is_quarantined_and_returns_none(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")

        result = read_json_with_recovery(path, label="state.json")

        assert result is None
        # 破損ファイルは元の場所には残らない(次回サイクルの自己修復が"未存在"として
        # 扱えるように)。
        assert not path.exists()
        quarantined = list(tmp_path.glob("state.json.corrupt.*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "{not valid json"

    def test_quarantine_warning_printed_to_stderr(self, tmp_path, capsys):
        path = tmp_path / "state.json"
        path.write_text("not json at all", encoding="utf-8")

        read_json_with_recovery(path, label="run_state.json")

        captured = capsys.readouterr()
        assert "run_state.json" in captured.err
        assert "corrupt" in captured.err.lower() or "quarantin" in captured.err.lower()

    def test_transient_os_error_propagates_instead_of_falling_back(self, tmp_path):
        """`OSError`（一時的なI/O障害やPermissionErrorなど）はファイル内容の破損を
        意味しないため、Noneへフォールバックさせず呼び出し元へ伝播させる。ここで
        Noneを返してしまうと、正常な状態ファイルを誤って空状態として扱い、後続の
        保存で既存状態を失いかねない。"""
        path = tmp_path / "state.json"
        write_json_atomic(path, {"pending": []})

        with patch(
            "orchestune.json_state.Path.read_text",
            side_effect=OSError("transient I/O error"),
        ):
            with pytest.raises(OSError):
                read_json_with_recovery(path, label="state.json")

        # 例外伝播時は元のファイルもquarantineされず残っている。
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {"pending": []}

    def test_quarantine_failure_propagates_instead_of_falling_back(self, tmp_path):
        """破損ファイルのquarantine（`os.replace`）自体が失敗した場合も、Noneへ
        フォールバックさせず例外を伝播させる（サイレントなデータ消失を防ぐ）。"""
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")

        with patch(
            "orchestune.json_state.os.replace", side_effect=OSError("cannot move")
        ):
            with pytest.raises(OSError):
                read_json_with_recovery(path, label="state.json")

        # quarantineに失敗しているため、破損ファイルは元の場所に残る。
        assert path.exists()
