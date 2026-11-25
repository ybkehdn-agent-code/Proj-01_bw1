"""`engine.py` 전수 단위 테스트.

검증 범위
- 초기 세팅, 0~8 흑(짝수)/백(홀수) 매핑
- 선·후공 결정, 승자에 따른 다음 라운드 선공 이동, 무승부 시 선공 유지
- 9라운드 정상 종료, 조기 종료(수학적 경계 포함), 동점 시 연장전(set_index)
- 상태 머신 전이와 잘못된 set_index/round/중복 제출/비턴 제출 예외
- 정보 은닉(상대 숫자 0건 유출) 및 GAME_OVER 복기 공개
- RL 표준 인터페이스(reset / legal_actions / step)와 무작위 Self-Play 불변식
"""

import json
import random

import pytest

import engine
from engine import (
    BLACK_TILES,
    COLOR_BLACK,
    COLOR_WHITE,
    DRAW,
    ENGINE_VERSION,
    PLAYER_A as A,
    PLAYER_B as B,
    PLAYERS,
    ROUNDS_PER_SET,
    TILES,
    WHITE_TILES,
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

# ---------------------------------------------------------------------------
# 고정 시나리오(양쪽 모두 0~8 타일을 한 번씩 쓰는 계획)
# ---------------------------------------------------------------------------
# A 5승 : B 4승 → 9라운드를 모두 채워 "normal" 종료.
# 선공: A, A, B, A, B, A, B, A, B 로 번갈아 이동한다.
PLAN_NINE_ROUNDS = [
    (8, 7),
    (0, 1),
    (7, 6),
    (1, 2),
    (6, 5),
    (2, 3),
    (5, 4),
    (3, 8),
    (4, 0),
]

# 6라운드 종료 시점 4:1, |차|3 == 남은 라운드 3 → 아직 끝나지 않는 경계 시나리오.
# 7라운드에서 A 가 이겨 5:1, |차|4 > 남은 2 → 조기 종료.
PLAN_BOUNDARY_6 = [(8, 7), (7, 6), (1, 2), (6, 5), (4, 4), (5, 3)]
PLAN_BOUNDARY_7 = PLAN_BOUNDARY_6 + [(3, 0)]

# 5라운드 만에 5:0 (|차|5 > 남은 4) → 조기 종료.
PLAN_EARLY_5_0 = [(4, 0), (5, 1), (6, 2), (7, 3), (8, 4)]

# 7라운드 종료 시점 5:2 (|차|3 > 남은 2) → 방송 11화 사례와 같은 조기 종료.
PLAN_EARLY_5_2 = [(8, 7), (0, 1), (7, 6), (1, 2), (6, 4), (5, 3), (4, 0)]

# 9라운드 4:4 동점(5라운드 무승부) → 연장전 진입.
PLAN_TIE_9 = [(8, 7), (0, 1), (7, 6), (1, 2), (4, 4), (6, 5), (2, 3), (5, 0), (3, 8)]


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def play_round(game: BlackWhiteGame, a_tile: int, b_tile: int) -> None:
    """현재 선공 순서에 맞춰 한 라운드를 끝까지 제출한다(서버처럼 set/round 동봉)."""

    set_index, round_number = game.set_index, game.round
    if game.first_player == A:
        game.submit_tile(A, a_tile, set_index=set_index, round_number=round_number)
        game.submit_tile(B, b_tile, set_index=set_index, round_number=round_number)
    else:
        game.submit_tile(B, b_tile, set_index=set_index, round_number=round_number)
        game.submit_tile(A, a_tile, set_index=set_index, round_number=round_number)


def play_rounds(game: BlackWhiteGame, plan, advance_last: bool = True) -> None:
    """계획대로 여러 라운드를 진행한다. 마지막 라운드의 ROUND_RESULT 는 선택적으로 남긴다."""

    for index, (a_tile, b_tile) in enumerate(plan):
        play_round(game, a_tile, b_tile)
        if index < len(plan) - 1 or advance_last:
            game.advance_round()


def int_lists(node):
    """dict/list 안의 '정수 리스트'를 모두 찾아낸다(정보 누수 검사용)."""

    if isinstance(node, dict):
        for value in node.values():
            yield from int_lists(value)
    elif isinstance(node, list):
        if node and all(isinstance(item, int) and not isinstance(item, bool) for item in node):
            yield list(node)
        else:
            for value in node:
                yield from int_lists(value)


def hidden_numbers_for(game: BlackWhiteGame, viewer: str):
    """진행 중 `viewer` 에게 숨겨져 있어야 하는 상대의 모든 실제 숫자."""

    opponent = B if viewer == A else A
    state = game.full_state()
    hidden = set(state["hands"][opponent])
    if state["submitted"].get(opponent) is not None:
        hidden.add(state["submitted"][opponent])
    for record in state["rounds"]:
        hidden.add(record["second_tile"] if record["first"] == opponent else record["first_tile"])
    return hidden


def permute_hand_same_color(game: BlackWhiteGame, player: str) -> bool:
    """상대 손패를 같은 색(홀짝) 안에서 맞바꾼다.

    공개 정보(남은 흑/백 개수)는 그대로이고 실제 숫자만 달라지므로,
    이 상태에서 만든 view 가 이전과 같다면 숫자가 새어 나가지 않은 것이다.
    (정보 은닉 검증을 위한 화이트박스 조작)
    """

    hand = sorted(game._hands[player])
    evens = [tile for tile in hand if tile % 2 == 0]
    odds = [tile for tile in hand if tile % 2 == 1]
    if len(evens) >= 2:
        evens[0], evens[1] = evens[1], evens[0]
    elif len(odds) >= 2:
        odds[0], odds[1] = odds[1], odds[0]
    else:
        return False
    game._hands[player] = sorted(evens + odds)
    return True


# ---------------------------------------------------------------------------
# 1. 초기 세팅 · 타일 색상 매핑 · 선공 결정
# ---------------------------------------------------------------------------
def test_initial_setup_hands_scores_and_phase():
    game = BlackWhiteGame(first_player=A)

    assert game.phase is Phase.WAITING_FIRST
    assert game.set_index == 0
    assert game.round == 1
    assert game.scores == {A: 0, B: 0}
    assert game.rounds_played == 0
    assert game.first_player == A
    assert game.second_player == B
    assert game.current_player == A
    assert game.winner is None and game.end_reason is None
    assert game.is_over is False

    for player in PLAYERS:
        assert game.remaining_tiles(player) == list(TILES)

    view = game.view_for(A)
    assert view["you"]["remaining"] == list(TILES)
    assert view["opponent"]["remaining_black"] == 5
    assert view["opponent"]["remaining_white"] == 4


def test_tile_color_mapping_even_black_odd_white():
    assert BLACK_TILES == (0, 2, 4, 6, 8)
    assert WHITE_TILES == (1, 3, 5, 7)
    assert len(BLACK_TILES) == 5 and len(WHITE_TILES) == 4

    for tile in TILES:
        assert tile_color(tile) == (COLOR_BLACK if tile % 2 == 0 else COLOR_WHITE)

    with pytest.raises(InvalidTileError):
        tile_color(9)
    with pytest.raises(InvalidTileError):
        tile_color(-1)


def test_first_player_can_be_fixed_or_coin_tossed():
    assert BlackWhiteGame(first_player=B).first_player == B
    assert BlackWhiteGame(first_player=B).second_player == A

    # 같은 시드 → 같은 결과(재현 가능), 여러 시드 → 양쪽 모두 등장(무작위)
    seeded = {BlackWhiteGame(rng=random.Random(11)).first_player for _ in range(3)}
    assert len(seeded) == 1
    tosses = {BlackWhiteGame(rng=random.Random(seed)).first_player for seed in range(30)}
    assert tosses == {A, B}


def test_engine_version_is_exposed():
    assert engine.engine_version == ENGINE_VERSION
    assert ENGINE_VERSION.count(".") == 2
    assert BlackWhiteGame().view_for(A)["engine_version"] == ENGINE_VERSION


def test_unknown_player_is_rejected():
    game = BlackWhiteGame(first_player=A)
    with pytest.raises(UnknownPlayerError):
        game.submit_tile("C", 3)
    with pytest.raises(UnknownPlayerError):
        game.view_for("C")
    with pytest.raises(UnknownPlayerError):
        game.legal_actions("C")
    with pytest.raises(UnknownPlayerError):
        BlackWhiteGame(first_player="C")


# ---------------------------------------------------------------------------
# 2. 라운드 진행 · 승패 판정 · 선공 이동
# ---------------------------------------------------------------------------
def test_state_machine_waiting_first_to_waiting_second():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)

    assert game.phase is Phase.WAITING_SECOND
    assert game.current_player == B
    assert game.remaining_tiles(A) == [0, 1, 2, 3, 4, 5, 6, 7]
    # 선공이 낸 타일의 '색'은 즉시 공개된다.
    assert game.view_for(B)["opponent"]["submitted_color"] == COLOR_BLACK


