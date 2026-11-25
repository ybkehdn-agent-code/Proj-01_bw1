# 「흑과 백 (Black & White)」 — Proj-01_bw1

색은 보이고 **숫자는 숨는** 1:1 심리 대전 웹 앱입니다.
브라우저만 있으면 플레이할 수 있고, 같은 순수 파이썬 엔진 위에서
사람 대전(2단계)과 AI 대전(3단계)이 동작합니다.

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| 1단계 | `engine.py` — 규칙·상태 머신·정보 가시성·RL 인터페이스 + pytest 47개 | 완료 |
| 2단계 | `storage.py` · `server.py` · `static/` — 온라인 사람 대전 + 로그 저장 | 완료 |
| 3단계 | `ai.py` — 룰·탐색 AI(쉬움/보통/어려움) 를 같은 방 구조에 연결 | 완료 |
| 터미널 | `cli.py` — 피클/터미널 플레이용 CLI 클라이언트(JSON 출력) | 완료 |

## 파일

| 경로 | 내용 |
| --- | --- |
| `engine.py` | 상태 머신·규칙·승패 판정·조기 종료·연장전·`view_for` 정보 가시성·RL 인터페이스 |
| `ai.py` | AI 플레이어 — `BaseAI.select_action(observation)` 인터페이스, `create_ai(difficulty)` 팩토리, 쉬움(무작위)·보통(기댓값 휴리스틱)·어려움(신념 + Expectimax/Minimax) |
| `storage.py` | `LogRepository` 인터페이스 + `SQLiteLogRepository`(WAL, 기본 `data/games.db`) |
| `server.py` | FastAPI + WebSocket 딜러: 방 생성·입장·재접속·heartbeat·`ROUND_RESULT` 전환·기권·재대결·TTL GC·**AI 좌석/턴 진행**·static 서빙 |
| `cli.py` | 터미널/피클용 CLI 클라이언트 — 초대 링크 파싱·세션 토큰 재접속·`join`/`submit`/`wait`/`status`/`forfeit`, 명령마다 JSON 하나 출력 |
| `static/index.html` | 첫 화면 → 대전 모드 → (AI) 난이도 선택 → 대기실(초대 링크) → 게임 → 종료/복기 |
| `static/style.css` | 흑/백 모노크롬 반응형 테마(모바일 터치 타일 58px 이상) |
| `static/game.js` | WebSocket 송수신·자동 재연결·ping·타일 선택→제출 2단계 UI·복기 표·AI 생각 중 표시 |
| `tests/test_engine.py` | 1단계 규칙·정보 은닉·Self-Play 불변식 (47개) |
| `tests/test_storage.py` | WAL·스키마·저장/조회·멱등 저장 (12개) |
| `tests/test_ai.py` | 난이도별 합법성·정보 은닉·신념 추정·시뮬레이션 승률 (21개) |
| `tests/test_server.py` | 방 생성·입장·ROOM_FULL·완주·비노출·재접속·기권·로그·GC·**AI 대전 E2E** (42개) |
| `tests/test_cli.py` | CLI URL 파싱·세션 파일·타임아웃/`wait` 재개·2번째 WS 클라이언트/AI 방 한 판 완주 (18개) |
| `requirements.txt` | `pytest`, `fastapi`, `uvicorn[standard]`, `websockets`(CLI 클라이언트), `httpx`(TestClient용) |
| `data/` | 로컬 게임 로그(SQLite + WAL). `.gitignore` 에 포함 — 커밋되지 않습니다 |

> `engine.py` 규칙·`view_for` 정보 가시성·사람 대전·로그 스키마는 1·2단계에서
> 검증된 그대로이고, 3단계는 `ai.py` 와 서버의 **AI 좌석 연결**만 더했습니다.

## 실행

```bash
cd work/Proj-01_bw1
python -m venv .venv && . .venv/bin/activate      # 이미 있으면 생략
pip install -r requirements.txt

# 테스트(전체 140개)
pytest tests -q

# 서버 실행
uvicorn server:app --host 0.0.0.0 --port 8000
```

