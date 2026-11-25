"""`cli.py` 테스트 — URL 파싱 · 실제 uvicorn 서버 상대 한 판 완주 · 타임아웃/재개.

서버는 임시 포트로 백그라운드 **스레드**에서 띄우고(모듈 스코프 1회), CLI 는
``cli.run([...])`` 을 그대로 호출한다(한 명령 = 한 접속). 사람 대전 상대는
두 번째 WebSocket 클라이언트(스레드)가 맡고, AI 대전은 방장 토큰으로 입장한다.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

import cli
from server import ServerSettings, create_app

ROOT = Path(__file__).resolve().parents[1]
Pick = Callable[[List[int]], int]


# ---------------------------------------------------------------------------
# 테스트용 서버(임시 포트, 백그라운드 스레드)
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):
    """실제 uvicorn 서버를 임시 포트에 띄우고 base URL 을 준다."""

    db_path = tmp_path_factory.mktemp("cli-db") / "games.db"
    settings = ServerSettings(
        db_path=db_path,
        ai_think_delay=0.0,
        round_result_seconds=0.05,
        reconnect_grace=30.0,
        enable_gc=False,
        enable_heartbeat=False,
        opening_player="A",  # 사람 대전/ AI 대전 모두 A 가 먼저 — 테스트를 결정적으로
    )
    app = create_app(settings)
    port = _free_port()
    uv_config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", loop="asyncio"
    )
    uv_server = uvicorn.Server(uv_config)
    thread = threading.Thread(target=uv_server.run, name="bw-cli-test-server", daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=1.0).status_code == 200:
                break
        except Exception:  # 서버가 아직 listen 전
            time.sleep(0.05)
    else:  # pragma: no cover - 환경 문제
        uv_server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("테스트용 uvicorn 서버가 뜨지 않았습니다.")

    yield base

    uv_server.should_exit = True
    thread.join(timeout=10)


def create_room(base: str, mode: str = "human", difficulty: Optional[str] = None) -> Dict[str, Any]:
    response = httpx.post(
        f"{base}/api/rooms", json={"mode": mode, "difficulty": difficulty}, timeout=5.0
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 두 번째 WebSocket 클라이언트(사람 대전 상대)
# ---------------------------------------------------------------------------
class ScriptedOpponent(threading.Thread):
    """A 좌석으로 접속해, 내 차례면 ``pick`` 한 장을 내고 결과 화면은 넘긴다."""

    def __init__(self, base: str, room_id: str, token: str, player: str = "A", pick: Optional[Pick] = None):
        super().__init__(name=f"bw-opponent-{room_id}", daemon=True)
        self.ws_url = base.replace("http://", "ws://") + "/ws"
        self.room_id = room_id
        self.token = token
        self.player = player
        self.pick: Pick = pick or (lambda tiles: tiles[0])
        self.moves_enabled = threading.Event()
        self.moves_enabled.set()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.states: List[Dict[str, Any]] = []
        self.submitted: List[tuple] = []
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # pragma: no cover - 테스트 디버깅용
            self.error = exc
        finally:
            self.ready.set()

    async def _run(self) -> None:
        query = urlencode({"room": self.room_id, "token": self.token})
        url = f"{self.ws_url}?{query}"
        async with connect(url, open_timeout=10, ping_interval=None) as ws:
            self.ready.set()
            while not self.stopped.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "t": message.get("t")}))
                    continue
                if kind not in ("welcome", "state"):
                    continue
                self.states.append(message)
                view = message.get("state")
                if not isinstance(view, dict) or message.get("ended"):
                    continue
                if view.get("phase") == "GAME_OVER":
                    continue
                if view.get("phase") == "ROUND_RESULT":
                    await ws.send(json.dumps({"type": "next"}))
                    continue
                if view.get("turn") != self.player or not self.moves_enabled.is_set():
                    continue
                key = (view.get("set_index"), view.get("round"))
                tiles = view["you"]["remaining"]
                tile = self.pick(tiles)
                await ws.send(
                    json.dumps(
                        {"type": "submit", "tile": tile,
                         "set": view.get("set_index"), "round": view.get("round")}
                    )
                )
                self.submitted.append((key, tile))

    def stop(self) -> None:
        self.stopped.set()
        self.join(timeout=5)


# ---------------------------------------------------------------------------
# URL 파싱
# ---------------------------------------------------------------------------
def test_parse_invite_url_variants():
    assert cli.parse_invite_url("http://192.168.0.12:8000/?room=AbC123") == (
        "ws://192.168.0.12:8000/ws?room=AbC123",
        "AbC123",
    )
    # https → wss, 포트 생략
    assert cli.parse_invite_url("https://bw.example.com/?room=Zz9") == (
        "wss://bw.example.com/ws?room=Zz9",
        "Zz9",
    )
    # ws/wss URL 을 그대로 줘도 된다
    assert cli.parse_invite_url("ws://127.0.0.1:9000/ws?room=QQ11") == (
        "ws://127.0.0.1:9000/ws?room=QQ11",
        "QQ11",
    )
    # 다른 쿼리는 버리고 room 만 남긴다
    assert cli.parse_invite_url("http://host:1234/?room=Ro0m&utm=x#frag") == (
        "ws://host:1234/ws?room=Ro0m",
        "Ro0m",
    )
    # 경로 접두사(리버스 프록시) 유지
    assert cli.parse_invite_url("https://games.example.com/bw/?room=PP77") == (
        "wss://games.example.com/bw/ws?room=PP77",
        "PP77",
    )
    # 앞뒤 공백·IPv6 호스트
    assert cli.parse_invite_url("  http://[::1]:8000/?room=Six6  ")[0] == (
        "ws://[::1]:8000/ws?room=Six6"
    )


def test_parse_invite_reads_token_from_query():
    invite = cli.parse_invite("http://host:8000/?room=AbC123&token=tok-1")
    assert invite.token == "tok-1"
    assert invite.room_id == "AbC123"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ftp://host/?room=ABC",
        "http://host:8000/?room=",
        "http://host:8000/?room_id=ABC",
        "http://:8000/?room=ABC",
        "not-a-url",
    ],
)
def test_parse_invite_url_rejects_bad_input(bad: str):
    with pytest.raises(cli.CliError) as info:
        cli.parse_invite(bad)
    assert info.value.code == "INVALID_URL"


def test_build_output_has_public_fields_only():
    tracker = cli.Tracker()
    tracker.feed(
        {
            "type": "state",
            "room": "AbC123",
            "you": "B",
            "ended": False,
            "winner": None,
            "end_reason": None,
            "state": {
                "viewer": "B",
                "phase": "WAITING_SECOND",
                "set_index": 0,
                "round": 2,
                "turn": "B",
                "scores": {"A": 1, "B": 0},
                "you": {"id": "B", "remaining": [1, 3, 5, 7], "submitted": None},
                "opponent": {
                    "id": "A",
                    "remaining_black": 4,
                    "remaining_white": 4,
                    "submitted_color": "black",
                },
                "rounds": [],
                "round_result": None,
                "replay": None,
            },
        }
    )
    output = cli.build_output("my_turn", tracker)
    assert output["status"] == "my_turn"
    assert output["room_id"] == "AbC123"
    assert output["player"] == "B"
    assert output["set_index"] == 0 and output["round"] == 2
    assert output["phase"] == "WAITING_SECOND"
    assert output["my_tiles"] == [1, 3, 5, 7]
    assert output["my_score"] == 0 and output["opp_score"] == 1
    assert output["opp_remaining_colors"] == {"black": 4, "white": 4}
    assert output["opp_submitted_color"] == "black"
    assert output["last_round_result"] is None
    # 진행 중에는 복기(history)를 싣지 않는다
    assert "history" not in output


def test_join_unknown_room_reports_server_error(server: str, tmp_path: Path):
    output, code = cli.run(
        [
            "join",
            f"{server}/?room=NOPE99",
            "--timeout",
            "5",
            "--session-file",
            str(tmp_path / "x.json"),
        ]
    )
    assert code == cli.EXIT_ERROR
    assert output["status"] == "error"
    assert output["code"] == "ROOM_NOT_FOUND"
    assert output["room_id"] == "NOPE99"


# ---------------------------------------------------------------------------
# 세션 파일 / 오류 경로
# ---------------------------------------------------------------------------
def test_missing_session_file_is_error(tmp_path: Path):
    output, code = cli.run(
        ["status", "--session-file", str(tmp_path / "nope.json"), "--timeout", "1"]
    )
    assert code == cli.EXIT_ERROR
    assert output["status"] == "error" and output["code"] == "NO_SESSION"


def test_submit_rejects_bad_tile_before_connecting(tmp_path: Path):
    session = tmp_path / "s.json"
    cli.save_session(
        session,
        {
            "ws_url": "ws://127.0.0.1:1/ws?room=AbC",
            "room_id": "AbC",
            "token": "t",
            "player": "A",
            "set_index": 0,
            "round": 1,
        },
    )
    output, code = cli.run(["submit", "9", "--session-file", str(session), "--timeout", "1"])
    assert code == cli.EXIT_ERROR
    assert output["code"] == "INVALID_TILE"


def test_subprocess_prints_single_json_document(server: str, tmp_path: Path):
    room = create_room(server, mode="ai", difficulty="easy")
    session = tmp_path / "sub.json"
    command = [
        sys.executable,
        "cli.py",
        "join",
        f"{server}{room['invite_path']}",
        "--token",
        room["token"],
        "--timeout",
        "10",
        "--session-file",
        str(session),
    ]
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == cli.EXIT_OK, proc.stderr
    data = json.loads(proc.stdout)  # stdout 은 JSON 문서 하나
    assert data["status"] == "my_turn"
    assert data["player"] == "A"
    assert data["phase"] == "WAITING_FIRST"
    assert session.is_file()


# ---------------------------------------------------------------------------
# 타임아웃 · 재개
# ---------------------------------------------------------------------------
def test_join_times_out_then_wait_resumes(server: str, tmp_path: Path):
    room = create_room(server)
    session = tmp_path / "wait.json"
    opponent = ScriptedOpponent(server, room["room_id"], room["token"], player="A")
    opponent.moves_enabled.clear()  # 처음에는 A 가 움직이지 않는다
    opponent.start()
    assert opponent.ready.wait(10)

    # 1) B 로 입장 — A 가 선공이라 내 차례가 아니다 → timeout(게임은 시작됨)
    output, code = cli.run(
        [
            "join",
            f"{server}{room['invite_path']}",
            "--timeout",
            "0.6",
            "--session-file",
            str(session),
        ]
    )
    assert code == cli.EXIT_TIMEOUT, output
    assert output["status"] == "timeout"
    assert output["room_id"] == room["room_id"]
    assert output["player"] == "B"
    assert output["phase"] == "WAITING_FIRST"
    assert output["waiting_for"] == "my_turn"

    saved = json.loads(session.read_text(encoding="utf-8"))
    assert saved["room_id"] == room["room_id"]
    assert saved["token"] and saved["player"] == "B"
    assert saved["set_index"] == 0 and saved["round"] == 1

    # 2) status 는 상대 차례를 한 번만 알려 준다
    status, code = cli.run(["status", "--session-file", str(session), "--timeout", "5"])
    assert code == cli.EXIT_OK and status["status"] == "opponent_turn"
    assert status["turn"] == "A"

    # 3) wait 도 아직 timeout
    again, code = cli.run(["wait", "--timeout", "0.4", "--session-file", str(session)])
    assert code == cli.EXIT_TIMEOUT and again["status"] == "timeout"

    # 4) A 가 움직이면 wait 가 내 차례로 돌아온다
    opponent.moves_enabled.set()
    resumed, code = cli.run(["wait", "--timeout", "10", "--session-file", str(session)])
    assert code == cli.EXIT_OK, resumed
    assert resumed["status"] == "my_turn"
    assert resumed["phase"] == "WAITING_SECOND"
    assert resumed["opp_submitted_color"] in ("black", "white")
    assert resumed["last_round_result"] is None
    opponent.stop()


def test_status_reports_waiting_before_opponent_joins(server: str, tmp_path: Path):
    room = create_room(server)
    session = tmp_path / "alone.json"
    output, code = cli.run(
        [
            "join",
            f"{server}{room['invite_path']}",
            "--timeout",
            "0.3",
            "--session-file",
            str(session),
        ]
    )
    assert code == cli.EXIT_TIMEOUT and output["status"] == "timeout"
    assert output["phase"] is None
    assert output["waiting_for"] == "game_start"

    status, code = cli.run(["status", "--session-file", str(session), "--timeout", "5"])
    assert code == cli.EXIT_OK and status["status"] == "waiting"


# ---------------------------------------------------------------------------
# 한 판 완주
# ---------------------------------------------------------------------------
def test_full_game_with_second_ws_client(server: str, tmp_path: Path):
    room = create_room(server)
    session = tmp_path / "human.json"
    # 상대는 시드 고정 난수(같은 정책끼리 두면 매 세트 4:4 무승부로 연장이 끝없이 이어진다).
    rng = random.Random(20261126)
    opponent = ScriptedOpponent(
        server, room["room_id"], room["token"], player="A", pick=lambda tiles: rng.choice(tiles)
    )
    opponent.start()
    assert opponent.ready.wait(10)

    output, code = cli.run(
        [
            "join",
            f"{server}{room['invite_path']}",
            "--timeout",
            "15",
            "--session-file",
            str(session),
        ]
    )
    assert code == cli.EXIT_OK, output

    rounds = 0
    while output["status"] == "my_turn":
        assert output["phase"] in ("WAITING_FIRST", "WAITING_SECOND")
        assert output["turn"] == output["player"]
        assert output["my_tiles"], output
        tile = max(output["my_tiles"])  # B 는 큰 수를 내 A 를 압박한다
        output, code = cli.run(
            ["submit", str(tile), "--timeout", "15", "--session-file", str(session)]
        )
        assert code == cli.EXIT_OK, output
        assert output["status"] in ("my_turn", "game_over"), output
        rounds += 1
        assert rounds < 40, "게임이 끝나지 않았습니다"

    assert output["status"] == "game_over"
    assert output["phase"] == "GAME_OVER"
    assert output["winner"] in ("A", "B")
    assert output["end_reason"] in ("normal", "early_win")
    assert output["my_score"] is not None and output["opp_score"] is not None
    assert output["my_score"] != output["opp_score"]
    # 복기: 전체 라운드의 실제 숫자가 공개된다
    history = output["history"]
    assert history and len(history) == sum(1 for _ in history)
    assert {"set_index", "round", "first_tile", "second_tile", "winner"} <= set(history[0])
    # 세션도 최종 라운드로 갱신되어 있다
    saved = json.loads(session.read_text(encoding="utf-8"))
    assert saved["set_index"] == output["set_index"]
    assert saved["round"] == output["round"]
    opponent.stop()


def test_ai_room_full_game_with_creator_token(server: str, tmp_path: Path):
    room = create_room(server, mode="ai", difficulty="easy")
    session = tmp_path / "ai.json"

    output, code = cli.run(
        [
            "join",
            f"{server}{room['invite_path']}",
            "--token",
            room["token"],
            "--timeout",
            "15",
            "--session-file",
            str(session),
        ]
    )
    assert code == cli.EXIT_OK, output
    assert output["player"] == "A"
    assert output["opponent_is_ai"] is True

    turns = 0
    while output["status"] == "my_turn":
        output, code = cli.run(
            [
                "submit",
                str(max(output["my_tiles"])),
                "--timeout",
                "15",
                "--session-file",
                str(session),
            ]
        )
        assert code == cli.EXIT_OK, output
        turns += 1
        assert turns < 40, "AI 대전이 끝나지 않았습니다"

    assert output["status"] == "game_over"
    assert output["winner"] in ("A", "B")
    assert output["history"]


def test_forfeit_ends_game_and_names_winner(server: str, tmp_path: Path):
    room = create_room(server, mode="ai", difficulty="easy")
    session = tmp_path / "ff.json"
    output, code = cli.run(
        [
            "join",
            f"{server}{room['invite_path']}",
            "--token",
            room["token"],
            "--timeout",
            "15",
            "--session-file",
            str(session),
        ]
    )
    assert code == cli.EXIT_OK and output["status"] == "my_turn"

    output, code = cli.run(["forfeit", "--timeout", "10", "--session-file", str(session)])
    assert code == cli.EXIT_OK, output
    assert output["status"] == "game_over"
    assert output["end_reason"] == "forfeit"
    assert output["winner"] == "B"  # 기권한 A 의 상대(AI B) 승
    assert output["player"] == "A"