def test_round_resolution_and_round_result_phase():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)
    result = game.submit_tile(B, 3)

    assert game.phase is Phase.ROUND_RESULT
    assert game.current_player is None
    assert result["round_completed"] is True
    assert result["round_winner"] == A
    assert result["scores"] == {A: 1, B: 0}

    view = game.view_for(B)
    assert view["round_result"]["winner"] == A
    assert view["round_result"]["scores"] == {A: 1, B: 0}
    assert view["round_result"]["first_color"] == COLOR_BLACK
    assert view["round_result"]["second_color"] == COLOR_WHITE
    assert len(view["rounds"]) == 1
    assert view["rounds"][0]["winner"] == A


def test_higher_number_wins_regardless_of_turn_order():
    # 선공 A 가 낮은 수 → 후공 B 승
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 1)
    game.submit_tile(B, 2)
    assert game.scores == {A: 0, B: 1}
    game.advance_round()
    assert game.first_player == B  # 승자가 다음 라운드 선공

    # 후공이 낮은 수 → 선공 승
    game2 = BlackWhiteGame(first_player=A)
    game2.submit_tile(A, 5)
    game2.submit_tile(B, 4)
    assert game2.scores == {A: 1, B: 0}


def test_no_zero_beats_eight_exception():
    # 0 이 8 을 이기는 예외는 없다: 순수 숫자 대소 비교만 한다.
    first = BlackWhiteGame(first_player=A)
    first.submit_tile(A, 0)
    first.submit_tile(B, 8)
    assert first.scores == {A: 0, B: 1}

    second = BlackWhiteGame(first_player=B)
    second.submit_tile(B, 8)
    second.submit_tile(A, 0)
    assert second.scores == {A: 0, B: 1}


