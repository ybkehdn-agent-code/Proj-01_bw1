"""게임 로그 저장소 — ``LogRepository`` 인터페이스 + 로컬 SQLite(WAL) 구현.

설계 원칙
----------
- ``server.py`` 는 파일을 직접 쓰지 않고 **이 인터페이스만** 호출한다.
  배포 단계에서 영구 볼륨(Railway Volume)이나 무료 클라우드 DB
  (Supabase / Neon PostgreSQL)로 옮길 때 ``LogRepository`` 구현만 갈아끼우면 된다.
- SQLite 는 **WAL 모드**로 열어 로그 적재(쓰기)와 분석 쿼리(읽기)가 서로를
  오래 막지 않게 한다.
- 한 판이 끝나면 게임 1행 + 라운드 N행을 **하나의 트랜잭션**으로 기록한다.
  중간에 서버가 죽어도 반쯤 쓰인 판이 남지 않는다.

로그 스키마(RL 학습 대비)
-------------------------
``games``  게임 1판 = 1행
    game_id, room_id, created_at, ended_at, engine_version, ai_version(NULL=사람 대전),
    mode(human_vs_human / human_vs_ai_easy ...), end_reason(normal/early_win/forfeit/disconnect),
    winner("A"/"B"/NULL), total_sets, p1_score, p2_score

``rounds`` 라운드 1개 = 1행
    game_id, set_index(본 게임 0, 연장전 1,2...), round(1~9), first_player,
    p1_tile, p2_tile, p1_color, p2_color, round_winner, p1_score, p2_score,
    think_time_ms(선공 고민 시간), second_think_time_ms(후공 고민 시간)

``p1`` / ``p2`` 는 **좌석(플레이어 A/B)** 을 가리킨다. 누가 선공이었는지는
``first_player`` 컬럼으로 구분하며, 그래서 ``p1_tile`` 은 항상 A 의 타일이다.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "RoundLog",
    "GameLog",
    "LogRepository",
    "SQLiteLogRepository",
]

#: 기본 DB 파일 위치(프로젝트 루트의 ``data/games.db``). ``.gitignore`` 에 ``data/`` 가 들어 있다.
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "games.db"

SCHEMA_VERSION = 1

GAME_MODES = (
    "human_vs_human",
    "human_vs_ai_easy",
    "human_vs_ai_normal",  # 3단계: ai.py 의 normal 난이도
    "human_vs_ai_medium",  # 예전 표기(호환용, 새 코드는 normal 을 쓴다)
    "human_vs_ai_hard",
)
END_REASONS = ("normal", "early_win", "forfeit", "disconnect")


# ---------------------------------------------------------------------------
# 레코드
# ---------------------------------------------------------------------------
@dataclass
class RoundLog:
    """라운드 1개의 기록. 숫자·색상·승패·누적 점수·고민 시간을 함께 남긴다."""

    set_index: int
    round: int
    first_player: str
    p1_tile: Optional[int] = None
    p2_tile: Optional[int] = None
    p1_color: Optional[str] = None
    p2_color: Optional[str] = None
    round_winner: Optional[str] = None
    p1_score: int = 0
    p2_score: int = 0
    think_time_ms: Optional[int] = None
    second_think_time_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "set_index": self.set_index,
            "round": self.round,
            "first_player": self.first_player,
            "p1_tile": self.p1_tile,
            "p2_tile": self.p2_tile,
            "p1_color": self.p1_color,
            "p2_color": self.p2_color,
            "round_winner": self.round_winner,
            "p1_score": self.p1_score,
            "p2_score": self.p2_score,
            "think_time_ms": self.think_time_ms,
            "second_think_time_ms": self.second_think_time_ms,
        }


@dataclass
class GameLog:
    """게임 1판의 기록(헤더 + 라운드 목록)."""

    game_id: str
    room_id: str
    created_at: str
    ended_at: str
    engine_version: str
    mode: str = "human_vs_human"
    end_reason: str = "normal"
    winner: Optional[str] = None
    total_sets: int = 1
    ai_version: Optional[str] = None
    p1_score: int = 0
    p2_score: int = 0
    rounds: List[RoundLog] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "room_id": self.room_id,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "engine_version": self.engine_version,
            "mode": self.mode,
            "end_reason": self.end_reason,
            "winner": self.winner,
            "total_sets": self.total_sets,
            "ai_version": self.ai_version,
            "p1_score": self.p1_score,
            "p2_score": self.p2_score,
            "rounds": [r.to_dict() for r in self.rounds],
        }


# ---------------------------------------------------------------------------
# 인터페이스
# ---------------------------------------------------------------------------
class LogRepository(ABC):
    """게임 로그 저장 인터페이스(교체 가능한 저장 계층).

    구현체는 스레드 세이프해야 하며(서버가 ``asyncio.to_thread`` 로 부를 수 있음),
    ``save_game`` 은 같은 ``game_id`` 를 다시 저장하면 덮어쓴다(멱등).
    """

    @abstractmethod
    def save_game(self, log: GameLog) -> None:
        """게임 1판(라운드 포함)을 저장한다."""

    @abstractmethod
    def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        """게임 1판을 라운드 목록과 함께 dict 로 돌려준다(없으면 ``None``)."""

    @abstractmethod
    def list_games(self, limit: int = 50) -> List[Dict[str, Any]]:
        """최근 게임 헤더 목록(최신 ``ended_at`` 순)."""

    def count_games(self) -> int:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 기본은 아무것도 하지 않음
        """자원 정리(커넥션 종료 등)."""


# ---------------------------------------------------------------------------
# SQLite 구현
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    game_id        TEXT PRIMARY KEY,
    room_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    ai_version     TEXT,
    mode           TEXT NOT NULL,
    end_reason     TEXT NOT NULL,
    winner         TEXT,
    total_sets     INTEGER NOT NULL DEFAULT 1,
    p1_score       INTEGER NOT NULL DEFAULT 0,
    p2_score       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rounds (
    game_id              TEXT NOT NULL,
    set_index            INTEGER NOT NULL,
    round                INTEGER NOT NULL,
    first_player         TEXT NOT NULL,
    p1_tile              INTEGER,
    p2_tile              INTEGER,
    p1_color             TEXT,
    p2_color             TEXT,
    round_winner         TEXT,
    p1_score             INTEGER NOT NULL DEFAULT 0,
    p2_score             INTEGER NOT NULL DEFAULT 0,
    think_time_ms        INTEGER,
    second_think_time_ms INTEGER,
    PRIMARY KEY (game_id, set_index, round),
    FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_games_ended_at ON games(ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_rounds_game ON rounds(game_id, set_index, round);
"""


