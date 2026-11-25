#!/usr/bin/env python3
"""「흑과 백」 CLI 클라이언트 — 피클/터미널에서 한 수씩 두는 플레이어.

브라우저(`static/game.js`)와 **같은 WebSocket 프로토콜**을 쓰는 얇은 클라이언트다.
한 번의 명령이 한 번의 접속으로 끝나고(세션 파일의 좌석 토큰으로 같은 자리에 다시
붙는다), 사람이 읽을 수 있는 **JSON 한 개**를 stdout 에 출력한다.

서버 계약(`server.py`)

- WebSocket: ``/ws?room=CODE[&token=...]`` (초대 링크는 ``http://host:port/?room=CODE``)
- 입장 직후 ``welcome`` 수신 → 좌석 토큰·현재 뷰
- 제출: ``{"type": "submit", "tile": 0~8, "set": set_index, "round": round}``
- ``ROUND_RESULT`` 는 2.5초 뒤 자동 전환되며, ``{"type": "next"}`` 로 앞당길 수 있다
- ``GAME_OVER`` 가 되면 ``state.view.replay`` 에 전체 라운드 숫자가 공개된다

사용 예::

    # 방장이 만든 초대 링크로 입장(사람 대전, 두 번째 좌석)
    python cli.py join 'http://192.168.0.12:8000/?room=AbC123' --timeout 50

    # 내 차례가 되면 타일 하나를 제출하고, 다시 내 차례(또는 종료)까지 대기
    python cli.py submit 7

    # 타임아웃으로 끊겼으면 이어서 대기
    python cli.py wait

    # 한 번만 조회 / 기권 / AI 방(방장 토큰으로 입장)
    python cli.py status
    python cli.py forfeit
    python cli.py join 'http://127.0.0.1:8000/?room=XyZ789' --token '<방장 토큰>'

종료 코드

- ``0``  내 차례(``my_turn``) / 게임 종료(``game_over``) — `status` 는
  ``opponent_turn`` · ``waiting`` 도 0
- ``1``  오류(``error`` — 연결 실패·잘못된 URL·없는 세션·불법 타일 등)
- ``3``  대기 시간 초과(``timeout``) — 세션은 그대로 남으므로 ``wait`` 로 이어간다

세션 파일(기본 ``.bw_session.json``, ``--session-file`` 로 변경)에는
``ws_url`` · ``room_id`` · ``token`` · ``player`` · ``set_index`` · ``round`` 가 남는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

__all__ = [
    "DEFAULT_SESSION_FILE",
    "DEFAULT_TIMEOUT",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_TIMEOUT",
    "CliError",
    "Invite",
    "parse_invite",
    "parse_invite_url",
    "load_session",
    "save_session",
    "build_output",
    "run",
    "main",
]

DEFAULT_SESSION_FILE = ".bw_session.json"
DEFAULT_TIMEOUT = 50.0
DEFAULT_WS_PATH = "/ws"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 3

#: 서버가 받아 주는 타일 범위.
TILES = range(9)
#: ``ROUND_RESULT`` 에서 다음 라운드로 넘기는 신호(양쪽이 보내면 즉시, 아니면 서버 타이머).
NEXT_MESSAGE = {"type": "next"}


# ---------------------------------------------------------------------------
# 예외 / 작은 도구
# ---------------------------------------------------------------------------
class CliError(Exception):
    """CLI 가 사용자에게 코드로 알려 줄 오류(JSON ``status: "error"``)."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def _monotonic() -> float:
    return time.monotonic()


def _remaining(deadline: float) -> float:
    return deadline - _monotonic()


# ---------------------------------------------------------------------------
# 초대 링크 → WebSocket URL
# ---------------------------------------------------------------------------
@dataclass
class Invite:
    """초대 링크를 해석한 결과."""

    ws_url: str
    room_id: str
    token: Optional[str] = None
    base_url: Optional[str] = None