def test_draw_gives_no_point_and_keeps_first_player():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 4)
    game.submit_tile(B, 4)

    view = game.view_for(A)
    assert view["round_result"]["winner"] == DRAW
    assert game.scores == {A: 0, B: 0}

    game.advance_round()
    assert game.first_player == A  # 무승부 → 선공 유지
    assert game.round == 2
    assert game.phase is Phase.WAITING_FIRST


def test_used_tile_is_permanently_gone():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)
    game.submit_tile(B, 7)
    game.advance_round()

    assert 8 not in game.remaining_tiles(A)
    assert 7 not in game.remaining_tiles(B)
    assert game.first_player == A  # 8 > 7 → A 승, A 선공 유지

    with pytest.raises(TileAlreadyUsedError):
        game.submit_tile(A, 8)

    game.submit_tile(A, 0)
    assert game.current_player == B
    with pytest.raises(TileAlreadyUsedError):
        game.submit_tile(B, 7)


def test_first_player_rotation_follows_round_winners():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS)

    view = game.view_for(A)
    assert [entry["first_player"] for entry in view["rounds"]] == [
        A, A, B, A, B, A, B, A, B
    ]
    assert [entry["winner"] for entry in view["rounds"]] == [
        A, B, A, B, A, B, A, B, A
    ]