- 내 PC: <http://localhost:8000>
- 같은 와이파이의 휴대폰/다른 PC: `http://<내부 IP>:8000` (예: `192.168.0.12:8000`)
- 외부 공개(모바일 데이터): Cloudflare Tunnel 등으로 터널을 열면
  프론트엔드가 `wss://` 로 자동 전환됩니다(주소 하드코딩 없음).

### 두 사람이 하는 순서

1. 방장: **방 만들기 → 사람 대전** → 초대 링크(`.../?room=XXXXXX`)가 만들어지고 **복사** 버튼으로 공유.
2. 상대: 링크를 열면 자동으로 같은 방에 입장(정원 2명). 두 사람이 모이면 바로 시작.
3. 타일을 **터치해 선택(강조) → ‘제출하기’** 로 제출. 한 브라우저에서 탭 2개로 테스트해도
   토큰이 `sessionStorage` 의 방별 키(`bw_token_${roomId}`)로 나뉘어 서로 덮어쓰지 않습니다.

### AI 와 하는 순서

1. **방 만들기 → AI 대전 → 난이도(쉬움/보통/어려움)** 를 고르면 초대 링크 없이 바로 입장.
2. AI 차례에는 서버가 AI 의 수를 계산해 자동으로 내고, 화면에는 **“AI가 생각 중…”** 이 표시됩니다.
3. 끝나면 **다시 대결** 한 번으로 같은 난이도로 새 판을 시작합니다(AI 의 동의 절차 없음).

### 터미널/피클에서 하는 순서 (`cli.py`)

브라우저와 **같은 WebSocket 프로토콜**을 쓰는 CLI 클라이언트입니다. 한 명령이 한 번의
접속으로 끝나고(세션 토큰으로 같은 좌석에 다시 붙음), stdout 에 **JSON 한 개**를 출력합니다.

```bash
cd work/Proj-01_bw1

# 1) 초대 링크로 입장 — 내 차례(또는 종료)까지 블로킹. 타임아웃이면 status:"timeout"
python cli.py join 'http://192.168.0.12:8000/?room=AbC123' --timeout 50

# 2) 내 차례가 되면 타일(0~8) 하나 제출 → 다시 내 차례/종료까지 대기
python cli.py submit 7

# 3) 타임아웃으로 빠져나왔으면 이어서 대기 (세션은 그대로)
python cli.py wait

# 4) 한 번만 조회 / 기권
python cli.py status
python cli.py forfeit
```

- **세션 파일**: `.bw_session.json`(기본, `.gitignore` 포함)에 `ws_url`·`room_id`·`token`·`player`·`set_index`·`round` 를 저장·갱신합니다. `--session-file other.json` 으로 바꿀 수 있고(서브커맨드 뒤에도 가능), 좌석이 다르면 파일도 따로 쓰세요.
- **AI 방**: 방을 만든 사람의 좌석(A)이 이미 차 있으므로 `--token`(방 생성 응답의 `token`)으로 입장합니다.
  ```bash
  TOKEN=$(curl -s -X POST localhost:8000/api/rooms -H 'content-type: application/json' \
           -d '{"mode":"ai","difficulty":"easy"}' | python -c 'import json,sys; d=json.load(sys.stdin); print(d["token"])')
  python cli.py join 'http://localhost:8000/?room=<CODE>' --token "$TOKEN"
  ```