def parse_invite(url: str) -> Invite:
    """``http(s)://host[:port]/?room=CODE`` 를 WebSocket URL 로 바꾼다.

    - ``http`` → ``ws``, ``https``/``wss`` → ``wss``
    - 경로가 루트가 아니면(리버스 프록시 등) 그 접두사를 유지하고 ``/ws`` 를 붙인다
    - 쿼리의 ``token`` 은 좌석 토큰으로 함께 읽는다(방장 자신의 링크 등)

    Raises
    ------
    CliError
        ``INVALID_URL`` — 스킴·호스트·``room`` 쿼리가 없거나 포트가 잘못된 경우
    """

    raw = (url or "").strip()
    if not raw:
        raise CliError("INVALID_URL", "초대 URL이 비어 있습니다.")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https", "ws", "wss"):
        raise CliError("INVALID_URL", f"http(s) 또는 ws(s) URL이어야 합니다: {url!r}")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise CliError("INVALID_URL", f"URL의 호스트/포트를 읽을 수 없습니다: {url!r}") from exc
    if not host:
        raise CliError("INVALID_URL", f"URL에 호스트가 없습니다: {url!r}")

    query = parse_qs(parts.query, keep_blank_values=True)
    room_id = (query.get("room") or [""])[0].strip()
    if not room_id:
        raise CliError(
            "INVALID_URL",
            f"URL에 room 쿼리(초대 코드)가 없습니다: {url!r} — 예: http://host:8000/?room=CODE",
        )
    token = (query.get("token") or [""])[0].strip() or None

    ws_scheme = "wss" if scheme in ("https", "wss") else "ws"
    host_part = f"[{host}]" if ":" in host else host
    netloc = f"{host_part}:{port}" if port else host_part

    prefix = parts.path.rstrip("/")
    if prefix.endswith(DEFAULT_WS_PATH):
        ws_path = prefix
    elif prefix:
        ws_path = prefix + DEFAULT_WS_PATH
    else:
        ws_path = DEFAULT_WS_PATH

    base_scheme = "https" if ws_scheme == "wss" else "http"
    ws_url = urlunsplit((ws_scheme, netloc, ws_path, urlencode({"room": room_id}), ""))
    base_url = urlunsplit((base_scheme, netloc, prefix or "/", urlencode({"room": room_id}), ""))
    return Invite(ws_url=ws_url, room_id=room_id, token=token, base_url=base_url)


def parse_invite_url(url: str) -> Tuple[str, str]:
    """``(ws_url, room_id)`` 만 필요한 곳을 위한 얇은 래퍼."""

    invite = parse_invite(url)
    return invite.ws_url, invite.room_id


# ---------------------------------------------------------------------------
# 세션 파일
# ---------------------------------------------------------------------------
SESSION_KEYS = ("ws_url", "room_id", "token", "player", "set_index", "round")


def load_session(path: str | Path) -> Dict[str, Any]:
    """세션 파일을 읽는다. 없거나 형식이 틀리면 ``NO_SESSION``/``BAD_SESSION``."""

    file_path = Path(path)
    if not file_path.is_file():
        raise CliError(
            "NO_SESSION",
            f"세션 파일이 없습니다: {file_path} — 먼저 `cli.py join <초대 링크>` 로 입장하세요.",
        )
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError("BAD_SESSION", f"세션 파일을 읽을 수 없습니다: {file_path}") from exc
    if not isinstance(data, dict) or not data.get("ws_url") or not data.get("room_id"):
        raise CliError("BAD_SESSION", f"세션 파일에 ws_url/room_id 가 없습니다: {file_path}")
    if not data.get("token"):
        raise CliError(
            "BAD_SESSION",
            f"세션 파일에 좌석 토큰이 없습니다: {file_path} — `cli.py join` 을 다시 실행하세요.",
        )
    return data