# ---------------------------------------------------------------------------
# 3. 잘못된 턴 · 중복 제출 · set/round 검증
# ---------------------------------------------------------------------------
def test_non_turn_player_cannot_submit():
    game = BlackWhiteGame(first_player=A)
    with pytest.raises(NotYourTurnError):
        game.submit_tile(B, 5)  # 선공 차례에 후공이 먼저 낼 수 없다
    assert game.remaining_tiles(B) == list(TILES)  # 실패한 제출은 상태를 바꾸지 않는다

    game.submit_tile(A, 5)
    assert game.phase is Phase.WAITING_SECOND
    with pytest.raises(DuplicateSubmissionError):
        game.submit_tile(A, 6)  # 이미 낸 라운드에 다른 타일로 재제출


def test_same_tile_twice_is_treated_as_duplicate():
    game = BlackWhiteGame(first_player=A)
    first = game.submit_tile(A, 5)
    again = game.submit_tile(A, 5)

    assert first["duplicate"] is False
    assert again["duplicate"] is True
    assert game.phase is Phase.WAITING_SECOND
    assert game.remaining_tiles(A) == [0, 1, 2, 3, 4, 6, 7, 8]  # 한 번만 소모


def test_different_tile_in_same_round_is_rejected():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 5)
    with pytest.raises(DuplicateSubmissionError):
        game.submit_tile(A, 6)


def test_duplicate_after_round_resolved_is_ignored():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)
    game.submit_tile(B, 3)
    assert game.phase is Phase.ROUND_RESULT

    replay = game.submit_tile(B, 3, set_index=0, round_number=1)
    assert replay["duplicate"] is True
    assert game.scores == {A: 1, B: 0}
    assert game.phase is Phase.ROUND_RESULT

    with pytest.raises(DuplicateSubmissionError):
        game.submit_tile(B, 4, set_index=0, round_number=1)


def test_stale_set_index_is_rejected():
    game = BlackWhiteGame(first_player=A)
    with pytest.raises(StaleRequestError):
        game.submit_tile(A, 5, set_index=1, round_number=1)


def test_stale_round_number_is_rejected():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 5)
    game.submit_tile(B, 3)
    game.advance_round()

    assert game.first_player == A and game.current_player == A
    with pytest.raises(StaleRequestError):
        game.submit_tile(A, 6, round_number=5)
    with pytest.raises(StaleRequestError):
        game.submit_tile(A, 6, round_number=0)


def test_stale_request_for_previous_round_with_different_tile():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 5)
    game.submit_tile(B, 3)
    game.advance_round()

    with pytest.raises(StaleRequestError):
        game.submit_tile(A, 4, set_index=0, round_number=1)


def test_invalid_tile_values_are_rejected():
    game = BlackWhiteGame(first_player=A)
    for bad in (-1, 9, 1.5, "3", True, None, 3.0):
        with pytest.raises(InvalidTileError):
            game.submit_tile(A, bad)


def test_submit_after_game_over_is_rejected():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_EARLY_5_0, advance_last=False)
    game.advance_round()
    assert game.is_over

    with pytest.raises(WrongPhaseError):
        game.submit_tile(A, 0)
    with pytest.raises(WrongPhaseError):
        game.advance_round()


def test_advance_round_requires_round_result_phase():
    game = BlackWhiteGame(first_player=A)
    with pytest.raises(WrongPhaseError):
        game.advance_round()

    game.submit_tile(A, 5)
    with pytest.raises(WrongPhaseError):
        game.confirm_round_result()


# ---------------------------------------------------------------------------
# 4. 9라운드 종료 · 조기 종료 · 연장전
# ---------------------------------------------------------------------------
def test_full_nine_rounds_ends_normally():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS)

    assert game.is_over
    assert game.phase is Phase.GAME_OVER
    assert game.scores == {A: 5, B: 4}
    assert game.winner == A
    assert game.end_reason == "normal"
    assert game.rounds_played == 9
    assert game.set_index == 0

    view = game.view_for(A)
    assert view["decided"] is True
    assert view["game_over"]["winner"] == A
    assert view["game_over"]["loser"] == B
    assert view["game_over"]["end_reason"] == "normal"
    assert view["game_over"]["total_sets"] == 1