- **출력 필드**: `status`(`my_turn`/`game_over`/`timeout`/`error`, `status` 명령은 `opponent_turn`/`waiting` 도), `room_id`, `player`, `set_index`, `round`, `phase`, `turn`, `my_tiles`, `my_score`, `opp_score`, `opp_remaining_colors`, `opp_submitted_color`, `last_round_result`, `winner`, `end_reason`, 그리고 `GAME_OVER` 면 `history`(전체 라운드 숫자)·`final_scores`·`sets`.
- **종료 코드**: `0` = 내 차례/게임 종료, `1` = 오류(`code`·`message` 포함), `3` = 타임아웃(세션 유지 → `wait` 로 재개).
- **재접속 유예 60초**: 명령 사이에 접속이 끊기므로, 내 차례에 60초 넘게 아무 명령도 보내지 않으면 서버가 `disconnect` 부전승 처리합니다. 오래 고민할 때는 `status`/`wait` 로 다시 붙으면 유예가 취소됩니다.
- **`ROUND_RESULT`**: CLI 가 `next` 를 보내 다음 라운드를 앞당기고, 그래도 서버는 2.5초 뒤 자동 전환합니다.

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/` | 게임 화면(`static/index.html`) |
| `POST` | `/api/rooms` | 방 생성. `{"mode": "human"}` 또는 `{"mode": "ai", "difficulty": "easy"\|"normal"\|"hard"}` → `{room_id, mode, difficulty, ai_version, token, player:"A", invite_path, ws_path, log_mode}`. 난이도가 틀리면 400 `INVALID_DIFFICULTY` |
| `GET` | `/api/rooms/{code}` | 방 상태(존재/모드/난이도/좌석 수·AI 여부/연결 여부). 없으면 404 `ROOM_NOT_FOUND` |
| `GET` | `/api/health` | 엔진·AI 버전, 지원 난이도, 방 수, 저장된 게임 수 |
| `WS` | `/ws?room=CODE&token=...` | 게임 채널. 토큰이 없으면 빈 좌석 배정, 정원이 차면 `ROOM_FULL`(AI 방은 AI 가 B 좌석) |

### WebSocket 프로토콜

클라이언트 → 서버

| `type` | 본문 | 의미 |
| --- | --- | --- |
| `submit` | `set`, `round`, `tile` | 타일 제출(세트·라운드 Idempotency 검증) |
| `next` | — | `ROUND_RESULT` 결과 확인(양쪽 확인 시 즉시 다음 라운드) |
| `forfeit` | — | 기권 |
| `rematch` | — | 재대결 요청(양쪽 요청 시 새 판) |
| `leave` | — | 대기 중 방 나가기(게임 중이면 기권) |
| `ping` / `pong` | `t` | 15초 주기 heartbeat |
| `sync` | — | 현재 상태 재요청(탭 복귀 시) |

서버 → 클라이언트

| `type` | 의미 |
| --- | --- |
| `welcome` | 좌석·토큰·모드·현재 뷰(재접속 복구용) |
| `state` | **내 시점 뷰**(`game.view_for(me)`) + 방 메타(모드·난이도·`ai_thinking`·연결 상태·기권/종료·재대결 투표) |
| `ack` | 중복 제출 흡수(`duplicate: true`) |
| `pong` / `ping` | heartbeat 응답/서버 ping |
| `error` | `code`(`ROOM_FULL`·`ROOM_NOT_FOUND`·`INVALID_TOKEN`·`STALE_ROUND`·`NOT_YOUR_TURN`·`TILE_UNAVAILABLE`·`INVALID_TILE`·`WRONG_PHASE`·`BAD_MESSAGE`·`SERVER_ERROR`) + `message` |

## AI 플레이어 (`ai.py`)

서버는 AI 도 **사람과 똑같은 딜러 경로**로 다룹니다. AI 는 `game.view_for(ai_player)`
가 돌려준 관측 하나만 받아 수를 고르고, 서버가 그 수를 `engine.submit_tile()` 로 접수합니다.
따라서 AI 도 상대 숫자를 볼 수 없고(사람과 같은 정보), 로그·복기·검증 경로도 동일합니다.

| 난이도 | 방식 |
| --- | --- |
| `easy` | 합법 수 중 균등 무작위 |
| `normal` | 공개된 흑/백 색·남은 흑백 개수·점수 차·남은 타일을 반영한 **기댓값 휴리스틱**(이번 라운드 기대 승점 + 남은 라운드 쌍비교 기대값) |
| `hard` | **제약 기반 신념(Belief) + Expectimax/Minimax**. 색상 제약을 만족하는 상대 손패 후보를 전수 열거(최대 60조합, 필요하면 시드 샘플링)하고, 상태를 “내 패 + 상대 흑/백 개수”로 추상화해 세트 끝까지 전개한다(메모이제이션으로 밀리초). 내 차례는 최대, 상대 차례는 제약 분포에 대한 기대(`expect`) 또는 최악(`min`) |

교체 지점은 `BaseAI.select_action(observation)` 하나입니다. 강화학습/Self-Play 모델로
바꿀 때는 `BaseAI` 를 구현해 `create_ai()` 팩토리에 연결하면 되고, 엔진·서버·클라이언트는
그대로입니다(엔진의 `reset/legal_actions/step`, `view_for` 가 그대로 학습 인터페이스).

**서버 동작**

- AI 좌석(`Room.ai_player = "B"`, `Seat.is_ai=True`)은 사람이 붙을 수 없고(토큰 없음), 유예·하트비트 대상이 아니다.
- AI 계산은 `asyncio.to_thread(...)` 로 워커 스레드에서 돌려 이벤트 루프를 막지 않는다.
- 응답 전 최소 대기: 난이도별 `easy 0.6초 / normal 0.8초 / hard 1.0초`(`AI_THINK_DELAYS`),
  `ServerSettings(ai_think_delay=...)` 로 한 번에 덮어쓸 수 있다(테스트는 0~0.01초).
- AI 가 선공이면(첫 라운드 또는 직전 라운드 승리/무승부 유지) 입장 직후 자동으로 한 수를 내고
  `WAITING_SECOND` 로 넘긴다. 상태 변화는 `broadcast()` 한 곳에서 감지해 한 국면당 한 번만 예약한다.
- 재대결: AI 는 투표하지 않으므로 사람이 `rematch` 를 보내면 바로 새 판이 시작된다.
- 기권·연결 끊김은 사람 대전과 동일하게 `forfeit` / `disconnect` 로 기록된다.

## 정보 가시성 (치팅 방지의 핵심)

- 서버는 매 메시지마다 **좌석별로 따로** `game.view_for(player)` 를 만들어 보냅니다.
  상대 타일 숫자는 진행 중 어떤 키로도 직렬화되지 않습니다(상대는 `remaining_black/white` 개수와
  `submitted_color` 뿐). `full_state()` 는 저장·테스트용으로만 쓰고 클라이언트로 보내지 않습니다.
- `GAME_OVER` 가 된 뒤에만 `replay` 로 전체 라운드의 실제 숫자가 열립니다.
- 테스트 근거: `test_progressing_payloads_never_leak_opponent_numbers`(진행 중 페이로드 구조 검사),
  `test_full_game_ends_with_early_win_and_is_logged`(종료 후 복기 공개) 등.

## 게임 로그 (`storage.py`)

`LogRepository` 인터페이스만 호출하므로, 배포 때 영구 볼륨·Supabase/Neon PostgreSQL 구현으로
갈아끼우면 `server.py` 는 그대로입니다. 한 판이 끝나면 게임 1행 + 라운드 N행을 **한 트랜잭션**으로
저장합니다(기권·연결 끊김 판도 기록되어 RL 학습에서 걸러낼 수 있습니다).

`games`: `game_id`, `room_id`, `created_at`, `ended_at`, `engine_version`, `ai_version`(사람 대전 NULL, AI 대전 `AI_VERSION`),
`mode`(`human_vs_human` / `human_vs_ai_easy` / `human_vs_ai_normal` / `human_vs_ai_hard`),
`end_reason`(`normal`/`early_win`/`forfeit`/`disconnect`),
`winner`(`A`/`B`), `total_sets`, `p1_score`, `p2_score`

AI 대전에서는 모드에 난이도가 들어가고(`human_vs_ai_hard` 등) `ai_version` 이 함께 남습니다.
그래서 모델을 교체한 뒤 “어느 버전과의 판인지”를 그대로 비교할 수 있습니다(예: hard 의 person-to-AI 승률 변화).

`rounds`: `game_id`, `set_index`(본 게임 0, 연장전 1,2…), `round`, `first_player`,
`p1_tile`, `p2_tile`, `p1_color`, `p2_color`, `round_winner`, `p1_score`, `p2_score`,
`think_time_ms`(선공 고민 시간), `second_think_time_ms`(후공 고민 시간)

`p1`/`p2` 는 **좌석 A/B** 이고, 선공이 누구였는지는 `first_player` 로 구분합니다.

## 연결 유지·재접속·정리

| 항목 | 기본값 | 동작 |
| --- | --- | --- |
| 재접속 유예 | 60초 | 끊긴 좌석을 유지하고 상대에게 알림. 넘기면 **부전승(disconnect)** + 로그 |
| 결과 표시 | 2.5초 | `ROUND_RESULT` 를 보여 주고 자동 다음 라운드(양쪽 `next` 면 즉시) |
| Heartbeat | 18초 | 서버가 ping, 클라이언트는 15초마다 ping. 60초 무응답이면 연결 종료 |
| 방 TTL | 30분 | 접속자가 모두 끊긴 방/시작 안 된 방을 백그라운드 GC 로 정리(AI 좌석은 접속자로 치지 않음) |
| 방 코드 | 6자리 | `secrets` 기반 영숫자(헷갈리는 0/O/1/l/I 제외), 중복 회피 |

재접속은 `sessionStorage` 의 `bw_token_${roomId}` 토큰으로 **같은 좌석**에 다시 붙습니다.
`onclose` 시 1초 → 2초 → 4초 백오프로 자동 재연결하고, 재연결되면 `welcome` 에 실린 최신 뷰로 동기화합니다.

## 명세에 없어 이번 단계에서 정한 부분

1. **방 코드 형식** — 6자리(요구 6~8자리) 영숫자, 혼동 문자 제외.
2. **부전승 주체** — 기권·연결 끊김 시 남은 쪽이 승자, `end_reason` 은 각각 `forfeit` / `disconnect`,
   패자의 좌석은 `loser` 로 함께 전달.
3. **`ROUND_RESULT` 전환** — 양쪽 `next` 가 모이면 즉시, 아니면 2.5초 뒤 자동.
   (테스트에서는 `ServerSettings.round_result_seconds` 로 줄임)
4. **재대결** — 종료된 방에서 양쪽이 `rematch` 를 보내면 같은 좌석으로 새 판(`game_id` 새로 발급).
5. **`total_sets`** — 진행/완료된 세트 수(`set_index + 1`, 본 게임 포함).
6. **`think_time_ms` / `second_think_time_ms`** — 라운드 선공/후공이 각각 턴을 받아 제출하기까지의 ms.
7. **AI 모드(3단계)** — 2단계의 501 자리표시자를 걭어내고 실제 좌석으로 연결했습니다.
   - 저장 `mode` 는 2단계 로그 스키마를 이어 `human_vs_ai_easy` / `_normal` / `_hard` 로 쓰고,
     `ai_version` 에 `ai.py` 의 `AI_VERSION`("1.0.0")을 함께 남깁니다(난이도가 모드에, 모델 버전이 컬럼에).
   - AI 좌석은 항상 `B` 이고 `Seat.is_ai=True`·토큰 없음—— 사람이 붙을 수 없습니다.
   - 첫 수는 엔진 기본(코인 토스). 테스트/데모에서는 `ServerSettings(opening_player="A"|"B")` 로 고정할 수 있습니다.
   - `hard` 의 깊이/표본/모델은 모두 생성자 옵션(`lookahead`·`belief_mode`·`opponent_model`)으로 바꿀 수 있습니다.
   - 이 게임은 양쪽 타일 집합이 같아 **무작위 상대에 대한 기대 이득이 작은 편**입니다. 참고로 고정 시드
     300판 시뮬레이션에서 hard 는 easy 를 64%, normal 을 76% 이겼습니다(둘 다 동일 규칙·동일 정보).

## 테스트

```bash
pytest tests -q          # 140 passed (engine 47 + storage 12 + server 42 + ai 21 + cli 18)
```

핵심 검증 3가지

1. **합법성** — 세 난이도 모두 합법 수만 고르고 예외 없이 한 판을 완주한다(`test_ai.py`).
2. **정보 은닉** — 같은 `view_for` 관측 + 같은 시드면 실제 상대 패가 무엇이든 완전히 같은
   수를 고른다. 후공(상대 색만 공개) 상황에서도 같게 동작하며, JSON 으로 직렬화한 관측만으로도
   결과가 동일하다. 소스에 `full_state` / `._hands` 같은 딜러 전용 접근이 없다는 것도 검사한다.
3. **AI 대전 E2E** — 방 생성 → WS 입장(초대 없이 바로 시작) → AI 자동 응수 → 한 판 완주 →
   `storage` 로그에 `mode=human_vs_ai_<난이도>` · `ai_version` 기록 → 재대결·기권(`test_server.py`).

WebSocket 테스트가 멈춘 것처럼 보이면 `pip install pytest-timeout` 후
`pytest tests -q --timeout 30` 으로 원인 지점을 바로 확인할 수 있습니다.