def save_session(path: str | Path, data: Dict[str, Any]) -> None:
    """세션 파일을 원자적으로 쓴다(중간에 깨진 JSON 이 남지 않게)."""

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: data.get(key) for key in SESSION_KEYS}
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    fd, tmp_name = tempfile.mkstemp(dir=str(file_path.parent), prefix=file_path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, file_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# 서버 상태 추적
# ---------------------------------------------------------------------------
@dataclass
class Tracker:
    """``welcome``/``state`` 메시지를 모아 "내 시점" 상태를 유지한다."""

    player: Optional[str] = None
    room_id: Optional[str] = None
    token: Optional[str] = None
    view: Optional[Dict[str, Any]] = None
    payload: Optional[Dict[str, Any]] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)
    illegal_tile: Optional[int] = None

    def feed(self, message: Dict[str, Any]) -> bool:
        """상태 메시지를 반영한다. 상태 메시지였으면 True."""

        kind = message.get("type")
        if kind not in ("welcome", "state"):
            return False
        self.payload = message
        if kind == "welcome":
            self.token = message.get("token") or self.token
            self.player = message.get("player") or self.player
        else:
            self.player = message.get("you") or self.player
        self.room_id = message.get("room") or self.room_id
        view = message.get("state")
        if isinstance(view, dict):
            self.view = view
            self.player = view.get("viewer") or self.player
        return True

    # -- 파생 값 ------------------------------------------------------------
    @property
    def phase(self) -> Optional[str]:
        return (self.view or {}).get("phase")

    @property
    def turn(self) -> Optional[str]:
        return (self.view or {}).get("turn")

    @property
    def ended(self) -> bool:
        payload = self.payload or {}
        if payload.get("ended"):
            return True
        return self.phase == "GAME_OVER"

    @property
    def opponent(self) -> Optional[str]:
        if self.player == "A":
            return "B"
        if self.player == "B":
            return "A"
        return None

    def my_turn(self) -> bool:
        return self.phase in ("WAITING_FIRST", "WAITING_SECOND") and self.turn == self.player

    def status(self, *, blocking: bool = True) -> Optional[str]:
        """``game_over`` / ``my_turn`` 또는 (blocking 아니면) ``opponent_turn``·``waiting``."""

        if self.ended:
            return "game_over"
        if self.view is None:
            return None if blocking else "waiting"
        if self.my_turn():
            return "my_turn"
        return None if blocking else "opponent_turn"

    def my_tiles(self) -> List[int]:
        you = (self.view or {}).get("you") or {}
        remaining = you.get("remaining")
        return list(remaining) if isinstance(remaining, list) else []

    def session_fields(self) -> Dict[str, Any]:
        view = self.view or {}
        return {
            "room_id": self.room_id,
            "token": self.token,
            "player": self.player,
            "set_index": view.get("set_index"),
            "round": view.get("round"),
        }