def test_early_win_when_lead_exceeds_remaining_rounds():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_EARLY_5_0, advance_last=False)

    # 5라운드 종료 시점 5:0 → 남은 4라운드를 모두 이겨도 5:4, 역전 불가.
    view = game.view_for(A)
    assert view["round_result"]["decided"] is True
    assert view["round_result"]["next"] == "game_over"
    assert view["round_result"]["end_reason"] == "early_win"
    assert view["round_result"]["set_winner"] == A

    game.advance_round()
    assert game.is_over
    assert game.end_reason == "early_win"
    assert game.winner == A
    assert game.rounds_played == 5  # 9라운드를 채우지 않는다
    assert game.scores == {A: 5, B: 0}


def test_early_win_broadcast_case_five_two_after_seven_rounds():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_EARLY_5_2, advance_last=False)

    assert game.scores == {A: 5, B: 2}
    assert game.view_for(A)["round_result"]["end_reason"] == "early_win"
    game.advance_round()
    assert game.is_over and game.winner == A and game.rounds_played == 7


def test_lead_equal_to_remaining_rounds_does_not_end_game():
    """|차| == 남은 라운드면 상대가 전승해 '동점'이 가능하므로 끝내지 않는다(경계값)."""

    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_BOUNDARY_6, advance_last=False)

    assert game.scores == {A: 4, B: 1}  # |차| 3, 남은 라운드 3
    view = game.view_for(A)
    assert game.phase is Phase.ROUND_RESULT
    assert view["round_result"]["decided"] is False
    assert view["round_result"]["next"] == "next_round"

    game.advance_round()
    assert game.phase is Phase.WAITING_FIRST
    assert game.round == 7
    assert game.is_over is False

    # 한 라운드 더 이기면 |차| 4 > 남은 2 → 조기 종료
    play_round(game, 3, 0)
    assert game.view_for(A)["round_result"]["decided"] is True
    game.advance_round()
    assert game.is_over
    assert game.winner == A
    assert game.end_reason == "early_win"
    assert game.scores == {A: 5, B: 1}


def test_nine_round_draw_starts_overtime_with_fresh_set():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_TIE_9)

    # 9라운드 4:4 → 연장전 세트 1이 시작된다(게임은 끝나지 않음).
    assert game.is_over is False
    assert game.phase is Phase.WAITING_FIRST
    assert game.set_index == 1
    assert game.round == 1
    assert game.scores == {A: 0, B: 0}
    for player in PLAYERS:
        assert game.remaining_tiles(player) == list(TILES)  # 0~8 새로 지급
    assert game.view_for(A)["completed_sets"] == [
        {"set_index": 0, "scores": {A: 4, B: 4}, "winner": None, "end_reason": "drawn"}
    ]


def test_overtime_first_player_is_previous_set_last_round_winner():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_TIE_9)

    # 마지막 9라운드는 B 승 → 연장전 1라운드 선공은 B
    last = game.full_state()["rounds"][-1]
    assert last["winner"] == B
    assert game.first_player == B
    assert game.current_player == B


def test_overtime_keeps_previous_set_history():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_TIE_9)
    first_set_rounds = [r for r in game.full_state()["rounds"] if r["set_index"] == 0]
    assert len(first_set_rounds) == 9

    # 연장전에서 5:0 조기 종료 → 최종 승자 A, 복기에 두 세트 모두 남는다.
    play_rounds(game, PLAN_EARLY_5_0)

    assert game.is_over
    assert game.set_index == 1
    assert game.end_reason == "early_win"
    assert game.winner == A

    state = game.full_state()
    assert len([r for r in state["rounds"] if r["set_index"] == 0]) == 9
    assert len([r for r in state["rounds"] if r["set_index"] == 1]) == 5
    assert [s["end_reason"] for s in state["sets"]] == ["drawn", "early_win"]

    view = game.view_for(B)
    assert view["set_index"] == 1
    assert view["game_over"]["total_sets"] == 2
    assert [entry["set_index"] for entry in view["game_over"]["sets"]] == [0, 1]
    assert len(view["replay"]) == 14


