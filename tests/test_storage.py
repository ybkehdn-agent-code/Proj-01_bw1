"""`storage.py` 테스트 — SQLite WAL 저장/조회, 스키마, 저장소 인터페이스."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import storage
from storage import GameLog, LogRepository, RoundLog, SQLiteLogRepository


def make_round(set_index=0, round_number=1, first="A", p1=8, p2=3, winner="A"):
    p1_color = "black" if p1 % 2 == 0 else "white"
    p2_color = "black" if p2 % 2 == 0 else "white"
    return RoundLog(
        set_index=set_index,
        round=round_number,
        first_player=first,
        p1_tile=p1,
        p2_tile=p2,
        p1_color=p1_color,
        p2_color=p2_color,
        round_winner=winner,
        p1_score=1 if winner == "A" else 0,
        p2_score=1 if winner == "B" else 0,
        think_time_ms=1200,
        second_think_time_ms=800,
    )


def make_game(game_id="g1", rounds=None, **overrides):
    payload = dict(
        game_id=game_id,
        room_id="room-abc123",
        created_at="2026-11-25T12:00:00+00:00",
        ended_at="2026-11-25T12:07:00+00:00",
        engine_version="1.0.0",
        mode="human_vs_human",
        end_reason="early_win",
        winner="A",
        total_sets=1,
        p1_score=5,
        p2_score=1,
        rounds=rounds if rounds is not None else [make_round()],
    )
    payload.update(overrides)
    return GameLog(**payload)


# ---------------------------------------------------------------------------
# 인터페이스
# ---------------------------------------------------------------------------
def test_log_repository_is_abstract() -> None:
    with pytest.raises(TypeError):
        LogRepository()  # type: ignore[abstract]


def test_sqlite_repository_implements_interface(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        assert isinstance(repo, LogRepository)
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# WAL / 스키마
# ---------------------------------------------------------------------------
def test_wal_mode_is_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "games.db"
    repo = SQLiteLogRepository(db_path)
    try:
        assert db_path.exists()  # 상위 폴더 자동 생성
        assert repo.journal_mode == "wal"
        # 실제 커넥션의 PRAGMA 값도 WAL 이어야 한다.
        mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]  # noqa: SLF001
        assert str(mode).lower() == "wal"
        # WAL 모드에서는 -wal / -shm 보조 파일이 생긴다(쓰기 이후).
        repo.save_game(make_game())
        assert Path(str(db_path) + "-wal").exists()
    finally:
        repo.close()


def test_schema_has_required_columns(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        conn = sqlite3.connect(tmp_path / "games.db")
        try:
            game_cols = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
            round_cols = {row[1] for row in conn.execute("PRAGMA table_info(rounds)")}
        finally:
            conn.close()
    finally:
        repo.close()

    assert {
        "game_id",
        "room_id",
        "created_at",
        "ended_at",
        "engine_version",
        "ai_version",
        "mode",
        "end_reason",
        "winner",
        "total_sets",
    } <= game_cols
    assert {
        "game_id",
        "set_index",
        "round",
        "first_player",
        "p1_tile",
        "p2_tile",
        "p1_color",
        "p2_color",
        "round_winner",
        "p1_score",
        "p2_score",
        "think_time_ms",
    } <= round_cols


def test_gitignore_excludes_local_data_dir() -> None:
    gitignore = Path(storage.__file__).resolve().parent / ".gitignore"
    lines = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]
    assert any(line in ("data/", "/data/", "data", "/data") for line in lines)


# ---------------------------------------------------------------------------
# 저장/조회
# ---------------------------------------------------------------------------
def test_save_and_get_game_roundtrip(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        log = make_game(
            rounds=[
                make_round(0, 1, "A", 8, 3, "A"),
                make_round(0, 2, "A", 0, 7, "B"),
            ]
        )
        repo.save_game(log)

        stored = repo.get_game("g1")
        assert stored is not None
        assert stored["game_id"] == "g1"
        assert stored["room_id"] == "room-abc123"
        assert stored["engine_version"] == "1.0.0"
        assert stored["ai_version"] is None
        assert stored["mode"] == "human_vs_human"
        assert stored["end_reason"] == "early_win"
        assert stored["winner"] == "A"
        assert stored["total_sets"] == 1
        assert stored["p1_score"] == 5 and stored["p2_score"] == 1

        assert len(stored["rounds"]) == 2
        first = stored["rounds"][0]
        assert first["first_player"] == "A"
        assert first["p1_tile"] == 8 and first["p2_tile"] == 3
        assert first["p1_color"] == "black" and first["p2_color"] == "white"
        assert first["round_winner"] == "A"
        assert first["think_time_ms"] == 1200
        assert first["second_think_time_ms"] == 800
    finally:
        repo.close()


def test_get_missing_game_returns_none(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        assert repo.get_game("nope") is None
        assert repo.count_games() == 0
    finally:
        repo.close()


def test_ai_version_and_mode_for_ai_games(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        repo.save_game(
            make_game(
                game_id="ai1",
                mode="human_vs_ai_easy",
                ai_version="rules-v0.1",
                end_reason="normal",
                rounds=[make_round(0, 1)],
            )
        )
        stored = repo.get_game("ai1")
        assert stored["mode"] == "human_vs_ai_easy"
        assert stored["ai_version"] == "rules-v0.1"
    finally:
        repo.close()


def test_rounds_are_ordered_by_set_then_round(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        rounds = [
            make_round(1, 1, "B", 6, 2, "A"),
            make_round(0, 2, "A", 4, 5, "B"),
            make_round(0, 1, "A", 8, 1, "A"),
        ]
        repo.save_game(make_game(rounds=rounds, total_sets=2))
        stored = repo.get_game("g1")
        keys = [(r["set_index"], r["round"]) for r in stored["rounds"]]
        assert keys == [(0, 1), (0, 2), (1, 1)]
    finally:
        repo.close()


def test_save_is_idempotent_and_replaces_rounds(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        repo.save_game(make_game(rounds=[make_round(0, 1), make_round(0, 2)]))
        repo.save_game(make_game(rounds=[make_round(0, 1)], winner="B", end_reason="forfeit"))

        stored = repo.get_game("g1")
        assert repo.count_games() == 1
        assert stored["winner"] == "B"
        assert stored["end_reason"] == "forfeit"
        assert len(stored["rounds"]) == 1  # 이전 라운드 행이 남지 않는다
    finally:
        repo.close()


def test_list_games_is_newest_first_with_round_count(tmp_path: Path) -> None:
    repo = SQLiteLogRepository(tmp_path / "games.db")
    try:
        repo.save_game(
            make_game(game_id="old", ended_at="2026-11-20T10:00:00+00:00",
                      rounds=[make_round(0, 1)])
        )
        repo.save_game(
            make_game(game_id="new", ended_at="2026-11-25T10:00:00+00:00",
                      rounds=[make_round(0, 1), make_round(0, 2)])
        )
        games = repo.list_games(limit=10)
        assert [g["game_id"] for g in games] == ["new", "old"]
        assert games[0]["round_count"] == 2
        assert "rounds" not in games[0]  # 목록은 헤더만 가볍게
    finally:
        repo.close()


def test_memory_database_works() -> None:
    repo = SQLiteLogRepository(":memory:")
    try:
        repo.save_game(make_game(game_id="mem"))
        assert repo.get_game("mem")["game_id"] == "mem"
        assert repo.count_games() == 1
    finally:
        repo.close()