# ---------------------------------------------------------------------------
# 출력 JSON
# ---------------------------------------------------------------------------
def _last_round_result(view: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """가장 최근 라운드의 공개 결과(``ROUND_RESULT`` 면 진행 중 결과, 아니면 직전 라운드)."""

    result = view.get("round_result")
    if isinstance(result, dict):
        return result
    rounds = view.get("rounds")
    if isinstance(rounds, list) and rounds:
        last = rounds[-1]
        if isinstance(last, dict):
            return last
    return None


def build_output(
    status: str,
    tracker: Tracker,
    *,
    message: Optional[str] = None,
    code: Optional[str] = None,
    waiting_for: Optional[str] = None,
) -> Dict[str, Any]:
    """stdout 에 낼 JSON 한 개를 만든다(비밀 정보 없음 — 서버 공개 뷰만)."""

    view = tracker.view or {}
    payload = tracker.payload or {}
    me = tracker.player
    opponent = tracker.opponent
    scores = view.get("scores") or {}
    opp = view.get("opponent") or {}
    game_over = view.get("game_over") or {}

    output: Dict[str, Any] = {
        "status": status,
        "room_id": tracker.room_id or payload.get("room"),
        "player": me,
        "set_index": view.get("set_index"),
        "round": view.get("round"),
        "phase": view.get("phase"),
        "turn": view.get("turn"),
        "my_tiles": tracker.my_tiles(),
        "my_score": scores.get(me) if me else None,
        "opp_score": scores.get(opponent) if opponent else None,
        "opp_remaining_colors": {
            "black": opp.get("remaining_black"),
            "white": opp.get("remaining_white"),
        },
        "opp_submitted_color": opp.get("submitted_color"),
        "last_round_result": _last_round_result(view),
        "winner": payload.get("winner") or game_over.get("winner"),
        "end_reason": payload.get("end_reason") or game_over.get("end_reason"),
    }

    # 대전 상대 정보(사람/AI·연결 여부·AI 생각 중) — 판단에 도움이 되는 부가 정보.
    if payload:
        output["opponent_is_ai"] = payload.get("opponent_is_ai")
        output["opponent_connected"] = payload.get("opponent_connected")
        output["ai_thinking"] = payload.get("ai_thinking")

    if status == "game_over":
        output["history"] = view.get("replay") or []
        output["final_scores"] = game_over.get("scores") or scores
        output["final_hands"] = game_over.get("final_hands")
        output["sets"] = game_over.get("sets") or []
        output["total_sets"] = game_over.get("total_sets")
    if status == "timeout":
        output["waiting_for"] = waiting_for or "my_turn"
        output["message"] = message or "제한 시간 안에 내 차례가 오지 않았습니다."
    if status == "error":
        output["code"] = code or "ERROR"
        output["message"] = message or "요청을 처리하지 못했습니다."
    if tracker.illegal_tile is not None:
        output["illegal_tile"] = tracker.illegal_tile
    return output


# ---------------------------------------------------------------------------
# 연결·메시지 루프
# ---------------------------------------------------------------------------
def _with_token(ws_url: str, token: Optional[str]) -> str:
    if not token:
        return ws_url
    parts = urlsplit(ws_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["token"] = [token]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _connect(ws_url: str, token: Optional[str], timeout: float) -> Any:
    open_timeout = max(1.0, min(15.0, timeout if timeout > 0 else 10.0))
    return connect(
        _with_token(ws_url, token),
        open_timeout=open_timeout,
        ping_interval=None,
        close_timeout=2,
        max_size=1 << 20,
    )


async def _send(ws: Any, payload: Dict[str, Any]) -> None:
    try:
        await ws.send(json.dumps(payload))
    except ConnectionClosed as exc:  # pragma: no cover - 서버가 먼저 닫은 경우
        raise CliError("CONNECTION_CLOSED", "서버와의 연결이 끊겼습니다.") from exc


def _decode(raw: str | bytes) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _handshake(ws: Any, tracker: Tracker, deadline: float) -> None:
    """입장 직후 ``welcome`` 을 받는다(없으면 오류로 처리)."""

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, _remaining(deadline)))
    except asyncio.TimeoutError as exc:
        raise CliError("TIMEOUT", "서버가 입장 응답(welcome)을 보내지 않았습니다.") from exc
    message = _decode(raw)
    if message is None:
        raise CliError("BAD_MESSAGE", "서버 응답을 JSON 으로 읽을 수 없습니다.")
    if message.get("type") == "error":
        raise CliError(
            str(message.get("code") or "SERVER_ERROR"),
            str(message.get("message") or "입장에 실패했습니다."),
        )
    if not tracker.feed(message):
        raise CliError("BAD_MESSAGE", f"예상치 못한 첫 메시지입니다: {message.get('type')!r}")