# ---------------------------------------------------------------------------
# 5. 정보 은닉(보안) 테스트
# ---------------------------------------------------------------------------
FORBIDDEN_KEYS = (
    "opponent_tiles",
    "opponent_hand",
    "hands",
    "remaining_tiles",
    "first_tile",
    "second_tile",
    "tile_first",
    "tile_second",
)


@pytest.mark.parametrize("viewer", [A, B])
def test_in_progress_view_exposes_only_own_numbers(viewer):
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS[:5])  # 5라운드까지 진행 → 6라운드 선공 대기

    view = game.view_for(viewer)
    assert view["phase"] == "WAITING_FIRST"

    payload = json.dumps(view, sort_keys=True, ensure_ascii=False)
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in payload

    # 상대 섹션에는 '개수'만 있고 숫자 배열 키가 없다.
    assert set(view["opponent"]) == {
        "id",
        "remaining_black",
        "remaining_white",
        "submitted_color",
    }
    assert "remaining" not in view["opponent"]

    opponent = B if viewer == A else A
    remaining = game.remaining_tiles(opponent)
    assert view["opponent"]["remaining_black"] + view["opponent"]["remaining_white"] == len(
        remaining
    )

    # 뷰 안의 '정수 리스트'는 오직 본인 남은 패 하나뿐이다.
    lists = list(int_lists(view))
    assert lists == [game.remaining_tiles(viewer)]

    # 완료 라운드 기록에도 상대 숫자는 없다(색상만).
    for entry in view["rounds"]:
        assert entry["opponent_color"] in (COLOR_BLACK, COLOR_WHITE)
        assert "first_tile" not in entry and "second_tile" not in entry
        assert isinstance(entry["your_tile"], int)
    assert view["replay"] is None


@pytest.mark.parametrize("viewer", [A, B])
def test_waiting_second_view_hides_first_player_number(viewer):
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)

    view = game.view_for(viewer)
    assert view["phase"] == "WAITING_SECOND"
    assert view["turn"] == B

    payload = json.dumps(view, sort_keys=True)
    assert "opponent_tiles" not in payload
    # 뷰 안의 정수 리스트는 본인 남은 패 하나뿐이다.
    assert list(int_lists(view)) == [game.remaining_tiles(viewer)]

    if viewer == A:
        # 선공 본인 시점: 자기 숫자는 보이고, 상대는 아직 내지 않았다.
        assert view["you"]["submitted"] == 8
        assert view["opponent"]["submitted_color"] is None
    else:
        # 후공 시점: 선공의 숫자는 어느 필드에도 없고 '색상'만 보인다.
        assert view["you"]["submitted"] is None
        assert view["you"]["remaining"] == list(TILES)
        assert view["opponent"]["submitted_color"] == COLOR_BLACK
        assert set(view["opponent"]) == {
            "id",
            "remaining_black",
            "remaining_white",
            "submitted_color",
        }


@pytest.mark.parametrize("viewer", [A, B])
def test_round_result_view_still_hides_opponent_numbers(viewer):
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS[:5], advance_last=False)

    view = game.view_for(viewer)
    assert view["phase"] == "ROUND_RESULT"
    assert set(view["opponent"]) == {
        "id",
        "remaining_black",
        "remaining_white",
        "submitted_color",
    }
    payload = json.dumps(view, sort_keys=True)
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in payload
    # 이번 라운드 결과조차 '색상'만 공개한다.
    assert view["round_result"]["first_color"] in (COLOR_BLACK, COLOR_WHITE)
    assert list(int_lists(view)) == [game.remaining_tiles(viewer)]


def test_view_is_invariant_under_opponent_hand_permutation():
    """상대 손패의 실제 숫자만 바꿔도(같은 색 유지) 뷰가 완전히 같아야 한다."""

    for rounds in (0, 2, 5):
        game = BlackWhiteGame(first_player=A)
        if rounds:
            play_rounds(game, PLAN_NINE_ROUNDS[:rounds])

        before = json.dumps(game.view_for(A), sort_keys=True, ensure_ascii=False)
        assert permute_hand_same_color(game, B) is True
        after = json.dumps(game.view_for(A), sort_keys=True, ensure_ascii=False)
        assert before == after


