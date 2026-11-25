"""「흑과 백」 FastAPI 딜러 서버.

서버는 **딜러**다. 타일 배분·제출 접수·승패 판정·점수 계산·선후공 갱신을 모두
``engine.BlackWhiteGame`` 안에서 처리하고, 클라이언트에는 ``game.view_for(player)``
로 만든 **그 플레이어 시점의 뷰만** 보낸다. 상대 타일의 숫자는 진행 중에는
어떤 키로도 내려가지 않으므로, 브라우저를 조작해도 치팅이 불가능하다.

핵심 동작
---------
- 방 생성 ``POST /api/rooms`` : ``secrets`` 기반 6자리 영숫자 방 코드 + 좌석 토큰 발급
- 입장 ``WS /ws?room=CODE&token=...`` : 초대 링크(``/?room=CODE``)로 바로 입장,
  정원(2명)이 찬 방의 제3자는 ``ROOM_FULL`` 로 차단
- 재접속: 좌석별 토큰(브라우저 ``sessionStorage`` 의 ``bw_token_${roomId}``)으로
  같은 자리에 복구. 유예 시간(기본 60초)을 넘기면 ``disconnect`` 부전승 처리
- Heartbeat: 클라이언트 ``ping`` → ``pong`` 즉답, 서버도 18초마다 ``ping`` 을 보내
  Cloudflare Tunnel 같은 프록시의 유휴 연결 종료를 막는다
- ``ROUND_RESULT`` 자동 전환: 2.5초 뒤 자동(또는 양측 ``next`` 확인 시 즉시)
- 기권 ``forfeit`` · 재대결 ``rematch`` · 종료 시 ``storage.save_game(...)`` 자동 저장
- 백그라운드 GC: 활동이 끊긴 좀비 방을 주기적으로 정리

실행::

    cd work/Proj-01_bw1
    .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai as ai_engine
import engine
from ai import AI_VERSION, BaseAI, UnknownDifficultyError, create_ai, normalize_difficulty
from engine import (
    PLAYER_A,
    PLAYER_B,
    PLAYERS,
    BlackWhiteGame,
    DuplicateSubmissionError,
    InvalidTileError,
    NotYourTurnError,
    Phase,
    StaleRequestError,
    TileAlreadyUsedError,
    UnknownPlayerError,
    WrongPhaseError,
    tile_color,
)
from storage import DEFAULT_DB_PATH, GameLog, LogRepository, RoundLog, SQLiteLogRepository

__all__ = [
    "ServerSettings",
    "GameHub",
    "HubError",
    "create_app",
    "app",
    "ROOM_CODE_LENGTH",
    "ROOM_CODE_ALPHABET",
]

logger = logging.getLogger("bw.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# 상수 / 기본 설정
# ---------------------------------------------------------------------------
#: 초대 코드에 쓰는 글자(헷갈리는 0/O/1/l/I 는 뺐다). 방 코드는 이 알파벳의 6자리.
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
#: ``secrets`` 기반 6자리 영숫자 코드(요구 범위 6~8자리).
ROOM_CODE_LENGTH = 6
#: 재접속 유예 시간(초). 넘기면 연결 끊김 부전승.
RECONNECT_GRACE_SECONDS = 60.0
#: ``ROUND_RESULT`` 결과를 보여 주는 시간(초). 지나면 자동으로 다음 라운드.
ROUND_RESULT_SECONDS = 2.5
#: 좀비 방 GC 주기(초).
ROOM_GC_INTERVAL_SECONDS = 30.0
#: 게임이 시작되지 않은 방 / 접속자가 모두 끊긴 방의 유휴 TTL(초).
ROOM_IDLE_TTL_SECONDS = 30 * 60.0
#: 게임이 끝난 방을 결과 화면용으로 남겨 두는 시간(초).
ENDED_ROOM_TTL_SECONDS = 10 * 60.0
#: 서버 → 클라이언트 heartbeat 주기(초). (요구 15~20초)
HEARTBEAT_INTERVAL_SECONDS = 18.0
#: 이 시간 동안 클라이언트 메시지가 없으면 연결이 죽은 것으로 보고 닫는다.
HEARTBEAT_TIMEOUT_SECONDS = 60.0
#: 한 메시지 최대 길이(바이트). 초과분은 읽지 않고 버린다.
MAX_MESSAGE_BYTES = 4096

#: 난이도별 기본 "생각하는 시간"(초). 너무 즉각적인 응답을 피하고, 난이도가 높을수록
#: 조금 더 기다리게 한다. ``ServerSettings.ai_think_delay`` 로 한 번에 덮어쓸 수 있다.
AI_THINK_DELAYS: Dict[str, float] = {"easy": 0.6, "normal": 0.8, "hard": 1.0}
#: AI 좌석은 사람이 앉을 수 없는 자리다(토큰 없음).
AI_TOKEN = ""


@dataclass
class ServerSettings:
    """서버 설정(테스트에서 시간 상수를 줄여 쓸 수 있게 파라미터화)."""

    db_path: Optional[str | Path] = None
    repository: Optional[LogRepository] = None
    reconnect_grace: float = RECONNECT_GRACE_SECONDS
    round_result_seconds: float = ROUND_RESULT_SECONDS
    gc_interval: float = ROOM_GC_INTERVAL_SECONDS
    room_idle_ttl: float = ROOM_IDLE_TTL_SECONDS
    ended_room_ttl: float = ENDED_ROOM_TTL_SECONDS
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS
    heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS
    enable_gc: bool = True
    enable_heartbeat: bool = True
    #: AI 가 수를 고르기 전 최소 대기(초). ``None`` 이면 난이도별 기본값(0.6~1.0초).
    ai_think_delay: Optional[float] = None
    #: 테스트/데모용: 첫 세트 선공을 고정한다(``None`` 이면 코인 토스).
    opening_player: Optional[str] = None


# ---------------------------------------------------------------------------
# 예외 / 좌석 / 방
# ---------------------------------------------------------------------------
class HubError(Exception):
    """WebSocket 입장 단계에서 클라이언트에 코드로 알려 줄 오류."""

    def __init__(self, code: str, message: str, close_code: int = 4400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.close_code = close_code


@dataclass
class Seat:
    """플레이어 좌석(플레이어 A / B). 토큰으로 같은 자리에 다시 붙는다."""

    player: str
    token: str
    connected: bool = False
    websocket: Optional[WebSocket] = None
    disconnected_at: Optional[float] = None
    last_seen: float = 0.0
    #: AI 가 앉은 좌석인가(사람이 붙을 수 없고, 유예·하트비트 대상이 아니다).
    is_ai: bool = False


@dataclass
class Room:
    """방 하나. 좌석 2개(사람 대전) + 게임 1판."""

    room_id: str
    mode: str = "human"  # "human" | "ai"
    difficulty: Optional[str] = None  # AI 대전 난이도(easy/normal/hard)
    ai_player: Optional[str] = None  # AI 가 앉은 좌석(AI 모드에서만)
    ai: Optional[BaseAI] = None
    ai_version: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    seats: Dict[str, Seat] = field(default_factory=dict)
    game: Optional[BlackWhiteGame] = None
    game_id: Optional[str] = None
    game_started_at: Optional[float] = None
    ended: bool = False
    end_reason: Optional[str] = None
    winner: Optional[str] = None
    loser: Optional[str] = None
    log_saved: bool = False

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    round_timer: Optional[asyncio.Task] = None
    grace_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    tasks: Set[asyncio.Task] = field(default_factory=set)
    next_votes: Set[str] = field(default_factory=set)
    rematch_votes: Set[str] = field(default_factory=set)
    think_started: Dict[Tuple[int, int, str], float] = field(default_factory=dict)
    think_times: Dict[Tuple[int, int, str], int] = field(default_factory=dict)
    round_result_started_at: Optional[float] = None
    #: AI 차례 스케줄 중복 방지 키(game_id, set, round, phase).
    ai_scheduled: Set[Tuple[Any, ...]] = field(default_factory=set)
    ai_task: Optional[asyncio.Task] = None

    # -- 조회 --------------------------------------------------------------
    def other(self, player: str) -> Optional[str]:
        return PLAYER_B if player == PLAYER_A else PLAYER_A

    def connected_players(self) -> List[str]:
        return [p for p, s in self.seats.items() if s.connected]

    def human_players(self) -> List[str]:
        """사람이 앉은 좌석(입장·투표·유예 판단용)."""

        return [p for p, s in self.seats.items() if not s.is_ai]

    @property
    def is_ai_mode(self) -> bool:
        return self.mode == "ai"

    @property
    def status(self) -> str:
        if self.ended:
            return "ended"
        if self.game is None:
            return "waiting"
        return "playing"


# ---------------------------------------------------------------------------
# 허브(방·게임·소켓·로그 관리)
# ---------------------------------------------------------------------------
class GameHub:
    """방 저장소 + 딜러 동작. FastAPI 라우트는 이 객체만 호출한다."""

    def __init__(self, settings: Optional[ServerSettings] = None) -> None:
        self.settings = settings or ServerSettings()
        if self.settings.repository is not None:
            self.storage: LogRepository = self.settings.repository
        else:
            db_path = self.settings.db_path or DEFAULT_DB_PATH
            self.storage = SQLiteLogRepository(db_path)
        self.rooms: Dict[str, Room] = {}
        self._gc_task: Optional[asyncio.Task] = None

    # -- 수명주기 ----------------------------------------------------------
    async def start(self) -> None:
        if self.settings.enable_gc and self._gc_task is None:
            self._gc_task = asyncio.create_task(self._gc_loop(), name="bw-gc")

    async def shutdown(self) -> None:
        if self._gc_task is not None:
            self._gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gc_task
            self._gc_task = None
        for room in list(self.rooms.values()):
            await self.dispose_room(room)
        self.rooms.clear()
        with contextlib.suppress(Exception):
            self.storage.close()

    # -- 방 생성 -----------------------------------------------------------
    def new_room_code(self) -> str:
        for _ in range(200):
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
            if code not in self.rooms:
                return code
        raise RuntimeError("방 코드를 만들 수 없습니다.")  # pragma: no cover

    def create_room(self, mode: str = "human", difficulty: Optional[str] = None) -> Room:
        """방을 만들다. ``mode="ai"`` 면 좌석 B 를 AI 플레이어가 차지한다."""

        normalized_mode = (mode or "human").strip().lower()
        if normalized_mode not in ("human", "ai"):
            raise HubError("INVALID_MODE", "모드는 human 또는 ai 입니다.")
        resolved_difficulty: Optional[str] = None
        if normalized_mode == "ai":
            try:
                resolved_difficulty = normalize_difficulty(difficulty)
            except UnknownDifficultyError as exc:
                raise HubError("INVALID_DIFFICULTY", str(exc)) from exc

        room = Room(
            room_id=self.new_room_code(),
            mode=normalized_mode,
            difficulty=resolved_difficulty,
        )
        # 방장은 좌석 A 를 미리 받는다(HTTP 응답으로 토큰 전달).
        room.seats[PLAYER_A] = Seat(player=PLAYER_A, token=self._new_token())
        if normalized_mode == "ai":
            room.ai_player = PLAYER_B
            room.ai = create_ai(resolved_difficulty, seed=secrets.randbits(32))
            room.ai_version = AI_VERSION
            room.seats[PLAYER_B] = Seat(
                player=PLAYER_B, token=AI_TOKEN, connected=True, is_ai=True
            )
        self.rooms[room.room_id] = room
        logger.info(
            "room created %s mode=%s difficulty=%s",
            room.room_id,
            normalized_mode,
            resolved_difficulty,
        )
        return room

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(24)

    def get_room(self, room_id: str) -> Room:
        room = self.rooms.get(room_id or "")
        if room is None:
            raise HubError(
                "ROOM_NOT_FOUND",
                "방을 찾을 수 없습니다. 초대 링크가 만료되었거나 코드가 잘못되었습니다.",
                4404,
            )
        return room

    # -- 입장 --------------------------------------------------------------
    def attach(self, room_id: str, token: Optional[str]) -> Tuple[Room, Seat]:
        """토큰으로 기존 좌석에 붙거나, 빈 좌석을 새로 배정한다."""

        room = self.get_room(room_id)
        if token:
            for seat in room.seats.values():
                if _token_matches(seat.token, token):
                    self.cancel_grace(room, seat.player)
                    return room, seat
            raise HubError(
                "INVALID_TOKEN",
                "이 방의 플레이어 토큰이 아닙니다. 초대 링크로 다시 입장해 주세요.",
                4403,
            )
        if room.ended:
            raise HubError(
                "ROOM_CLOSED",
                "이미 끝난 방입니다. 방을 새로 만들어 다시 초대해 주세요.",
                4408,
            )
        for player in PLAYERS:
            if player not in room.seats:
                seat = Seat(player=player, token=self._new_token())
                room.seats[player] = seat
                logger.info("room %s seat %s joined", room.room_id, player)
                return room, seat
        raise HubError(
            "ROOM_FULL",
            "이미 두 명이 대전 중인 방입니다(정원 2명).",
            4409,
        )

    async def detach(self, room: Room, seat: Seat, websocket: WebSocket) -> None:
        """소켓이 끊긴 좌석을 표시하고 유예 타이머를 건다."""

        if seat.websocket is not websocket:
            return
        seat.websocket = None
        seat.connected = False
        seat.disconnected_at = time.monotonic()
        room.last_activity = time.monotonic()
        logger.info("room %s seat %s disconnected", room.room_id, seat.player)
        await self.broadcast(room)
        self.schedule_grace(room, seat)

    # -- 게임 시작 ---------------------------------------------------------
    def maybe_start(self, room: Room) -> bool:
        if room.game is not None or room.ended:
            return False
        if len(room.seats) < 2:
            return False
        if room.is_ai_mode:
            # AI 는 이미 앉아 있다 → 사람 좌석이 붙는 순간 시작한다.
            if not all(
                seat.connected for player, seat in room.seats.items() if not seat.is_ai
            ):
                return False
            if room.ai is None:
                return False
        elif not all(seat.connected for seat in room.seats.values()):
            return False
        self.start_game(room)
        return True

    def start_game(self, room: Room) -> None:
        room.game = BlackWhiteGame(first_player=self.settings.opening_player)
        room.game_id = uuid.uuid4().hex
        room.game_started_at = time.time()
        room.ended = False
        room.end_reason = None
        room.winner = None
        room.loser = None
        room.log_saved = False
        room.next_votes.clear()
        room.rematch_votes.clear()
        room.think_started.clear()
        room.think_times.clear()
        room.round_result_started_at = None
        room.ai_scheduled.clear()
        room.last_activity = time.monotonic()
        self.note_turn_start(room)
        logger.info(
            "game started room=%s game=%s mode=%s first=%s",
            room.room_id,
            room.game_id,
            room.mode,
            room.game.first_player,
        )

    # -- 턴/고민 시간 ------------------------------------------------------
    def note_turn_start(self, room: Room) -> None:
        game = room.game
        if game is None:
            return
        turn = game.turn
        if turn is None:
            return
        key = (game.set_index, game.round, turn)
        room.think_started.setdefault(key, time.monotonic())

    def _record_think_time(self, room: Room, key: Tuple[int, int, str]) -> None:
        started = room.think_started.get(key)
        if started is None:
            return
        room.think_times[key] = max(0, int((time.monotonic() - started) * 1000))

    # -- AI 차례 ------------------------------------------------------------
    def ai_think_delay(self, room: Room) -> float:
        """AI 가 수를 고르기 전 기다리는 시간(초). 설정이 우선한다."""

        if self.settings.ai_think_delay is not None:
            return max(0.0, float(self.settings.ai_think_delay))
        return AI_THINK_DELAYS.get(room.difficulty or "normal", 0.8)

    @staticmethod
    def ai_turn_key(room: Room) -> Optional[Tuple[Any, ...]]:
        """지금 AI 가 내야 하는 국면의 고유 키(중복 예약 방지)."""

        game = room.game
        if game is None or room.ai_player is None:
            return None
        if game.phase not in (Phase.WAITING_FIRST, Phase.WAITING_SECOND):
            return None
        if game.turn != room.ai_player:
            return None
        return (room.game_id, game.set_index, game.round, game.phase.value)

    def is_ai_thinking(self, room: Room) -> bool:
        """지금 AI 가 수를 고르는 중인가(클라이언트 '생각 중' 표시용)."""

        if not room.is_ai_mode or room.ended:
            return False
        return self.ai_turn_key(room) is not None

    def schedule_ai_turn(self, room: Room) -> None:
        """AI 차례면 백그라운드 테스크를 예약한다(같은 국면은 한 번만)."""

        if not room.is_ai_mode or room.ai is None or room.ended:
            return
        key = self.ai_turn_key(room)
        if key is None or key in room.ai_scheduled:
            return
        room.ai_scheduled.add(key)
        task = asyncio.create_task(self._ai_turn(room, key), name=f"bw-ai-{room.room_id}")
        room.ai_task = task
        room.tasks.add(task)
        task.add_done_callback(room.tasks.discard)

    async def _ai_turn(self, room: Room, key: Tuple[Any, ...]) -> None:
        """최소 대기 후 AI 수를 계산해 제출한다.

        계산(inference)은 ``asyncio.to_thread`` 로 돌려 이벤트 루프를 막지 않는다.
        """

        try:
            await asyncio.sleep(self.ai_think_delay(room))
        except asyncio.CancelledError:  # pragma: no cover - 정리 경로
            return

        submitted = False
        try:
            async with room.lock:
                game = room.game
                if game is None or room.ended or room.ai_player is None:
                    room.ai_scheduled.discard(key)
                    return
                current = (room.game_id, game.set_index, game.round, game.phase.value)
                if current != key or game.turn != room.ai_player:
                    return
                view = game.view_for(room.ai_player)
                tile = await asyncio.to_thread(self._ai_pick, room, view)
                legal = game.legal_actions(room.ai_player)
                if not legal:
                    room.ai_scheduled.discard(key)
                    return
                if tile is None or tile not in legal:
                    logger.warning(
                        "ai picked illegal tile %r (legal=%s) room=%s", tile, legal, room.room_id
                    )
                    tile = legal[0]
                think_key = (game.set_index, game.round, room.ai_player)
                try:
                    info = game.submit_tile(
                        room.ai_player,
                        tile,
                        set_index=game.set_index,
                        round_number=game.round,
                    )
                except Exception:  # pragma: no cover - 방어적: 규칙 위반이면 상태만 다시 보낸다
                    room.ai_scheduled.discard(key)
                    logger.exception("ai submit failed room=%s tile=%s", room.room_id, tile)
                    return
                self._record_think_time(room, think_key)
                room.next_votes.clear()
                room.last_activity = time.monotonic()
                submitted = True
                if info.get("round_completed"):
                    self.schedule_round_advance(room)
                else:
                    self.note_turn_start(room)
        except asyncio.CancelledError:  # pragma: no cover - 방 정리 경로
            raise
        except Exception:  # pragma: no cover - 추론 실패가 서버를 멈추지 않게
            room.ai_scheduled.discard(key)
            logger.exception("ai turn failed room=%s", room.room_id)
        finally:
            if submitted:
                await self.broadcast(room)

    def _ai_pick(self, room: Room, view: Dict[str, Any]) -> Optional[int]:
        """워커 스레드에서 돌아가는 AI 추론(관측 = view_for 뿐)."""

        bot = room.ai
        if bot is None:  # pragma: no cover - 방어적
            return None
        try:
            return bot.select_action(view)
        except ai_engine.AIError:
            logger.warning("ai could not choose in room=%s", room.room_id, exc_info=True)
        except Exception:  # pragma: no cover - 추론 실패가 게임을 멈추지 않게
            logger.exception("ai inference failed room=%s", room.room_id)
        legal = ai_engine.legal_actions_from_view(view)
        return legal[0] if legal else None

    # -- 상태 메시지 -------------------------------------------------------
    def state_payload(self, room: Room, seat: Seat) -> Dict[str, Any]:
        opponent = room.seats.get(room.other(seat.player) or "")
        view = room.game.view_for(seat.player) if room.game is not None else None
        return {
            "type": "state",
            "room": room.room_id,
            "you": seat.player,
            "mode": room.mode,
            "difficulty": room.difficulty,
            "ai_version": room.ai_version,
            "status": room.status,
            "state": view,
            "ended": room.ended,
            "end_reason": room.end_reason,
            "winner": room.winner,
            "loser": room.loser,
            "seats_filled": len(room.seats),
            "opponent_present": opponent is not None,
            "opponent_connected": bool(opponent and opponent.connected),
            "opponent_is_ai": bool(opponent and opponent.is_ai),
            "ai_thinking": self.is_ai_thinking(room),
            "rematch_votes": sorted(room.rematch_votes),
            "next_votes": sorted(room.next_votes),
            "round_result_seconds": self.settings.round_result_seconds,
            "reconnect_grace_seconds": self.settings.reconnect_grace,
        }

    def welcome_payload(self, room: Room, seat: Seat) -> Dict[str, Any]:
        opponent = room.seats.get(room.other(seat.player) or "")
        return {
            "type": "welcome",
            "room": room.room_id,
            "player": seat.player,
            "token": seat.token,
            "mode": room.mode,
            "difficulty": room.difficulty,
            "ai_version": room.ai_version,
            "status": room.status,
            "invite_path": f"/?room={room.room_id}",
            "state": room.game.view_for(seat.player) if room.game is not None else None,
            "ended": room.ended,
            "end_reason": room.end_reason,
            "winner": room.winner,
            "loser": room.loser,
            "seats_filled": len(room.seats),
            "opponent_present": opponent is not None,
            "opponent_connected": bool(opponent and opponent.connected),
            "opponent_is_ai": bool(opponent and opponent.is_ai),
            "ai_thinking": self.is_ai_thinking(room),
            "rematch_votes": sorted(room.rematch_votes),
            "heartbeat_seconds": self.settings.heartbeat_interval,
            "reconnect_grace_seconds": self.settings.reconnect_grace,
            "round_result_seconds": self.settings.round_result_seconds,
        }

    async def send_state(self, room: Room, seat: Seat) -> None:
        if seat.websocket is None:
            return
        await self._send(seat.websocket, self.state_payload(room, seat))

    async def broadcast(self, room: Room, *, exclude: Optional[str] = None) -> None:
        """각 좌석에 **그 좌석 시점의 뷰**를 따로 만들어 보낸다(숫자 누출 방지).

        AI 대전이면 브로드캐스트 직후 AI 차례를 확인해 예약한다(상태 변화의 유일한 통로).
        """

        for seat in list(room.seats.values()):
            if exclude is not None and seat.player == exclude:
                continue
            if seat.connected and seat.websocket is not None:
                await self._send(seat.websocket, self.state_payload(room, seat))
        self.schedule_ai_turn(room)

    async def send_error(
        self, seat: Seat, code: str, message: str, *, websocket: Optional[WebSocket] = None
    ) -> None:
        ws = websocket or seat.websocket
        if ws is None:
            return
        await self._send(ws, {"type": "error", "code": code, "message": message})

    @staticmethod
    async def _send(websocket: WebSocket, payload: Dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except Exception:  # 이미 닫힌 소켓 — 다음 상태 브로드캐스트로 대체된다
            pass

    # -- 메시지 처리 -------------------------------------------------------
    async def handle_message(self, room: Room, seat: Seat, data: Dict[str, Any]) -> None:
        kind = str(data.get("type") or "").lower()
        if kind == "ping":
            await self._send(
                seat.websocket,  # type: ignore[arg-type]
                {"type": "pong", "t": data.get("t"), "server_time": int(time.time() * 1000)},
            )
            return
        if kind == "pong":
            return
        if kind in ("submit", "tile"):
            await self.handle_submit(room, seat, data)
            return
        if kind in ("next", "continue", "ready"):
            await self.handle_next(room, seat)
            return
        if kind == "forfeit":
            await self.handle_forfeit(room, seat)
            return
        if kind == "rematch":
            await self.handle_rematch(room, seat)
            return
        if kind in ("hello", "sync", "state"):
            await self.send_state(room, seat)
            # AI 차례에 예약이 유실된 경우(추론 실패 등) 사람의 sync 로 다시 살린다.
            self.schedule_ai_turn(room)
            return
        if kind == "leave":
            await self.handle_leave(room, seat)
            return
        await self.send_error(seat, "UNKNOWN_TYPE", f"알 수 없는 메시지 형식입니다: {kind!r}")

    async def handle_submit(self, room: Room, seat: Seat, data: Dict[str, Any]) -> None:
        game = room.game
        if game is None:
            await self.send_error(seat, "NO_GAME", "아직 게임이 시작되지 않았습니다.")
            return
        if room.ended:
            await self.send_error(seat, "GAME_OVER", "이미 끝난 게임입니다.")
            return

        raw_tile = data.get("tile")
        try:
            if isinstance(raw_tile, bool):
                raise ValueError
            tile = int(raw_tile)
        except (TypeError, ValueError):
            await self.send_error(seat, "INVALID_TILE", "타일은 0~8 사이의 정수여야 합니다.")
            return

        set_raw = data.get("set", data.get("set_index"))
        round_raw = data.get("round", data.get("round_number"))
        set_index = _as_int(set_raw)
        round_number = _as_int(round_raw)
        if (set_raw is not None and set_index is None) or (
            round_raw is not None and round_number is None
        ):
            await self.send_error(seat, "BAD_REQUEST", "set/round 값이 올바르지 않습니다.")
            return

        think_key = (game.set_index, game.round, seat.player)
        error_code: Optional[str] = None
        error_message = ""
        try:
            info = game.submit_tile(
                seat.player, tile, set_index=set_index, round_number=round_number
            )
        except StaleRequestError:
            error_code = "STALE_ROUND"
            error_message = "이미 지난 라운드의 요청입니다. 최신 화면으로 맞춥니다."
        except NotYourTurnError:
            error_code = "NOT_YOUR_TURN"
            error_message = "지금은 내 차례가 아닙니다."
        except DuplicateSubmissionError:
            error_code = "DUPLICATE_SUBMISSION"
            error_message = "이번 라운드에 이미 다른 타일을 냈습니다."
        except TileAlreadyUsedError:
            error_code = "TILE_UNAVAILABLE"
            error_message = "이미 사용했거나 보유하지 않은 타일입니다."
        except WrongPhaseError:
            error_code = "WRONG_PHASE"
            error_message = "지금 단계에서는 타일을 낼 수 없습니다."
        except InvalidTileError:
            error_code = "INVALID_TILE"
            error_message = "타일은 0~8 사이의 정수여야 합니다."
        except UnknownPlayerError:  # pragma: no cover - 좌석 값은 서버가 정한다
            error_code = "UNKNOWN_PLAYER"
            error_message = "알 수 없는 플레이어입니다."

        if error_code is not None:
            await self.send_error(seat, error_code, error_message)
            await self.send_state(room, seat)  # 화면을 실제 상태로 되돌린다
            return

        if info.get("duplicate"):
            # 같은 요청 재전송(모바일 더블 탭·지연 패킷) — 상태를 바꾸지 않는다.
            await self._send(
                seat.websocket,  # type: ignore[arg-type]
                {"type": "ack", "duplicate": True, "tile": tile,
                 "round": info.get("round_number"), "set": info.get("set_index")},
            )
            return

        self._record_think_time(room, think_key)
        room.next_votes.clear()
        room.last_activity = time.monotonic()

        if info.get("round_completed"):
            self.schedule_round_advance(room)
        else:
            self.note_turn_start(room)

        await self.broadcast(room)

    async def handle_next(self, room: Room, seat: Seat) -> None:
        game = room.game
        if game is None or room.ended:
            await self.send_state(room, seat)
            return
        if game.phase is not Phase.ROUND_RESULT:
            # 결과 화면이 아니면 조용히 최신 상태만 다시 보낸다(중복 클릭 방어).
            await self.send_state(room, seat)
            return
        room.next_votes.add(seat.player)
        room.last_activity = time.monotonic()
        waiting_for = {p for p, s in room.seats.items() if s.connected and not s.is_ai} - room.next_votes
        if not waiting_for:
            await self.advance_round(room)
        else:
            await self.broadcast(room)

    async def advance_round(self, room: Room) -> bool:
        async with room.lock:
            game = room.game
            if game is None or room.ended or game.phase is not Phase.ROUND_RESULT:
                return False
            self._cancel_round_timer(room)
            result = game.advance_round()
            room.next_votes.clear()
            room.round_result_started_at = None
            room.last_activity = time.monotonic()
            if result.get("game_over"):
                self.mark_ended(room, result.get("end_reason"), result.get("winner"))
            else:
                self.note_turn_start(room)
        await self.broadcast(room)
        return True

    async def handle_forfeit(self, room: Room, seat: Seat) -> None:
        if room.game is None:
            await self.send_error(seat, "NO_GAME", "아직 게임이 시작되지 않았습니다.")
            return
        if room.ended:
            await self.send_state(room, seat)
            return
        opponent = room.other(seat.player)
        async with room.lock:
            self.mark_ended(room, "forfeit", opponent)
        await self.broadcast(room)

    async def handle_leave(self, room: Room, seat: Seat) -> None:
        """방에서 나가기(대기 중 나가기). 게임 중이면 기권으로 처리한다."""

        if room.game is not None and not room.ended:
            await self.handle_forfeit(room, seat)
        seat.connected = False
        room.seats.pop(seat.player, None)
        room.last_activity = time.monotonic()
        await self.broadcast(room)

    async def handle_rematch(self, room: Room, seat: Seat) -> None:
        if room.game is None:
            await self.send_error(seat, "NO_GAME", "아직 게임이 시작되지 않았습니다.")
            return
        if not room.ended:
            await self.send_error(seat, "NOT_FINISHED", "게임이 끝난 뒤에 재대결을 요청할 수 있습니다.")
            return
        room.rematch_votes.add(seat.player)
        room.last_activity = time.monotonic()
        if room.is_ai_mode:
            # AI 는 투표하지 않는다 → 사람이 누르면 바로 새 판을 시작한다.
            async with room.lock:
                self._cancel_round_timer(room)
                self.start_game(room)
            await self.broadcast(room)
            return
        if len(room.seats) == 2 and room.rematch_votes >= set(PLAYERS):
            async with room.lock:
                self._cancel_round_timer(room)
                self.start_game(room)
        await self.broadcast(room)

    # -- 라운드 결과 자동 전환 --------------------------------------------
    def schedule_round_advance(self, room: Room) -> None:
        self._cancel_round_timer(room)
        game = room.game
        if game is None:
            return
        key = (game.set_index, game.round)
        room.round_result_started_at = time.monotonic()
        room.round_timer = asyncio.create_task(self._round_timer(room, key))
        room.tasks.add(room.round_timer)
        room.round_timer.add_done_callback(room.tasks.discard)

    async def _round_timer(self, room: Room, key: Tuple[int, int]) -> None:
        try:
            await asyncio.sleep(max(0.0, self.settings.round_result_seconds))
        except asyncio.CancelledError:  # pragma: no cover - 정상 취소 경로
            return
        game = room.game
        if game is None or room.ended:
            return
        if game.phase is not Phase.ROUND_RESULT:
            return
        if (game.set_index, game.round) != key:
            return
        await self.advance_round(room)

    def _cancel_round_timer(self, room: Room) -> None:
        task = room.round_timer
        room.round_timer = None
        if task is None or task.done():
            return
        with contextlib.suppress(RuntimeError):
            if task is not asyncio.current_task():
                task.cancel()

    # -- 재접속 유예 -------------------------------------------------------
    def schedule_grace(self, room: Room, seat: Seat) -> None:
        if room.ended or room.game is None:
            return
        self.cancel_grace(room, seat.player)
        task = asyncio.create_task(self._grace_timer(room, seat.player))
        room.grace_tasks[seat.player] = task
        room.tasks.add(task)
        task.add_done_callback(room.tasks.discard)

    def cancel_grace(self, room: Room, player: str) -> None:
        task = room.grace_tasks.pop(player, None)
        if task is None or task.done():
            return
        with contextlib.suppress(RuntimeError):
            # 유예 타이머가 스스로 종료를 확정한 경우(자기 자신) 취소하지 않는다.
            if task is not asyncio.current_task():
                task.cancel()

    async def _grace_timer(self, room: Room, player: str) -> None:
        try:
            await asyncio.sleep(max(0.0, self.settings.reconnect_grace))
        except asyncio.CancelledError:
            return
        seat = room.seats.get(player)
        if seat is None or seat.connected:
            return
        if room.ended or room.game is None:
            return
        opponent = room.other(player)
        logger.info("room %s seat %s reconnect grace expired", room.room_id, player)
        # 자기 자신은 취소 대상에서 빼고(취소하면 아래 브로드캐스트가 사라진다) 종료 처리한다.
        room.grace_tasks.pop(player, None)
        async with room.lock:
            self.mark_ended(room, "disconnect", opponent)
        await self.broadcast(room)

    # -- 종료/저장 ---------------------------------------------------------
    def mark_ended(self, room: Room, reason: Optional[str], winner: Optional[str]) -> None:
        if room.ended:
            return
        room.ended = True
        room.end_reason = reason or "normal"
        room.winner = winner if winner in PLAYERS else None
        room.loser = room.other(room.winner) if room.winner else None
        room.last_activity = time.monotonic()
        self._cancel_round_timer(room)
        for player in list(room.grace_tasks):
            self.cancel_grace(room, player)
        room.rematch_votes.clear()
        logger.info(
            "room %s ended reason=%s winner=%s",
            room.room_id,
            room.end_reason,
            room.winner,
        )
        self.save_log(room)

    def build_log(self, room: Room) -> Optional[GameLog]:
        game = room.game
        if game is None or room.game_id is None:
            return None
        full = game.full_state()
        rounds: List[RoundLog] = []
        for record in full.get("rounds", []):  # type: ignore[union-attr]
            set_index = int(record["set_index"])
            round_number = int(record["round"])
            first = record["first"]
            first_is_a = first == PLAYER_A
            p1_tile = record["first_tile"] if first_is_a else record["second_tile"]
            p2_tile = record["second_tile"] if first_is_a else record["first_tile"]
            second = record["second"]
            rounds.append(
                RoundLog(
                    set_index=set_index,
                    round=round_number,
                    first_player=first,
                    p1_tile=p1_tile,
                    p2_tile=p2_tile,
                    p1_color=tile_color(p1_tile) if p1_tile is not None else None,
                    p2_color=tile_color(p2_tile) if p2_tile is not None else None,
                    round_winner=record["winner"],
                    p1_score=int(record["scores_after"].get(PLAYER_A, 0)),
                    p2_score=int(record["scores_after"].get(PLAYER_B, 0)),
                    think_time_ms=room.think_times.get((set_index, round_number, first)),
                    second_think_time_ms=room.think_times.get((set_index, round_number, second)),
                )
            )

        end_reason = room.end_reason or full.get("end_reason") or "normal"
        winner = room.winner if room.winner in PLAYERS else full.get("winner")
        scores = full.get("scores") or {}
        return GameLog(
            game_id=room.game_id,
            room_id=room.room_id,
            created_at=_iso(room.game_started_at),
            ended_at=_iso(),
            engine_version=str(full.get("engine_version") or engine.ENGINE_VERSION),
            mode=self.log_mode(room),
            end_reason=str(end_reason),
            winner=winner if winner in PLAYERS else None,
            total_sets=int(full.get("set_index", 0)) + 1,
            ai_version=room.ai_version if room.is_ai_mode else None,
            p1_score=int(scores.get(PLAYER_A, 0)),
            p2_score=int(scores.get(PLAYER_B, 0)),
            rounds=rounds,
        )

    def save_log(self, room: Room) -> None:
        if room.log_saved:
            return
        log = self.build_log(room)
        if log is None:
            return
        try:
            self.storage.save_game(log)
            room.log_saved = True
            logger.info("game log saved game=%s rounds=%d", log.game_id, len(log.rounds))
        except Exception:  # 저장 실패가 게임 진행을 막지 않게 한다
            logger.exception("failed to save game log game=%s", room.game_id)

    @staticmethod
    def log_mode(room: Room) -> str:
        """로그에 남길 대전 모드(``human_vs_human`` / ``human_vs_ai_easy`` …)."""

        if not room.is_ai_mode:
            return "human_vs_human"
        return f"human_vs_ai_{room.difficulty or 'normal'}"

    # -- Heartbeat ---------------------------------------------------------
    async def heartbeat_loop(self, room: Room, seat: Seat, websocket: WebSocket) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.01, self.settings.heartbeat_interval))
                if seat.websocket is not websocket or not seat.connected:
                    return
                now = time.monotonic()
                if now - seat.last_seen > self.settings.heartbeat_timeout:
                    logger.info("room %s seat %s heartbeat timeout", room.room_id, seat.player)
                    with contextlib.suppress(Exception):
                        await websocket.close(code=4001)
                    return
                await self._send(websocket, {"type": "ping", "t": int(time.time() * 1000)})
        except asyncio.CancelledError:  # pragma: no cover
            return
        except Exception:  # pragma: no cover - 소켓 종료 등
            return

    # -- GC ---------------------------------------------------------------
    async def _gc_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.01, self.settings.gc_interval))
                await self.sweep()
        except asyncio.CancelledError:  # pragma: no cover
            return

    def should_evict(self, room: Room, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        idle = now - room.last_activity
        if room.ended:
            return idle > self.settings.ended_room_ttl
        # AI 좌석은 항상 '접속됨' 이므로, 사람이 아무도 없을 때만 유휴로 본다.
        if not any(seat.connected for seat in room.seats.values() if not seat.is_ai):
            return idle > self.settings.room_idle_ttl
        return False

    async def sweep(self, now: Optional[float] = None) -> List[str]:
        removed: List[str] = []
        for room_id, room in list(self.rooms.items()):
            if self.should_evict(room, now):
                self.rooms.pop(room_id, None)
                removed.append(room_id)
                await self.dispose_room(room)
        if removed:
            logger.info("gc removed rooms: %s", ", ".join(removed))
        return removed

    async def dispose_room(self, room: Room) -> None:
        self._cancel_round_timer(room)
        for player in list(room.grace_tasks):
            self.cancel_grace(room, player)
        for task in list(room.tasks):
            if not task.done():
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        room.tasks.clear()
        for seat in list(room.seats.values()):
            ws = seat.websocket
            seat.websocket = None
            seat.connected = False
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(code=4002)


def _iso(timestamp: Optional[float] = None) -> str:
    ts = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any) -> Optional[int]:
    """JSON 에서 온 값을 int 로(변환 불가·bool·None 이면 None)."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_matches(expected: str, given: str) -> bool:
    """상수 시간 토큰 비교(비 ASCII 토큰은 단순 불일치로 처리)."""

    try:
        return secrets.compare_digest(expected, given)
    except TypeError:  # pragma: no cover - 비정상 입력
        return False


