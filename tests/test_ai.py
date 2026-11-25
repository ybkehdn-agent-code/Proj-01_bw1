"""`ai.py` 테스트 — 난이도별 합법성·정보 은닉·신념 추정·시뮬레이션 승률.

핵심 검증
---------
1. 쉬움/보통/어려움 모두 **합법 수만** 고르고 예외 없이 한 판을 완주한다.
2. 어려움 AI 는 ``game.view_for()`` 관측만 사용한다 → **같은 관측 + 같은 시드**면
   실제 상대의 숨은 타일이 무엇이든 완전히 같은 수를 고른다(정보 누수 없음).
3. 어려움 AI 가 쉬움 AI 보다 유의미하게 높은 승률을 기록한다(고정 시드 시뮬레이션).
"""

from __future__ import annotations

import ast
import inspect
import json
import random
from typing import Dict, List, Optional, Tuple

import pytest

import ai as ai_module
from ai import (
    AI_VERSION,
    HARD,
    EASY,
    NORMAL,
    AIError,
    BaseAI,
    HeuristicAI,
    Observation,
    RandomAI,
    SearchAI,
    UnknownDifficultyError,
    count_belief_hands,
    create_ai,
    legal_actions_from_view,
    opponent_marginal,
    sample_opponent_hand,
)
from engine import BLACK_TILES, WHITE_TILES, BlackWhiteGame, Phase


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def play_game(
    ai_a: BaseAI,
    ai_b: BaseAI,
    *,
    seed: int = 0,
    first_player: str = "A",
    max_sets: int = 8,
    max_steps: int = 400,
) -> Tuple[Optional[str], BlackWhiteGame]:
    """두 AI 로 한 판을 끝까지(연장전 포함) 진행한다. 매 수 합법성을 단언한다."""

    game = BlackWhiteGame(first_player=first_player, rng=random.Random(seed))
    steps = 0
    while not game.is_over:
        if game.phase is Phase.ROUND_RESULT:
            game.advance_round()
            continue
        if game.set_index >= max_sets or steps > max_steps:  # 무한 연장전 방지
            return None, game
        player = game.turn
        assert player in ("A", "B")
        bot = ai_a if player == "A" else ai_b
        view = game.view_for(player)
        tile = bot.select_action(view)
        legal = game.legal_actions(player)
        assert tile in legal, f"{bot.name} 가 합법 수가 아닌 {tile} 를 골랐습니다(legal={legal})"
        game.step(player, tile)
        steps += 1
    return game.winner, game


def hidden_hand_game(
    hidden: Tuple[int, ...],
    *,
    first_player: str = "A",
    played: Optional[int] = None,
) -> BlackWhiteGame:
    """상대(B)의 숨은 패를 테스트에서만 직접 지정한 게임을 만든다.

    ``played`` 를 주면 B 가 그 타일을 먼저 낸 상태(``WAITING_SECOND``)로 만든다.
    """

    game = BlackWhiteGame(first_player=first_player, rng=random.Random(0))
    game._hands["B"] = list(hidden)  # noqa: SLF001 - 딜러 내부를 아는 것은 테스트뿐
    if played is not None:
        game.submit_tile("B", played)
    return game


# ---------------------------------------------------------------------------
# 버전 / 팩토리 / 관측
# ---------------------------------------------------------------------------
def test_ai_version_is_exposed() -> None:
    assert ai_module.AI_VERSION == "1.0.0"
    assert ai_module.ai_version == AI_VERSION
    assert ai_module.DIFFICULTIES == (EASY, NORMAL, HARD)


def test_create_ai_returns_expected_implementations() -> None:
    assert isinstance(create_ai("easy"), RandomAI)
    assert isinstance(create_ai("normal"), HeuristicAI)
    assert isinstance(create_ai("hard"), SearchAI)
    # 기본값은 보통 난이도
    assert isinstance(create_ai(), HeuristicAI)
    assert create_ai("hard").info()["ai_version"] == AI_VERSION


def test_create_ai_accepts_aliases_and_rejects_unknown() -> None:
    assert isinstance(create_ai("쉬움"), RandomAI)
    assert isinstance(create_ai("보통"), HeuristicAI)
    assert isinstance(create_ai("어려움"), SearchAI)
    assert isinstance(create_ai("MEDIUM"), HeuristicAI)  # 2단계 표기
    with pytest.raises(UnknownDifficultyError):
        create_ai("impossible")
    with pytest.raises(AIError):
        SearchAI(opponent_model="random")
    with pytest.raises(AIError):
        SearchAI(belief_mode="magic")


def test_observation_uses_only_public_fields() -> None:
    game = BlackWhiteGame(first_player="A", rng=random.Random(3))
    view = game.view_for("A")
    obs = Observation.from_view(view)
    assert obs.viewer == "A"
    assert obs.own_hand == tuple(range(9))
    assert obs.opp_remaining_black == 5 and obs.opp_remaining_white == 4
    assert obs.i_am_first is True
    assert obs.legal_actions == list(range(9))
    assert obs.rounds_left_in_set == 9
    # 관측 dict 를 손봐도 원본 뷰는 그대로(복사본만 쓴다)
    payload = json.dumps(view, ensure_ascii=False)
    assert json.loads(payload)["you"]["remaining"] == list(range(9))