def test_view_is_invariant_under_opponent_submitted_number_permutation():
    game = BlackWhiteGame(first_player=A)
    game.submit_tile(A, 8)

    before = json.dumps(game.view_for(B), sort_keys=True)
    assert game._submitted[A] == 8
    game._submitted[A] = 6  # 같은 색(흑)의 다른 숫자
    after = json.dumps(game.view_for(B), sort_keys=True)

    assert before == after
    assert game.view_for(B)["opponent"]["submitted_color"] == COLOR_BLACK


def test_round_result_view_invariant_under_opponent_hand_permutation():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS[:5], advance_last=False)
    assert game.phase is Phase.ROUND_RESULT

    before = json.dumps(game.view_for(A), sort_keys=True, ensure_ascii=False)
    assert permute_hand_same_color(game, B) is True
    after = json.dumps(game.view_for(A), sort_keys=True, ensure_ascii=False)
    assert before == after


def test_hidden_numbers_never_appear_in_any_in_progress_view():
    game = BlackWhiteGame(first_player=A)
    plan = PLAN_NINE_ROUNDS

    for index, (a_tile, b_tile) in enumerate(plan):
        for viewer in PLAYERS:
            view = game.view_for(viewer)
            hidden = hidden_numbers_for(game, viewer)
            for lst in int_lists(view):
                shown = set(lst)
                # 본인 남은 패 외의 정수 리스트에는 숨겨야 할 숫자가 있으면 안 된다.
                if lst != game.remaining_tiles(viewer):
                    assert not (shown & hidden), f"유출: {lst} ∩ {hidden}"
        play_round(game, a_tile, b_tile)
        if index < len(plan) - 1:
            game.advance_round()


@pytest.mark.parametrize("viewer", [A, B])
def test_game_over_reveals_full_replay(viewer):
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS)
    assert game.phase is Phase.GAME_OVER

    view = game.view_for(viewer)
    state = game.full_state()

    assert view["replay"] is not None
    assert len(view["replay"]) == 9 == len(state["rounds"])

    for public, private in zip(view["replay"], state["rounds"]):
        assert public["first_tile"] == private["first_tile"]
        assert public["second_tile"] == private["second_tile"]
        assert public["set_index"] == private["set_index"]
        assert public["round"] == private["round"]
        assert public["winner"] == private["winner"]
        assert public["scores"] == private["scores_after"]

    # 1라운드: 선공 A 가 8, 후공 B 가 7
    assert view["replay"][0]["first_tile"] == 8
    assert view["replay"][0]["second_tile"] == 7

    # 종료 후에는 숫자가 실제로 공개된다.
    payload = json.dumps(view, sort_keys=True)
    assert "first_tile" in payload and "second_tile" in payload
    assert view["game_over"]["final_hands"] == {A: [], B: []}


# ---------------------------------------------------------------------------
# 6. RL 표준 인터페이스
# ---------------------------------------------------------------------------
def test_legal_actions_only_for_player_on_turn():
    game = BlackWhiteGame(first_player=A)
    assert game.legal_actions(A) == list(TILES)
    assert game.legal_actions(B) == []

    game.submit_tile(A, 4)
    assert game.legal_actions(A) == []
    assert game.legal_actions(B) == list(TILES)  # A 만 4 를 냈다

    game.submit_tile(B, 2)
    assert game.legal_actions(A) == []  # ROUND_RESULT 에서는 아무도 낼 수 없다
    assert game.legal_actions(B) == []

    game.advance_round()
    assert game.legal_actions(A) == [0, 1, 2, 3, 5, 6, 7, 8]  # A 승 → A 선공 유지


