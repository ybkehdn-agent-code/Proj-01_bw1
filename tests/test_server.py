"""`server.py` 테스트 — 방 생성·입장·ROOM_FULL·한 판 완주·정보 비노출·재접속·
기권·로그 저장·ROUND_RESULT 자동 전환·GC.

FastAPI ``TestClient`` 는 실제 ASGI 앱(WebSocket 포함)을 인메모리로 돌린다.
시간 상수(재접속 유예 0.35초, 결과 표시 0.05초 등)는 ``ServerSettings`` 로 줄여
테스트가 오래 기다리지 않게 했다.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

import engine
from ai import AI_VERSION
from server import AI_TOKEN, ROOM_CODE_LENGTH, ServerSettings, create_app


# ---------------------------------------------------------------------------
# 픽스처 / 헬퍼
# ---------------------------------------------------------------------------
def base_settings(tmp_path: Path, **overrides: Any) -> ServerSettings:
    values: Dict[str, Any] = dict(
        db_path=tmp_path / "games.db",
        reconnect_grace=0.35,
        round_result_seconds=0.05,
        gc_interval=0.05,
        room_idle_ttl=0.3,
        ended_room_ttl=30.0,
        heartbeat_interval=18.0,
        heartbeat_timeout=120.0,
        enable_gc=False,
        enable_heartbeat=False,
    )
    values.update(overrides)
    return ServerSettings(**values)


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(base_settings(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def create_room(client: TestClient) -> Dict[str, Any]:
    res = client.post("/api/rooms", json={"mode": "human"})
    assert res.status_code == 200, res.text
    return res.json()


def create_ai_room(client: TestClient, difficulty: str = "normal") -> Dict[str, Any]:
    res = client.post("/api/rooms", json={"mode": "ai", "difficulty": difficulty})
    assert res.status_code == 200, res.text
    return res.json()


def human_pick(view: Dict[str, Any]) -> int:
    """AI 대전 테스트에서 사람 좌석이 쓸 결정적 전략(라운드·세트에 따라 달라짐)."""

    remaining = view["you"]["remaining"]
    return remaining[(view["round"] + view["set_index"]) % len(remaining)]


def play_ai_game(
    ws, *, first_view: Optional[Dict[str, Any]] = None, max_steps: int = 400
) -> Dict[str, Any]:
    """AI 방에서 사람 좌석으로 한 판을 끝까지 진행하고 마지막 state 를 돌려준다.

    ``first_view`` 는 이미 받아 둔 관측(재대결 직후 등)을 이어서 쓰기 위한 것이다.
    """

    steps = 0
    view = first_view
    while True:
        if (
            view is not None
            and view["phase"] in ("WAITING_FIRST", "WAITING_SECOND")
            and view["turn"] == "A"
        ):
            submit(ws, view, human_pick(view))
            steps += 1
            assert steps <= max_steps, "AI 대전이 제한 횟수 안에 끝나지 않았습니다"
        msg = recv_until(ws, lambda m: m.get("type") == "state")
        if msg.get("ended"):
            return msg
        view = msg.get("state")


def ws_url(room: Dict[str, Any], token: Optional[str] = None) -> str:
    url = f"/ws?room={room['room_id']}"
    if token is not None:
        url += f"&token={token}"
    return url


def recv_until(
    ws,
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    *,
    limit: int = 40,
    only: Optional[str] = None,
) -> Dict[str, Any]:
    """조건을 만족하는 메시지가 올 때까지 읽는다(없으면 실패)."""

    for _ in range(limit):
        msg = ws.receive_json()
        assert isinstance(msg, dict), msg
        if only is not None and msg.get("type") != only:
            continue
        if predicate is None or predicate(msg):
            return msg
    raise AssertionError(f"기다린 메시지를 받지 못했습니다: {predicate=}")


def recv_view(ws) -> Dict[str, Any]:
    """게임 뷰가 담긴 state 메시지의 view 만 돌려준다."""

    return recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") is not None)["state"]


def recv_view_where(ws, predicate: Callable[[Dict[str, Any]], bool], limit: int = 12) -> Dict[str, Any]:
    for _ in range(limit):
        view = recv_view(ws)
        if predicate(view):
            return view
    raise AssertionError("조건에 맞는 상태를 받지 못했습니다.")


def submit(ws, view: Dict[str, Any], tile: int) -> None:
    ws.send_json(
        {"type": "submit", "set": view["set_index"], "round": view["round"], "tile": tile}
    )


def decisive_pick(player: str, view: Dict[str, Any]) -> int:
    """A 가 5라운드 연속 이기는 결정적 전략(조기 종료 유도).

    A 는 가장 큰 타일(8,7,6,5,4), B 는 4 이하 중 가장 큰 타일(4,3,2,1,0)을 내면
    (8,4)(7,3)(6,2)(5,1)(4,0) 로 A 가 5:0 → 남은 4라운드로는 역전 불가(early_win).
    """

    remaining = view["you"]["remaining"]
    if player == "A":
        return max(remaining)
    low = [tile for tile in remaining if tile <= 4]
    return max(low) if low else min(remaining)


@dataclass
class Session:
    """두 좌석을 묶어 상태를 동기화하며 진행하는 테스트 도우미."""

    ws: Dict[str, Any] = field(default_factory=dict)
    views: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def turn(self) -> Optional[str]:
        return self.views["A"]["turn"]

    @property
    def phase(self) -> str:
        return self.views["A"]["phase"]

    def play_turn(self, pick: Callable[[str, Dict[str, Any]], int]) -> tuple[str, int]:
        """지금 차례인 좌석이 타일을 내고, 두 좌석의 새 상태를 읽어 동기화한다."""

        player = self.turn
        assert player in ("A", "B"), player
        tile = pick(player, self.views[player])
        submit(self.ws[player], self.views[player], tile)
        self.sync()
        return player, tile

    def sync(self) -> None:
        for player in ("A", "B"):
            self.views[player] = recv_view(self.ws[player])

    def play_to_end(self, max_steps: int = 200) -> None:
        """결정적 전략으로 조기 종료까지 진행한다."""

        for _ in range(max_steps):
            if self.views["A"].get("game_over"):
                return
            if self.phase == "ROUND_RESULT":
                self.sync()  # 자동 전환(0.05초)된 다음 상태
                continue
            if self.phase == "GAME_OVER":
                return
            self.play_turn(decisive_pick)
        raise AssertionError("게임이 제한 횟수 안에 끝나지 않았습니다")


@contextlib.contextmanager
def session(client: TestClient, room: Dict[str, Any]) -> Iterator[Session]:
    """방장(A) + 손님(B) 두 소켓을 붙이고 게임 시작 상태까지 맞춘다."""

    with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
        recv_until(ws_a, only="welcome")
        with client.websocket_connect(ws_url(room)) as ws_b:
            recv_until(ws_b, only="welcome")
            s = Session(ws={"A": ws_a, "B": ws_b})
            s.sync()
            yield s


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
def test_health_endpoint(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["engine_version"] == engine.ENGINE_VERSION
    assert body["rooms"] == 0


def test_create_room_returns_secret_code_and_token(client: TestClient) -> None:
    body = create_room(client)
    assert re.fullmatch(r"[A-Za-z0-9]{6,8}", body["room_id"])
    assert len(body["room_id"]) == ROOM_CODE_LENGTH
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", body["token"])
    assert body["mode"] == "human"
    assert body["log_mode"] == "human_vs_human"
    assert body["invite_path"] == f"/?room={body['room_id']}"

    info = client.get(f"/api/rooms/{body['room_id']}").json()
    assert info["exists"] is True
    assert info["status"] == "waiting"
    assert info["seats_filled"] == 1


def test_room_codes_are_unique(client: TestClient) -> None:
    codes = {create_room(client)["room_id"] for _ in range(20)}
    assert len(codes) == 20


def test_ai_mode_is_available_now(client: TestClient) -> None:
    body = create_ai_room(client, "easy")
    assert body["mode"] == "ai"
    assert body["difficulty"] == "easy"
    assert body["log_mode"] == "human_vs_ai_easy"
    assert body["ai_version"] == AI_VERSION
    assert body["player"] == "A"
    assert body["invite_path"] == f"/?room={body['room_id']}"


# ---------------------------------------------------------------------------
# AI 대전
# ---------------------------------------------------------------------------
def test_create_ai_room_reports_difficulty_and_modes(client: TestClient) -> None:
    for difficulty, log_mode in (
        ("easy", "human_vs_ai_easy"),
        ("normal", "human_vs_ai_normal"),
        ("hard", "human_vs_ai_hard"),
    ):
        body = create_ai_room(client, difficulty)
        assert body["mode"] == "ai"
        assert body["difficulty"] == difficulty
        assert body["log_mode"] == log_mode
        assert body["ai_version"] == AI_VERSION
        assert body["token"] and body["token"] != AI_TOKEN
        info = client.get(f"/api/rooms/{body['room_id']}").json()
        assert info["mode"] == "ai"
        assert info["difficulty"] == difficulty
        assert info["seats_filled"] == 2  # 사람 A + AI B
        assert info["seats"]["B"]["is_ai"] is True
        assert info["seats"]["A"]["is_ai"] is False


def test_ai_room_rejects_unknown_difficulty(client: TestClient) -> None:
    res = client.post("/api/rooms", json={"mode": "ai", "difficulty": "impossible"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["code"] == "INVALID_DIFFICULTY"
    assert detail["difficulties"] == ["easy", "normal", "hard"]
    assert client.app.state.hub.rooms == {}


def test_ai_room_is_full_for_a_second_human(client: TestClient) -> None:
    room = create_ai_room(client, "normal")
    with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
        recv_until(ws_a, only="welcome")
        with client.websocket_connect(ws_url(room)) as second:
            error = recv_until(second, only="error")
            assert error["code"] == "ROOM_FULL"
        # AI 좌석 토큰은 존재하지 않으므로 어떤 문자열로도 붙을 수 없다.
        with client.websocket_connect(ws_url(room, "ai-seat")) as intruder:
            error = recv_until(intruder, only="error")
            assert error["code"] == "INVALID_TOKEN"


def test_health_reports_ai_version_and_difficulties(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ai_version"] == AI_VERSION
    assert body["difficulties"] == ["easy", "normal", "hard"]


def test_ai_takes_the_first_turn_automatically(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, ai_think_delay=0.01, opening_player="B")
    with TestClient(create_app(settings)) as client:
        room = create_ai_room(client, "normal")
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            welcome = recv_until(ws, only="welcome")
            assert welcome["mode"] == "ai" and welcome["difficulty"] == "normal"
            assert welcome["opponent_is_ai"] is True
            assert welcome["state"] is None  # 입장 순간에는 아직 시작 전
            # AI 가 먼저 타일을 내고, 사람(후공) 차례로 넘어온다.
            view = recv_view_where(ws, lambda v: v["phase"] == "WAITING_SECOND")
            assert view["turn"] == "A"
            assert view["first_player"] == "B"
            assert view["opponent"]["submitted_color"] in ("black", "white")
            assert len(view["you"]["remaining"]) == 9
            assert view["opponent"]["remaining_black"] + view["opponent"]["remaining_white"] == 8


def test_ai_waits_the_configured_delay_before_moving(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, ai_think_delay=0.25, opening_player="B")
    with TestClient(create_app(settings)) as client:
        room = create_ai_room(client, "easy")
        started = time.monotonic()
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            recv_until(ws, only="welcome")
            recv_view_where(ws, lambda v: v["phase"] == "WAITING_SECOND", limit=24)
        elapsed = time.monotonic() - started
        assert elapsed >= 0.2, f"AI 가 너무 빨리 두었습니다: {elapsed:.3f}s"


def test_ai_end_to_end_game_is_logged_with_ai_version(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, ai_think_delay=0.0, opening_player="A")
    with TestClient(create_app(settings)) as client:
        room = create_ai_room(client, "hard")
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            ended = play_ai_game(ws)
            assert ended["ended"] is True
            assert ended["end_reason"] in ("normal", "early_win", "forfeit", "disconnect")
            assert ended["winner"] in ("A", "B", None)

        hub = client.app.state.hub
        games = hub.storage.list_games()
        assert len(games) == 1
        stored = hub.storage.get_game(games[0]["game_id"])
        assert stored["mode"] == "human_vs_ai_hard"
        assert stored["ai_version"] == AI_VERSION
        assert stored["engine_version"] == engine.ENGINE_VERSION
        assert stored["room_id"] == room["room_id"]
        assert stored["end_reason"] == ended["end_reason"]
        assert stored["winner"] == ended["winner"]
        assert len(stored["rounds"]) >= 5


def test_ai_rematch_starts_a_new_game_without_ai_vote(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, ai_think_delay=0.0, opening_player="A")
    with TestClient(create_app(settings)) as client:
        room = create_ai_room(client, "normal")
        hub = client.app.state.hub
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            play_ai_game(ws)
            old_game_id = hub.rooms[room["room_id"]].game_id

            ws.send_json({"type": "rematch"})
            fresh = recv_view_where(
                ws, lambda v: v["phase"] != "GAME_OVER" and v["round"] == 1,
            )
            assert fresh["scores"] == {"A": 0, "B": 0}
            assert fresh["you"]["remaining"] == list(range(9))
            assert hub.rooms[room["room_id"]].game_id != old_game_id

            play_ai_game(ws, first_view=fresh)

        assert hub.storage.count_games() == 2
        modes = {game["mode"] for game in hub.storage.list_games()}
        assert modes == {"human_vs_ai_normal"}
        assert all(game["ai_version"] == AI_VERSION for game in hub.storage.list_games())


def test_ai_forfeit_is_recorded_for_the_ai(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, ai_think_delay=0.0, opening_player="A")
    with TestClient(create_app(settings)) as client:
        room = create_ai_room(client, "easy")
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            recv_until(ws, only="welcome")
            recv_view_where(ws, lambda v: v["turn"] == "A")
            ws.send_json({"type": "forfeit"})
            ended = recv_until(ws, lambda m: m.get("type") == "state" and m.get("ended") is True)
            assert ended["end_reason"] == "forfeit"
            assert ended["winner"] == "B"
            assert ended["loser"] == "A"

        games = client.app.state.hub.storage.list_games()
        assert len(games) == 1
        assert games[0]["mode"] == "human_vs_ai_easy"
        assert games[0]["ai_version"] == AI_VERSION
        assert games[0]["winner"] == "B"


def test_invalid_mode_is_rejected(client: TestClient) -> None:
    res = client.post("/api/rooms", json={"mode": "robot"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "INVALID_MODE"


def test_missing_room_is_404(client: TestClient) -> None:
    res = client.get("/api/rooms/ZZZZZZ")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "ROOM_NOT_FOUND"


def test_static_ui_is_served(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "흑과 백" in index.text
    assert "/static/game.js" in index.text
    # AI 대전 선택 → 난이도 3단계 화면
    assert 'data-screen="difficulty"' in index.text
    for difficulty in ("easy", "normal", "hard"):
        assert f'data-difficulty="{difficulty}"' in index.text
    assert "is-disabled" not in index.text.split('data-mode="ai"')[0]

    assert client.get("/static/style.css").status_code == 200
    js = client.get("/static/game.js")
    assert js.status_code == 200
    # WebSocket 주소 자동 계산 + 방별 토큰 키 + 자동 재연결(1→2→4초) + 15초 ping
    assert "location.protocol === 'https:' ? 'wss:' : 'ws:'" in js.text
    assert "bw_token_" in js.text
    assert "RECONNECT_DELAYS = [1000, 2000, 4000]" in js.text
    assert "PING_INTERVAL_MS = 15000" in js.text
    # AI 대전: 난이도 전송 · 생각 중 표시
    assert "mode: 'ai', difficulty" in js.text
    assert "AI가 생각 중…" in js.text
    assert "data-difficulty" in js.text


# ---------------------------------------------------------------------------
# 입장 / ROOM_FULL / 토큰
# ---------------------------------------------------------------------------
def test_two_players_join_and_game_starts(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
        welcome_a = recv_until(ws_a, only="welcome")
        assert welcome_a["player"] == "A"
        assert welcome_a["token"] == room["token"]
        assert welcome_a["status"] == "waiting"
        assert welcome_a["state"] is None

        with client.websocket_connect(ws_url(room)) as ws_b:
            welcome_b = recv_until(ws_b, only="welcome")
            assert welcome_b["player"] == "B"
            assert welcome_b["token"] != room["token"]

            view_a = recv_view(ws_a)
            view_b = recv_view(ws_b)
            assert view_a["viewer"] == "A" and view_b["viewer"] == "B"
            assert view_a["phase"] == engine.Phase.WAITING_FIRST.value
            assert view_a["opponent"]["id"] == "B"
            assert view_a["you"]["remaining"] == list(range(9))
            assert view_a["turn"] in ("A", "B")


def test_third_player_is_blocked_with_room_full(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])):
        with client.websocket_connect(ws_url(room)):
            with client.websocket_connect(ws_url(room)) as third:
                error = recv_until(third, only="error")
                assert error["code"] == "ROOM_FULL"
                assert "정원" in error["message"]


def test_unknown_token_is_rejected(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, "not-a-real-token")) as ws:
        error = recv_until(ws, only="error")
        assert error["code"] == "INVALID_TOKEN"


def test_unknown_room_is_reported(client: TestClient) -> None:
    with client.websocket_connect("/ws?room=NOPE01") as ws:
        error = recv_until(ws, only="error")
        assert error["code"] == "ROOM_NOT_FOUND"


def test_ping_gets_pong(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws:
        recv_until(ws, only="welcome")
        ws.send_json({"type": "ping", "t": 12345})
        pong = recv_until(ws, only="pong")
        assert pong["t"] == 12345
        assert isinstance(pong["server_time"], int)


def test_heartbeat_interval_is_15_to_20_seconds(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws:
        welcome = recv_until(ws, only="welcome")
        assert 15.0 <= welcome["heartbeat_seconds"] <= 20.0
        assert welcome["reconnect_grace_seconds"] == 0.35


# ---------------------------------------------------------------------------
# 한 판 완주 / 정보 비노출 / 로그
# ---------------------------------------------------------------------------
def test_full_game_ends_with_early_win_and_is_logged(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.play_to_end()
        view_a = s.views["A"]
        view_b = s.views["B"]

        assert view_a["phase"] == "GAME_OVER"
        assert view_a["game_over"]["winner"] == "A"
        assert view_a["game_over"]["end_reason"] == "early_win"
        assert view_b["game_over"]["winner"] == "A"

        # 복기는 GAME_OVER 에서만 실제 숫자가 열린다.
        replay = view_a["replay"]
        assert replay and all(isinstance(r["first_tile"], int) for r in replay)
        assert [r["round"] for r in replay] == list(range(1, len(replay) + 1))

    hub = client.app.state.hub
    games = hub.storage.list_games()
    assert len(games) == 1
    stored = hub.storage.get_game(games[0]["game_id"])
    assert stored["room_id"] == room["room_id"]
    assert stored["mode"] == "human_vs_human"
    assert stored["ai_version"] is None
    assert stored["end_reason"] == "early_win"
    assert stored["winner"] == "A"
    assert stored["engine_version"] == engine.ENGINE_VERSION
    assert stored["total_sets"] == 1
    assert len(stored["rounds"]) == len(replay)

    # 저장된 라운드 숫자가 실제 복기와 일치한다(좌석 A = p1).
    for stored_round, replayed in zip(stored["rounds"], replay):
        if replayed["first_player"] == "A":
            assert stored_round["p1_tile"] == replayed["first_tile"]
            assert stored_round["p2_tile"] == replayed["second_tile"]
        else:
            assert stored_round["p1_tile"] == replayed["second_tile"]
            assert stored_round["p2_tile"] == replayed["first_tile"]
        assert stored_round["p1_color"] == ("black" if stored_round["p1_tile"] % 2 == 0 else "white")
        assert stored_round["p2_color"] == ("black" if stored_round["p2_tile"] % 2 == 0 else "white")
        assert stored_round["think_time_ms"] is not None
        assert stored_round["second_think_time_ms"] is not None


def test_game_is_logged_only_once(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.play_to_end()
        # 종료 뒤 기권/동기화 메시지를 더 보내도 로그가 중복되지 않는다.
        s.ws["A"].send_json({"type": "forfeit"})
        s.ws["A"].send_json({"type": "sync"})
        recv_until(s.ws["A"], lambda m: m.get("type") == "state")
        assert client.app.state.hub.storage.count_games() == 1


def test_progressing_payloads_never_leak_opponent_numbers(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        for _ in range(8):
            if s.views["A"].get("game_over"):
                break
            view = s.views["A"]
            assert view["replay"] is None

            # 상대 정보는 id + 남은 흑/백 '개수' + 제출 '색상' 뿐이다.
            assert set(view["opponent"]) == {
                "id",
                "remaining_black",
                "remaining_white",
                "submitted_color",
            }
            assert "remaining" not in view["opponent"]

            # 완료 라운드도 본인 숫자 + 상대 색상만 담는다.
            for record in view["rounds"]:
                assert "opponent_tile" not in record
                assert record["opponent_color"] in ("black", "white")
                assert isinstance(record["your_tile"], int)

            payload = json.dumps(view, ensure_ascii=False)
            for forbidden in ("hands", "full_state", "opponent_tile", "opponent_tiles"):
                assert forbidden not in payload

            if s.phase == "ROUND_RESULT":
                # 결과 화면에도 상대 숫자는 없다.
                assert "opponent_tile" not in json.dumps(view["round_result"])
                s.sync()
                continue

            s.play_turn(
                lambda player, v: max(v["you"]["remaining"])
                if player == "A"
                else min(v["you"]["remaining"])
            )

# ---------------------------------------------------------------------------
# Idempotency / 오류 처리
# ---------------------------------------------------------------------------
def test_duplicate_submit_is_acknowledged_without_state_change(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        player = s.turn
        waiting = "B" if player == "A" else "A"
        view = s.views[player]
        tile = min(view["you"]["remaining"])
        submit(s.ws[player], view, tile)
        # 같은 요청이 한 번 더 오면 상태를 바꾸지 않고 duplicate ack 만 보낸다.
        submit(s.ws[player], view, tile)
        first_msgs = [s.ws[player].receive_json() for _ in range(2)]
        assert [m["type"] for m in first_msgs] == ["state", "ack"]
        assert first_msgs[1]["duplicate"] is True
        assert first_msgs[1]["tile"] == tile
        s.views[player] = first_msgs[0]["state"]

        # 후공이 이어서 내면 라운드가 정상 종료된다(중복 패킷이 상태를 깨지 않는다).
        s.views[waiting] = recv_view(s.ws[waiting])
        submit(s.ws[waiting], s.views[waiting], max(s.views[waiting]["you"]["remaining"]))
        s.sync()
        assert s.views["A"]["phase"] == "ROUND_RESULT"
        assert s.views["A"]["round_result"]["round"] == 1


def test_stale_round_request_is_rejected(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        player = s.turn
        view = s.views[player]
        s.ws[player].send_json(
            {"type": "submit", "set": view["set_index"], "round": 7,
             "tile": min(view["you"]["remaining"])}
        )
        error = recv_until(s.ws[player], only="error")
        assert error["code"] == "STALE_ROUND"


def test_wrong_turn_request_is_rejected(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        waiting = "B" if s.turn == "A" else "A"
        view = s.views[waiting]
        submit(s.ws[waiting], view, min(view["you"]["remaining"]))
        error = recv_until(s.ws[waiting], only="error")
        assert error["code"] == "NOT_YOUR_TURN"


def test_invalid_tile_value_is_rejected(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        player = s.turn
        view = s.views[player]
        s.ws[player].send_json(
            {"type": "submit", "set": view["set_index"], "round": view["round"], "tile": 42}
        )
        error = recv_until(s.ws[player], only="error")
        assert error["code"] == "INVALID_TILE"


def test_unknown_message_type_is_reported(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws:
        recv_until(ws, only="welcome")
        ws.send_json({"type": "dance"})
        error = recv_until(ws, only="error")
        assert error["code"] == "UNKNOWN_TYPE"


# ---------------------------------------------------------------------------
# 재접속 복구 / 연결 끊김
# ---------------------------------------------------------------------------
def test_reconnect_with_saved_token_restores_same_seat(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room)) as ws_b:  # 손님(B)
        recv_until(ws_b, only="welcome")
        with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
            recv_until(ws_a, only="welcome")
            s = Session(ws={"A": ws_a, "B": ws_b})
            s.sync()
            player = s.turn
            tile = min(s.views[player]["you"]["remaining"])
            s.play_turn(lambda p, v: tile if p == player else max(v["you"]["remaining"]))
            before_round = s.views["A"]["round"]

        # A 세션이 닫힌 뒤 저장된 토큰으로 같은 좌석에 다시 붙는다.
        with client.websocket_connect(ws_url(room, room["token"])) as ws_a2:
            welcome = recv_until(ws_a2, only="welcome")
            assert welcome["player"] == "A"
            assert welcome["token"] == room["token"]
            assert welcome["ended"] is False
            assert welcome["state"] is not None
            assert welcome["state"]["viewer"] == "A"
            assert welcome["state"]["round"] == before_round
            assert welcome["state"]["you"]["remaining"] == s.views["A"]["you"]["remaining"]
            # 상대(B)는 A의 재접속을 알게 된다(부전승 아님).
            notice = recv_until(
                ws_b,
                lambda m: m.get("type") == "state"
                and m.get("ended") is False
                and m.get("opponent_connected") is True,
            )
            assert notice["opponent_connected"] is True


def test_reconnect_within_grace_prevents_forfeit(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
        recv_until(ws_a, only="welcome")
        with client.websocket_connect(ws_url(room)) as ws_b:
            welcome_b = recv_until(ws_b, only="welcome")
            token_b = welcome_b["token"]
            Session(ws={"A": ws_a, "B": ws_b}).sync()
        # B 가 잠깐 끊겼다가 유예(0.35초) 안에 저장된 토큰으로 돌아온다.
        with client.websocket_connect(ws_url(room, token_b)) as ws_b2:
            welcome = recv_until(ws_b2, only="welcome")
            assert welcome["player"] == "B"
            assert welcome["ended"] is False
            time.sleep(0.5)  # 유예 시간이 지나도 게임은 계속된다.
            ws_b2.send_json({"type": "sync"})
            state = recv_until(ws_b2, lambda m: m.get("type") == "state")
            assert state["ended"] is False
            assert state["state"]["phase"] == engine.Phase.WAITING_FIRST.value


def test_disconnect_after_grace_gives_opponent_the_win(client: TestClient) -> None:
    room = create_room(client)
    with client.websocket_connect(ws_url(room, room["token"])) as ws_a:
        recv_until(ws_a, only="welcome")
        with client.websocket_connect(ws_url(room)) as ws_b:
            recv_until(ws_b, only="welcome")
            Session(ws={"A": ws_a, "B": ws_b}).sync()
        # B 가 나가고 유예(0.35초)가 지나면 A 승리(disconnect).
        gone = recv_until(
            ws_a, lambda m: m.get("type") == "state" and m.get("opponent_connected") is False
        )
        assert gone["ended"] is False
        ended = recv_until(ws_a, lambda m: m.get("type") == "state" and m.get("ended") is True)
        assert ended["end_reason"] == "disconnect"
        assert ended["winner"] == "A"
        assert ended["loser"] == "B"

    games = client.app.state.hub.storage.list_games()
    assert len(games) == 1
    assert games[0]["end_reason"] == "disconnect"
    assert games[0]["winner"] == "A"


# ---------------------------------------------------------------------------
# 기권 / 재대결 / 라운드 전환
# ---------------------------------------------------------------------------
def test_forfeit_ends_game_and_saves_log(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.ws["A"].send_json({"type": "forfeit"})
        ended_a = recv_until(
            s.ws["A"], lambda m: m.get("type") == "state" and m.get("ended") is True
        )
        ended_b = recv_until(
            s.ws["B"], lambda m: m.get("type") == "state" and m.get("ended") is True
        )
        assert ended_a["end_reason"] == "forfeit"
        assert ended_a["winner"] == "B"
        assert ended_b["winner"] == "B" and ended_b["loser"] == "A"

    games = client.app.state.hub.storage.list_games()
    assert len(games) == 1
    assert games[0]["end_reason"] == "forfeit"
    assert games[0]["winner"] == "B"


def test_rematch_starts_a_new_game(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.play_to_end()
        hub = client.app.state.hub
        old_game_id = hub.rooms[room["room_id"]].game_id

        s.ws["A"].send_json({"type": "rematch"})
        pending = recv_until(
            s.ws["B"], lambda m: m.get("type") == "state" and m.get("ended") is True
        )
        assert "A" in pending["rematch_votes"]
        assert hub.rooms[room["room_id"]].game_id == old_game_id  # 아직 새 판 아님

        s.ws["B"].send_json({"type": "rematch"})
        fresh = recv_view_where(
            s.ws["A"],
            lambda v: v["phase"] != "GAME_OVER" and v["round"] == 1 and v["set_index"] == 0,
        )
        assert fresh["scores"] == {"A": 0, "B": 0}
        assert fresh["you"]["remaining"] == list(range(9))
        assert hub.rooms[room["room_id"]].game_id != old_game_id

        s.views["A"] = fresh
        s.views["B"] = recv_view_where(
            s.ws["B"], lambda v: v["phase"] != "GAME_OVER" and v["round"] == 1
        )
        s.play_to_end()

    assert client.app.state.hub.storage.count_games() == 2


def test_rematch_before_game_over_is_refused(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.ws["A"].send_json({"type": "rematch"})
        error = recv_until(s.ws["A"], only="error")
        assert error["code"] == "NOT_FINISHED"


def test_both_players_confirm_skips_round_result_wait(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, round_result_seconds=30.0)
    app = create_app(settings)
    with TestClient(app) as client:
        room = create_room(client)
        with session(client, room) as s:
            s.play_turn(decisive_pick)
            s.play_turn(decisive_pick)
            assert s.phase == "ROUND_RESULT"

            s.ws["A"].send_json({"type": "next"})
            s.ws["B"].send_json({"type": "next"})
            advanced = recv_view_where(s.ws["A"], lambda v: v["round"] == 2)
            assert advanced["phase"] == engine.Phase.WAITING_FIRST.value


def test_round_result_auto_advances_after_delay(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        s.play_turn(decisive_pick)
        s.play_turn(decisive_pick)
        assert s.phase == "ROUND_RESULT"
        # 아무도 '다음'을 누르지 않아도 0.05초 뒤 자동 전환된다.
        s.sync()
        assert s.views["A"]["round"] == 2
        assert s.views["A"]["phase"] == engine.Phase.WAITING_FIRST.value


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------
def test_joining_a_finished_room_is_refused(client: TestClient) -> None:
    room = create_room(client)
    with session(client, room) as s:
        # B 가 '나가기' → 기권 처리되고 좌석이 비워진다(방은 종료 상태).
        s.ws["B"].send_json({"type": "leave"})
        ended = recv_until(
            s.ws["A"], lambda m: m.get("type") == "state" and m.get("ended") is True
        )
        assert ended["end_reason"] == "forfeit"
    # 빈 좌석이 있어도 이미 끝난 방에는 새로 들어갈 수 없다.
    with client.websocket_connect(ws_url(room)) as third:
        error = recv_until(third, only="error")
        assert error["code"] == "ROOM_CLOSED"


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------
def test_gc_removes_idle_rooms(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, enable_gc=True, gc_interval=0.05, room_idle_ttl=0.2)
    app = create_app(settings)
    with TestClient(app) as client:
        room = create_room(client)
        assert client.get(f"/api/rooms/{room['room_id']}").status_code == 200
        time.sleep(0.8)
        assert client.get(f"/api/rooms/{room['room_id']}").status_code == 404
        assert client.get("/api/health").json()["rooms"] == 0


def test_gc_keeps_room_while_connected(tmp_path: Path) -> None:
    settings = base_settings(tmp_path, enable_gc=True, gc_interval=0.05, room_idle_ttl=0.2)
    app = create_app(settings)
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(ws_url(room, room["token"])) as ws:
            recv_until(ws, only="welcome")
            time.sleep(0.5)  # TTL 보다 오래 연결만 유지
            ws.send_json({"type": "sync"})
            recv_until(ws, lambda m: m.get("type") == "state")
        assert client.get(f"/api/rooms/{room['room_id']}").status_code == 200


def test_gc_removes_ai_room_nobody_joined(tmp_path: Path) -> None:
    """AI 좌석은 항상 '접속됨' 이지만, 사람이 안 들어온 방은 유휴로 정리된다."""

    settings = base_settings(
        tmp_path, enable_gc=True, gc_interval=0.05, room_idle_ttl=0.2, ai_think_delay=0.0
    )
    app = create_app(settings)
    with TestClient(app) as client:
        room = create_ai_room(client, "easy")
        assert client.get(f"/api/rooms/{room['room_id']}").status_code == 200
        time.sleep(0.8)
        assert client.get(f"/api/rooms/{room['room_id']}").status_code == 404
        assert client.get("/api/health").json()["rooms"] == 0