def test_ai_refuses_action_out_of_turn() -> None:
    game = BlackWhiteGame(first_player="A", rng=random.Random(1))
    game.submit_tile("A", 3)  # 이제 B 차례
    for difficulty in (EASY, NORMAL, HARD):
        bot = create_ai(difficulty, seed=1)
        with pytest.raises(AIError):
            bot.select_action(game.view_for("A"))


def test_belief_hands_respect_colour_constraints() -> None:
    game = BlackWhiteGame(first_player="B", rng=random.Random(5))
    game._hands["B"] = [5, 6]  # 흑 1(6), 백 1(5)
    game.submit_tile("B", 5)  # 백 5 사용 → 남은 흑 1
    view = game.view_for("A")
    obs = Observation.from_view(view)
    assert obs.opp_played_tile_unknown is True
    rng = random.Random(0)
    for _ in range(20):
        hand, played = sample_opponent_hand(obs, rng)
        assert len(hand) == 1 and hand[0] in BLACK_TILES
        assert played in WHITE_TILES
    assert count_belief_hands(obs.opp_remaining_black, obs.opp_remaining_white) == 5 * 1
    weights = opponent_marginal(2, 2)
    assert {tile for tile, _ in weights} == set(BLACK_TILES) | set(WHITE_TILES)
    assert sum(w for _, w in weights) == pytest.approx(1.0)


def test_legal_actions_helper_matches_engine() -> None:
    game = BlackWhiteGame(first_player="A", rng=random.Random(9))
    assert legal_actions_from_view(game.view_for("A")) == game.legal_actions("A")
    assert legal_actions_from_view(game.view_for("B")) == []
    game.submit_tile("A", 2)
    assert legal_actions_from_view(game.view_for("B")) == game.legal_actions("B")


# ---------------------------------------------------------------------------
# 난이도별 합법성 / 완주
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "difficulty_a,difficulty_b",
    [
        ("easy", "easy"),
        ("easy", "normal"),
        ("normal", "hard"),
        ("hard", "easy"),
    ],
)
def test_each_difficulty_plays_only_legal_moves(difficulty_a: str, difficulty_b: str) -> None:
    for seed in range(3):
        ai_a = create_ai(difficulty_a, seed=seed)
        ai_b = create_ai(difficulty_b, seed=100 + seed)
        winner, game = play_game(ai_a, ai_b, seed=seed)
        assert winner in ("A", "B", None)
        assert game.rounds_played >= 1
        # 게임이 끝났다면 복기/점수가 정상이다.
        if game.is_over:
            assert game.winner in ("A", "B")


def test_easy_ai_is_random_across_calls() -> None:
    game = BlackWhiteGame(first_player="A", rng=random.Random(2))
    view = game.view_for("A")
    bot = create_ai("easy", seed=42)
    picks = [bot.select_action(view) for _ in range(60)]
    assert len(set(picks)) > 1  # 균등 무작위 → 여러 타일 등장
    assert set(picks) <= set(range(9))
    # 같은 시드의 두 인스턴스는 같은 수열을 낸다(재현성).
    twin = create_ai("easy", seed=42)
    assert picks == [twin.select_action(view) for _ in range(60)]


def test_normal_ai_uses_revealed_colour_and_score() -> None:
    # 후공: 상대가 백(홀수)을 냈다 → 8 은 백을 모두 이긴다.
    game = BlackWhiteGame(first_player="B", rng=random.Random(4))
    game._hands["B"] = [1, 2]
    game.submit_tile("B", 1)  # 백 1 사용
    view = game.view_for("A")
    # 8라운드(남은 라운드 2) · 내 잔여 [0, 8] 로 맞춰 둔다(관측과 일관).
    view["round"] = 8
    view["you"]["remaining"] = [0, 8]
    normal = create_ai("normal", seed=0)
    assert normal.select_action(view) == 8
    hard = create_ai("hard", seed=0)
    assert hard.select_action(view) == 8


def test_hard_ai_reports_action_values_for_every_legal_tile() -> None:
    game = BlackWhiteGame(first_player="A", rng=random.Random(6))
    view = game.view_for("A")
    bot = create_ai("hard", seed=3)
    values = bot.action_values(view)
    assert set(values) == set(range(9))
    assert all(isinstance(v, float) for v in values.values())
    # 가장 큰 값을 고른다(작은 타일 우선 동점 처리).
    best = max(values.values())
    assert values[bot.select_action(view)] == pytest.approx(best)