async def _loop(
    ws: Any,
    tracker: Tracker,
    deadline: float,
    *,
    tile: Optional[int] = None,
    forfeit: bool = False,
    blocking: bool = True,
) -> str:
    """내 차례(또는 ``GAME_OVER``)까지 메시지를 소화하며 기다린다.

    반환값: ``my_turn`` | ``game_over`` | ``timeout`` | ``error`` | ``closed``
    """

    submitted = False
    submitted_key: Optional[Tuple[Any, Any]] = None
    forfeited = False
    next_sent: Set[Tuple[Any, Any]] = set()

    while True:
        status = tracker.status(blocking=blocking)
        if status == "game_over":
            return "game_over"
        if status in ("opponent_turn", "waiting"):
            return status  # non-blocking(status 명령)에서만 나온다

        view = tracker.view or {}
        current_key = (view.get("set_index"), view.get("round"))

        if forfeit and not forfeited:
            await _send(ws, {"type": "forfeit"})
            forfeited = True
        elif tile is not None and not submitted and status == "my_turn":
            remaining = tracker.my_tiles()
            if tile not in remaining:
                tracker.illegal_tile = tile
                tracker.errors.append(
                    {
                        "type": "error",
                        "code": "ILLEGAL_TILE",
                        "message": f"보유하지 않은 타일입니다: {tile} (남은 타일 {remaining})",
                    }
                )
                return "error"
            await _send(
                ws,
                {
                    "type": "submit",
                    "tile": tile,
                    "set": view.get("set_index"),
                    "round": view.get("round"),
                },
            )
            submitted = True
            submitted_key = current_key
        elif tile is None and not forfeit and status == "my_turn":
            return "my_turn"
        elif tile is not None and submitted and status == "my_turn" and current_key != submitted_key:
            # 제출한 라운드를 지나 다음 내 차례(또는 연장전)가 왔다.
            return "my_turn"

        view = tracker.view or {}
        if view.get("phase") == "ROUND_RESULT":
            # 결과 화면은 서버가 2.5초 뒤 자동 전환한다. 양쪽 확인이면 즉시 넘어가게
            # `next` 를 보내 두되, 같은 라운드에는 한 번만 보낸다.
            key = (view.get("set_index"), view.get("round"))
            if key not in next_sent:
                next_sent.add(key)
                await _send(ws, dict(NEXT_MESSAGE))

        remaining_time = _remaining(deadline)
        if remaining_time <= 0:
            return "timeout"
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining_time)
        except asyncio.TimeoutError:
            return "timeout"
        except ConnectionClosed:
            return "closed"

        message = _decode(raw)
        if message is None:
            continue

        kind = message.get("type")
        if kind == "ping":
            await _send(ws, {"type": "pong", "t": message.get("t")})
            continue
        if kind == "pong":
            continue
        if kind == "error":
            tracker.errors.append(message)
            code = str(message.get("code") or "")
            if code in ("STALE_ROUND", "NOT_YOUR_TURN", "WRONG_PHASE", "DUPLICATE_SUBMISSION"):
                # 서버가 곧 최신 state 를 보낸다. 그 state 기준으로 다시 판단한다.
                submitted = False
                submitted_key = None
                continue
            return "error"
        tracker.feed(message)


def _resolve_result(outcome: str, tracker: Tracker) -> Tuple[Dict[str, Any], int]:
    """루프 결과를 (출력 JSON, 종료 코드)로 바꾼다."""

    if outcome == "my_turn":
        return build_output("my_turn", tracker), EXIT_OK
    if outcome == "game_over":
        return build_output("game_over", tracker), EXIT_OK
    if outcome == "opponent_turn":
        return build_output("opponent_turn", tracker), EXIT_OK
    if outcome == "waiting":
        return build_output("waiting", tracker), EXIT_OK
    if outcome == "timeout":
        waiting_for = "game_start" if tracker.view is None else "my_turn"
        return build_output("timeout", tracker, waiting_for=waiting_for), EXIT_TIMEOUT
    if outcome == "closed":
        last_error = tracker.errors[-1] if tracker.errors else None
        if last_error is not None:
            return (
                build_output(
                    "error",
                    tracker,
                    code=str(last_error.get("code") or "ERROR"),
                    message=str(last_error.get("message") or "서버가 연결을 닫았습니다."),
                ),
                EXIT_ERROR,
            )
        return (
            build_output(
                "error",
                tracker,
                code="CONNECTION_CLOSED",
                message="서버와의 연결이 끊겼습니다. `cli.py wait` 로 이어갈 수 있습니다.",
            ),
            EXIT_ERROR,
        )
    # error
    last_error = tracker.errors[-1] if tracker.errors else {}
    return (
        build_output(
            "error",
            tracker,
            code=str(last_error.get("code") or "ERROR"),
            message=str(last_error.get("message") or "요청을 처리하지 못했습니다."),
        ),
        EXIT_ERROR,
    )