# ---------------------------------------------------------------------------
# FastAPI 앱
# ---------------------------------------------------------------------------
class CreateRoomRequest(BaseModel):
    mode: str = "human"
    difficulty: Optional[str] = None


def create_app(settings: Optional[ServerSettings] = None) -> FastAPI:
    """서버 앱을 만든다. 테스트는 시간 상수를 줄인 ``ServerSettings`` 를 넘긴다."""

    settings = settings or ServerSettings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        hub = GameHub(settings)
        app.state.hub = hub
        await hub.start()
        try:
            yield
        finally:
            await hub.shutdown()

    app = FastAPI(title="흑과 백 (Black & White)", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def hub() -> GameHub:
        return app.state.hub  # type: ignore[no-any-return]

    # -- 정적 페이지 -------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():  # pragma: no cover - 배포 사고 방지
            raise HTTPException(status_code=404, detail="static/index.html 이 없습니다.")
        return FileResponse(index_path)

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        h = hub()
        return {
            "ok": True,
            "engine_version": engine.ENGINE_VERSION,
            "ai_version": AI_VERSION,
            "difficulties": list(ai_engine.DIFFICULTIES),
            "rooms": len(h.rooms),
            "games_logged": h.storage.count_games(),
        }

    # -- 방 생성 -----------------------------------------------------------
    @app.post("/api/rooms")
    async def create_room(payload: CreateRoomRequest) -> Dict[str, Any]:
        mode = (payload.mode or "human").strip().lower()
        if mode not in ("human", "ai"):
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_MODE", "message": "모드는 human 또는 ai 입니다."},
            )
        h = hub()
        try:
            room = h.create_room(mode, payload.difficulty)
        except HubError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "message": exc.message,
                        "difficulties": list(ai_engine.DIFFICULTIES)},
            ) from exc
        seat = room.seats[PLAYER_A]
        return {
            "room_id": room.room_id,
            "mode": room.mode,
            "difficulty": room.difficulty,
            "ai_version": room.ai_version,
            "ai_think_delay": h.ai_think_delay(room) if room.is_ai_mode else None,
            "log_mode": h.log_mode(room),
            "player": seat.player,
            "token": seat.token,
            "invite_path": f"/?room={room.room_id}",
            "ws_path": f"/ws?room={room.room_id}",
        }

    # -- 방 조회(초대 링크 미리보기/테스트용) -----------------------------
    @app.get("/api/rooms/{room_id}")
    async def room_info(room_id: str) -> Dict[str, Any]:
        h = hub()
        room = h.rooms.get(room_id.strip())
        if room is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "ROOM_NOT_FOUND", "message": "방을 찾을 수 없습니다."},
            )
        return {
            "room_id": room.room_id,
            "exists": True,
            "mode": room.mode,
            "difficulty": room.difficulty,
            "ai_version": room.ai_version,
            "status": room.status,
            "seats_filled": len(room.seats),
            "seats": {
                player: {"connected": seat.connected, "is_ai": seat.is_ai}
                for player, seat in room.seats.items()
            },
            "invite_path": f"/?room={room.room_id}",
        }

    # -- WebSocket 입장 ----------------------------------------------------
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        h = hub()
        room_id = (websocket.query_params.get("room") or "").strip()
        token = (websocket.query_params.get("token") or "").strip() or None
        try:
            room, seat = h.attach(room_id, token)
        except HubError as exc:
            await GameHub._send(
                websocket, {"type": "error", "code": exc.code, "message": exc.message}
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=exc.close_code)
            return

        # 같은 좌석으로 다른 소켓이 붙으면(탭 복제 등) 이전 소켓을 정리한다.
        previous = seat.websocket
        if previous is not None and previous is not websocket:
            with contextlib.suppress(Exception):
                await previous.close(code=4000)

        seat.websocket = websocket
        seat.connected = True
        seat.disconnected_at = None
        seat.last_seen = time.monotonic()
        room.last_activity = time.monotonic()
        h.cancel_grace(room, seat.player)

        await GameHub._send(websocket, h.welcome_payload(room, seat))
        started = h.maybe_start(room)
        # 방금 붙은 좌석은 welcome 에 자기 상태가 실려 있으므로, 게임이 시작된
        # 경우에만 전체에 알리고 아니면 상대에게만 '입장/복귀' 소식을 보낸다.
        if started:
            await h.broadcast(room)
        else:
            await h.broadcast(room, exclude=seat.player)

        heartbeat: Optional[asyncio.Task] = None
        if settings.enable_heartbeat:
            heartbeat = asyncio.create_task(
                h.heartbeat_loop(room, seat, websocket), name=f"bw-hb-{room.room_id}-{seat.player}"
            )
            room.tasks.add(heartbeat)
            heartbeat.add_done_callback(room.tasks.discard)

        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > MAX_MESSAGE_BYTES:
                    await GameHub._send(
                        websocket,
                        {"type": "error", "code": "MESSAGE_TOO_LARGE", "message": "메시지가 너무 큽니다."},
                    )
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    await GameHub._send(
                        websocket,
                        {"type": "error", "code": "BAD_MESSAGE", "message": "JSON 형식이 아닙니다."},
                    )
                    continue
                if not isinstance(data, dict):
                    await GameHub._send(
                        websocket,
                        {"type": "error", "code": "BAD_MESSAGE", "message": "JSON 객체를 보내 주세요."},
                    )
                    continue
                seat.last_seen = time.monotonic()
                room.last_activity = time.monotonic()
                try:
                    await h.handle_message(room, seat, data)
                except Exception:  # 개별 메시지 실패가 연결을 죽이지 않게
                    logger.exception("message handling failed room=%s player=%s", room.room_id, seat.player)
                    await GameHub._send(
                        websocket,
                        {"type": "error", "code": "SERVER_ERROR", "message": "요청을 처리하지 못했습니다."},
                    )
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - 비정상 종료
            logger.debug("websocket closed abnormally", exc_info=True)
        finally:
            if heartbeat is not None and not heartbeat.done():
                heartbeat.cancel()
            await h.detach(room, seat, websocket)

    return app


app = create_app()