# ---------------------------------------------------------------------------
# 정보 은닉 — 어려움 AI 가 숨은 숫자를 보지 않는다
# ---------------------------------------------------------------------------
def test_hard_ai_is_invariant_to_hidden_opponent_tiles() -> None:
    """같은 관측 + 같은 시드 → 상대 숨은 패와 무관하게 완전히 같은 수."""

    pairs = [
        ((0, 1, 2), (4, 5, 6)),  # 흑 2 · 백 1 — 숫자만 다르다
        ((2, 3, 4), (6, 7, 8)),
        ((0,), (8,)),
    ]
    for hidden_a, hidden_b in pairs:
        view_a = hidden_hand_game(hidden_a).view_for("A")
        view_b = hidden_hand_game(hidden_b).view_for("A")
        assert view_a == view_b, "같은 색 구성을 만들었는데 관측이 다릅니다(테스트 전제 오류)"
        for mode in ("enumerate", "sample"):
            for seed in (0, 1, 2, 12345):
                bot_a = create_ai("hard", seed=seed, belief_mode=mode)
                bot_b = create_ai("hard", seed=seed, belief_mode=mode)
                assert bot_a.select_action(view_a) == bot_b.select_action(view_b)


def test_hard_ai_is_invariant_when_opponent_already_played() -> None:
    """후공 상황(상대 타일 색만 공개)에서도 숫자가 달라도 같은 수를 고른다."""

    cases = [((1, 2, 3), 3), ((5, 6, 7), 7), ((1, 6, 7), 7)]
    views: List[Dict[str, object]] = []
    for hidden, played in cases:
        game = hidden_hand_game(hidden, first_player="B", played=played)
        view = game.view_for("A")
        assert view["phase"] == "WAITING_SECOND"
        assert view["opponent"]["submitted_color"] == "white"
        views.append(view)
    assert views[0] == views[1] == views[2]
    for mode in ("enumerate", "sample"):
        bot = create_ai("hard", seed=5, belief_mode=mode)
        assert len({bot.select_action(view) for view in views}) == 1


def test_hard_ai_works_from_plain_json_view() -> None:
    """엔진 객체 없이, JSON 으로 직렬화/복원한 관측만으로도 같은 수를 고른다."""

    game = BlackWhiteGame(first_player="A", rng=random.Random(11))
    game.submit_tile("A", 6)
    view = game.view_for("B")
    plain = json.loads(json.dumps(view, ensure_ascii=False))
    bot = create_ai("hard", seed=9)
    assert bot.select_action(view) == bot.select_action(plain)


def test_ai_module_never_touches_dealer_internal_state() -> None:
    """소스 수준에서도 ``full_state`` / ``._hands`` 같은 딜러 전용 상태를 쓰지 않는다."""

    tree = ast.parse(inspect.getsource(ai_module))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    forbidden = {"_hands", "_tiles", "_submitted", "full_state", "remaining_tiles"}
    assert not (attributes & forbidden), attributes & forbidden
    assert not (names & {"full_state"}), names & {"full_state"}


# ---------------------------------------------------------------------------
# 시뮬레이션 — 어려움 vs 쉬움
# ---------------------------------------------------------------------------
def test_hard_ai_beats_easy_ai_consistently() -> None:
    games = 60
    wins = losses = draws = 0
    for index in range(games):
        hard = create_ai("hard", seed=index)
        easy = create_ai("easy", seed=10_000 + index)
        if index % 2 == 0:
            winner, _ = play_game(hard, easy, seed=index)
            wins += winner == "A"
            losses += winner == "B"
        else:
            winner, _ = play_game(easy, hard, seed=index)
            wins += winner == "B"
            losses += winner == "A"
        draws += winner is None
    total = max(1, wins + losses)
    rate = wins / total
    assert rate >= 0.55, f"어려움 AI 승률이 너무 낮습니다: {wins}승 {losses}패 {draws}무"
    assert wins > losses


def test_hard_ai_beats_normal_ai_head_to_head() -> None:
    wins = losses = 0
    for index in range(40):
        hard = create_ai("hard", seed=index)
        normal = create_ai("normal", seed=20_000 + index)
        if index % 2 == 0:
            winner, _ = play_game(hard, normal, seed=index)
            wins += winner == "A"
            losses += winner == "B"
        else:
            winner, _ = play_game(normal, hard, seed=index)
            wins += winner == "B"
            losses += winner == "A"
    assert wins > losses, f"어려움이 보통을 이기지 못했습니다: {wins}승 {losses}패"


def test_simulation_never_leaks_hidden_numbers_in_views() -> None:
    """시뮬레이션 중 관측에도 상대 숫자가 절대 들어가지 않는다(다시 한 번 확인)."""

    game = BlackWhiteGame(first_player="A", rng=random.Random(21))
    hard = create_ai("hard", seed=0)
    easy = create_ai("easy", seed=0)
    steps = 0
    while not game.is_over and steps < 40:
        if game.phase is Phase.ROUND_RESULT:
            game.advance_round()
            continue
        player = game.turn
        view = game.view_for(player)
        payload = json.dumps(view, ensure_ascii=False)
        assert "opponent_tile" not in payload
        assert set(view["opponent"]) == {
            "id",
            "remaining_black",
            "remaining_white",
            "submitted_color",
        }
        game.step(player, (hard if player == "A" else easy).select_action(view))
        steps += 1