def _persist(path: str | Path, session: Dict[str, Any], tracker: Tracker) -> None:
    """지금까지의 진행을 세션 파일에 반영한다(타임아웃 뒤 ``wait`` 재개용)."""

    merged = dict(session)
    for key, value in tracker.session_fields().items():
        if value is not None:
            merged[key] = value
    save_session(path, merged)


async def _open_and_wait(
    session: Dict[str, Any],
    path: str | Path,
    deadline: float,
    *,
    tile: Optional[int] = None,
    forfeit: bool = False,
    blocking: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """세션으로 재접속 → welcome → ``_loop`` → 결과 JSON."""

    tracker = Tracker()
    try:
        async with _connect(session["ws_url"], session.get("token"), _remaining(deadline)) as ws:
            await _handshake(ws, tracker, deadline)
            _persist(path, session, tracker)
            outcome = await _loop(
                ws, tracker, deadline, tile=tile, forfeit=forfeit, blocking=blocking
            )
            _persist(path, session, tracker)
            return _resolve_result(outcome, tracker)
    except CliError as exc:
        output = build_output("error", tracker, code=exc.code, message=exc.message)
        _fill_from_session(output, session)
        _safe_persist(path, session, tracker)
        return output, exc.exit_code
    except (OSError, InvalidHandshake, ConnectionClosed) as exc:
        output = build_output(
            "error",
            tracker,
            code="CONNECTION_ERROR",
            message=f"서버에 연결하지 못했습니다: {exc}",
        )
        _fill_from_session(output, session)
        _safe_persist(path, session, tracker)
        return output, EXIT_ERROR


def _fill_from_session(output: Dict[str, Any], session: Dict[str, Any]) -> None:
    """연결 전에 실패해도 방·좌석 정보는 알려 준다."""

    if output.get("room_id") is None:
        output["room_id"] = session.get("room_id")
    if output.get("player") is None:
        output["player"] = session.get("player")


def _safe_persist(path: str | Path, session: Dict[str, Any], tracker: Tracker) -> None:
    try:
        _persist(path, session, tracker)
    except OSError:  # pragma: no cover - 쓰기 실패가 결과 출력을 막지 않게
        pass


# ---------------------------------------------------------------------------
# 서브커맨드
# ---------------------------------------------------------------------------
def _session_from_invite(invite: Invite, token: Optional[str], timeout: float) -> Dict[str, Any]:
    return {
        "ws_url": invite.ws_url,
        "room_id": invite.room_id,
        "token": token,
        "player": None,
        "set_index": None,
        "round": None,
        "timeout": timeout,
    }


async def _cmd_join(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    invite = parse_invite(args.invite_url)
    token = args.token or invite.token
    session = _session_from_invite(invite, token, args.timeout)
    path = Path(args.session_file)
    deadline = _monotonic() + max(0.0, args.timeout)
    tracker = Tracker()

    try:
        async with _connect(invite.ws_url, token, max(0.0, args.timeout)) as ws:
            await _handshake(ws, tracker, deadline)
            # welcome 의 좌석 토큰을 즉시 저장한다(이후 명령은 토큰으로 같은 자리에 붙는다).
            session.update(tracker.session_fields())
            session["ws_url"] = invite.ws_url
            if token:
                session["token"] = tracker.token or token
            _persist(path, session, tracker)
            outcome = await _loop(ws, tracker, deadline)
            _persist(path, session, tracker)
            return _resolve_result(outcome, tracker)
    except CliError as exc:
        output = build_output("error", tracker, code=exc.code, message=exc.message)
        _fill_from_session(output, session)
        _safe_persist(path, session, tracker)
        return output, exc.exit_code
    except (OSError, InvalidHandshake, ConnectionClosed) as exc:
        output = build_output(
            "error",
            tracker,
            code="CONNECTION_ERROR",
            message=f"서버에 연결하지 못했습니다: {exc}",
        )
        _fill_from_session(output, session)
        _safe_persist(path, session, tracker)
        return output, EXIT_ERROR


async def _cmd_session(
    args: argparse.Namespace,
    *,
    tile: Optional[int] = None,
    forfeit: bool = False,
    blocking: bool = True,
) -> Tuple[Dict[str, Any], int]:
    session = load_session(args.session_file)
    deadline = _monotonic() + max(0.0, getattr(args, "timeout", DEFAULT_TIMEOUT))
    return await _open_and_wait(
        session,
        Path(args.session_file),
        deadline,
        tile=tile,
        forfeit=forfeit,
        blocking=blocking,
    )


def _parse_tile(raw: str) -> int:
    try:
        tile = int(str(raw).strip())
    except ValueError as exc:
        raise CliError("INVALID_TILE", f"타일은 0~8 사이의 정수여야 합니다: {raw!r}") from exc
    if tile not in TILES:
        raise CliError("INVALID_TILE", f"타일은 0~8 사이의 정수여야 합니다: {raw!r}")
    return tile


def _add_session_option(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """``--session-file`` 을 서브커맨드 뒤에도 쓸 수 있게 한다(앞/뒤 어느 쪽이든)."""

    parser.add_argument(
        "--session-file",
        default=argparse.SUPPRESS if suppress else DEFAULT_SESSION_FILE,
        help=f"세션 파일 경로 (기본 {DEFAULT_SESSION_FILE})",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="「흑과 백」 터미널 클라이언트 — 명령마다 JSON 하나를 출력한다.",
        epilog=(
            "종료 코드: 0 = my_turn/game_over(status 는 opponent_turn/waiting 포함), "
            "1 = error, 3 = timeout(세션 유지 → wait 로 재개)."
        ),
    )
    _add_session_option(parser)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    join = sub.add_parser("join", help="초대 링크로 입장하고 내 차례(또는 종료)까지 대기")
    join.add_argument("invite_url", help="초대 링크 (http://host:port/?room=CODE)")
    join.add_argument(
        "--token",
        default=None,
        help="좌석 토큰(방장 자신/AI 방 입장용). 없으면 빈 좌석을 배정받는다",
    )
    join.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="대기 초 (기본 50)")
    _add_session_option(join, suppress=True)

    submit = sub.add_parser("submit", help="타일을 제출하고 다시 내 차례(또는 종료)까지 대기")
    submit.add_argument("tile", help="제출할 타일 0~8")
    submit.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="대기 초 (기본 50)")
    _add_session_option(submit, suppress=True)

    wait = sub.add_parser("wait", help="재접속해 내 차례(또는 종료)까지 다시 대기")
    wait.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="대기 초 (기본 50)")
    _add_session_option(wait, suppress=True)

    status = sub.add_parser("status", help="현재 상태를 한 번만 조회")
    status.add_argument("--timeout", type=float, default=15.0, help="연결·응답 대기 초 (기본 15)")
    _add_session_option(status, suppress=True)

    forfeit = sub.add_parser("forfeit", help="기권하고 종료 상태까지 대기")
    forfeit.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="대기 초 (기본 50)")
    _add_session_option(forfeit, suppress=True)

    return parser


async def _dispatch(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    if args.command == "join":
        return await _cmd_join(args)
    if args.command == "submit":
        return await _cmd_session(args, tile=_parse_tile(args.tile))
    if args.command == "wait":
        return await _cmd_session(args)
    if args.command == "status":
        return await _cmd_session(args, blocking=False)
    if args.command == "forfeit":
        return await _cmd_session(args, forfeit=True)
    raise CliError("USAGE", f"알 수 없는 명령입니다: {args.command!r}")  # pragma: no cover


def run(argv: Optional[Sequence[str]] = None) -> Tuple[Dict[str, Any], int]:
    """``argv`` 를 실행하고 ``(출력 JSON, 종료 코드)`` 를 돌려준다(테스트·스크립트용)."""

    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help / 사용 오류
        if exc.code in (0, None):
            raise
        return (
            {
                "status": "error",
                "code": "USAGE",
                "message": "명령 사용법이 올바르지 않습니다. `cli.py --help` 를 확인하세요.",
            },
            EXIT_ERROR,
        )
    try:
        return asyncio.run(_dispatch(args))
    except CliError as exc:
        return (
            {"status": "error", "code": exc.code, "message": exc.message},
            exc.exit_code,
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    output, code = run(argv)
    print(_json(output))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