def test_step_returns_view_reward_and_done():
    game = BlackWhiteGame(first_player=A)

    first = game.step(A, 8)
    assert set(first) == {"view", "reward", "done", "info", "turn", "legal_actions"}
    assert first["reward"] == 0.0  # 라운드가 아직 안 끝났다
    assert first["done"] is False
    assert first["view"]["phase"] == "WAITING_SECOND"
    assert first["turn"] == B

    second = game.step(B, 3)
    assert second["reward"] == -1.0  # B 가 라운드를 졌다
    assert second["done"] is False
    assert second["view"]["phase"] == "ROUND_RESULT"
    assert second["turn"] is None


def test_step_reward_for_win_and_draw():
    # 승리 리워드
    winner_game = BlackWhiteGame(first_player=A)
    winner_game.step(A, 1)
    result = winner_game.step(B, 8)
    assert result["info"]["round_winner"] == B
    assert result["reward"] == 1.0

    # 무승부 리워드
    draw_game = BlackWhiteGame(first_player=A)
    draw_game.step(A, 4)
    result = draw_game.step(B, 4)
    assert result["info"]["round_winner"] == DRAW
    assert result["reward"] == 0.0


def test_step_refuses_out_of_turn_and_after_game_over():
    game = BlackWhiteGame(first_player=A)
    with pytest.raises(NotYourTurnError):
        game.step(B, 3)

    played = BlackWhiteGame(first_player=A)
    play_rounds(played, PLAN_EARLY_5_0, advance_last=False)
    played.advance_round()
    assert played.is_over
    with pytest.raises(WrongPhaseError):
        played.step(A, 0)


def test_reset_restores_initial_state():
    game = BlackWhiteGame(first_player=A)
    play_rounds(game, PLAN_NINE_ROUNDS[:4])
    assert game.rounds_played == 4

    game.reset(first_player=B)
    assert game.phase is Phase.WAITING_FIRST
    assert game.set_index == 0 and game.round == 1
    assert game.scores == {A: 0, B: 0}
    assert game.rounds_played == 0
    assert game.first_player == B
    assert game.winner is None and game.end_reason is None
    for player in PLAYERS:
        assert game.remaining_tiles(player) == list(TILES)
    assert game.full_state()["rounds"] == []


def test_self_play_loop_terminates_and_stays_consistent():
    """무작위 Self-Play 로 전체 게임을 돌려 규칙·정보 불변식을 검사한다."""

    rng = random.Random(20261125)
    for _ in range(30):
        game = BlackWhiteGame(rng=random.Random(rng.randrange(1_000_000)))
        turns = 0
        while not game.is_over:
            if game.phase is Phase.ROUND_RESULT:
                game.advance_round()
                continue

            player = game.current_player
            actions = game.legal_actions(player)
            assert actions, "제출 차례인데 낼 타일이 없다"
            assert actions == game.view_for(player)["you"]["remaining"]

            tile = rng.choice(actions)
            result = game.step(player, tile)
            turns += 1
            assert turns < 500
            # 매 관측은 JSON 직렬화 가능하고 상대 숫자를 담지 않는다.
            json.dumps(result["view"], sort_keys=True)

        state = game.full_state()
        assert game.winner in PLAYERS
        assert game.end_reason in {"normal", "early_win"}
        loser = B if game.winner == A else A
        assert game.scores[game.winner] > game.scores[loser]
        assert state["rounds"][-1]["set_index"] == game.set_index
        assert len(state["rounds"]) <= 9 * max(1, game.set_index + 1)

        # 라운드 레코드 자체의 정합성
        for record in state["rounds"]:
            if record["first_tile"] > record["second_tile"]:
                expected = record["first"]
            elif record["first_tile"] < record["second_tile"]:
                expected = record["second"]
            else:
                expected = DRAW
            assert record["winner"] == expected

        # 세트별 사용 타일은 서로 겹치지 않는다(한 세트에 0~8 한 번씩).
        for set_index in {record["set_index"] for record in state["rounds"]}:
            used_a, used_b = [], []
            for record in state["rounds"]:
                if record["set_index"] != set_index:
                    continue
                used_a.append(record["first_tile"] if record["first"] == A else record["second_tile"])
                used_b.append(record["first_tile"] if record["first"] == B else record["second_tile"])
            assert len(used_a) == len(set(used_a))
            assert len(used_b) == len(set(used_b))