class SQLiteLogRepository(LogRepository):
    """SQLite(WAL) 기반 게임 로그 저장소.

    Parameters
    ----------
    path:
        DB 파일 경로. ``":memory:"`` 를 주면 메모리 DB(테스트용)로 연다.
        기본값은 ``data/games.db`` 이며 상위 폴더는 자동 생성한다.
    timeout:
        SQLite 잠금 대기 시간(초).
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        timeout: float = 5.0,
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=timeout,
            check_same_thread=False,
            isolation_level=None,  # 트랜잭션은 직접 제어한다
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self.journal_mode = self._prepare_journal_mode()
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- 초기화 ------------------------------------------------------------
    def _prepare_journal_mode(self) -> str:
        """WAL 모드를 켠다(메모리 DB 는 WAL 이 없어 ``memory`` 로 남는다)."""

        try:
            row = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
            mode = str(row[0]).lower() if row else ""
        except sqlite3.DatabaseError:  # pragma: no cover - 파일 시스템 제약
            mode = ""
        if mode != "wal":
            # WAL 을 못 쓰는 환경(네트워크 FS 등)에서도 동작은 계속한다.
            self._conn.execute("PRAGMA synchronous = NORMAL")
            return mode or "delete"
        self._conn.execute("PRAGMA synchronous = NORMAL")
        return "wal"

    # -- 쓰기 --------------------------------------------------------------
    def save_game(self, log: GameLog) -> None:
        rounds: Sequence[RoundLog] = log.rounds or ()
        game_row = (
            log.game_id,
            log.room_id,
            log.created_at,
            log.ended_at,
            log.engine_version,
            log.ai_version,
            log.mode,
            log.end_reason,
            log.winner,
            int(log.total_sets),
            int(log.p1_score),
            int(log.p2_score),
        )
        round_rows = [
            (
                log.game_id,
                int(r.set_index),
                int(r.round),
                r.first_player,
                r.p1_tile,
                r.p2_tile,
                r.p1_color,
                r.p2_color,
                r.round_winner,
                int(r.p1_score),
                int(r.p2_score),
                r.think_time_ms,
                r.second_think_time_ms,
            )
            for r in rounds
        ]
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    """
                    INSERT INTO games (
                        game_id, room_id, created_at, ended_at, engine_version, ai_version,
                        mode, end_reason, winner, total_sets, p1_score, p2_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        room_id=excluded.room_id,
                        created_at=excluded.created_at,
                        ended_at=excluded.ended_at,
                        engine_version=excluded.engine_version,
                        ai_version=excluded.ai_version,
                        mode=excluded.mode,
                        end_reason=excluded.end_reason,
                        winner=excluded.winner,
                        total_sets=excluded.total_sets,
                        p1_score=excluded.p1_score,
                        p2_score=excluded.p2_score
                    """,
                    game_row,
                )
                cur.execute("DELETE FROM rounds WHERE game_id = ?", (log.game_id,))
                if round_rows:
                    cur.executemany(
                        """
                        INSERT INTO rounds (
                            game_id, set_index, round, first_player, p1_tile, p2_tile,
                            p1_color, p2_color, round_winner, p1_score, p2_score,
                            think_time_ms, second_think_time_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        round_rows,
                    )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

    # -- 읽기 --------------------------------------------------------------
    def get_game(self, game_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
            if row is None:
                return None
            rounds = self._conn.execute(
                """
                SELECT * FROM rounds
                WHERE game_id = ?
                ORDER BY set_index ASC, round ASC
                """,
                (game_id,),
            ).fetchall()
        game = dict(row)
        game["rounds"] = [dict(r) for r in rounds]
        return game

    def list_games(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(0, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT g.*, (SELECT COUNT(*) FROM rounds r WHERE r.game_id = g.game_id) AS round_count
                FROM games g
                ORDER BY g.ended_at DESC, g.game_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_games(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM games").fetchone()
        return int(row[0]) if row else 0

    # -- 정리 --------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
