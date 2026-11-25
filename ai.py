"""「흑과 백」 AI 플레이어 — 규칙 기반(쉬움/보통/어려움) AI.

이 모듈은 ``engine.BlackWhiteGame`` 을 **관측(observation)으로만** 본다.
AI 는 ``game.view_for(ai_player)`` 가 돌려준 dict 하나만 받아 한 수를 고르므로,
상대의 숨은 숫자(딜러 내부 상태)를 읽을 방법이 애초에 없다. 같은 관측 + 같은 시드
= 언제나 같은 수라는 성질 덕분에 정보 누수 여부를 테스트로 검증할 수 있다.

인터페이스 (RL/Self-Play 교체 지점)
-----------------------------------
::

    class BaseAI:
        def select_action(self, observation: Dict[str, Any]) -> int: ...

``observation`` 은 ``BlackWhiteGame.view_for(player)`` 의 반환값과 같은 형태이며,
``turn == viewer`` 일 때만 호출한다(아니면 ``AIError``). 학습된 모델로 바꿀 때는
``BaseAI`` 를 구현한 클래스를 ``create_ai()`` 팩토리에 등록하기만 하면 되고
``server.py`` · 엔진 · 클라이언트는 손대지 않는다.

난이도
------
- ``easy``   : 합법 수 중 균등 무작위.
- ``normal`` : 상대가 공개한 흑/백 색상, 남은 흑/백 개수, 현재 점수 차, 남은 타일
  구성을 반영한 **기댓값 휴리스틱**. 상대 다음 타일의 주변 확률을 색상 제약에서
  해석적으로 계산하고(표본 오차 없음), 이번 라운드 기대 승점 + 남은 라운드의 기대
  승점(쌍 비교 기대값)을 합산한다.
- ``hard``   : **제약 기반 신념(Belief Sampling) + Expectimax/Minimax 탐색**.
  상대가 이미 낸 타일의 색과 남은 흑/백 개수가 만드는 **가능한 손패 조합**을 세우고
  (기본은 전수 열거, 조합이 많으면 시드 기반 샘플링), 그 신념 위에서 남은 라운드를
  전개해 기대 점수 차가 가장 큰 수를 고른다. 내 차례는 최대화, 상대 차례는 신념
  분포에 대한 기대(``expect``) 또는 최악(``min``)으로 접는다.

정보 은닉 원칙
--------------
- 입력은 ``view_for`` 의 공개 필드뿐이다(``Observation.from_view`` 가 공개 필드만
  복사한다). 엔진 인스턴스를 참조하지 않으므로 ``_hands`` / ``_tiles`` /
  ``full_state()`` 같은 딜러 전용 상태에 접근하는 코드 경로가 없다.
- 상대 손패는 항상 **색상 제약을 만족하는 후보(가설)일 뿐**이고, 실제 숫자가 아니라
  관측에서 유도한 분포/표본이다.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from engine import (
    BLACK_TILES,
    COLOR_BLACK,
    COLOR_WHITE,
    PLAYERS,
    ROUNDS_PER_SET,
    TILES,
    WHITE_TILES,
)

__all__ = [
    "AI_VERSION",
    "ai_version",
    "EASY",
    "NORMAL",
    "HARD",
    "DIFFICULTIES",
    "AI_THINK_HINT",
    "AIError",
    "UnknownDifficultyError",
    "Observation",
    "BaseAI",
    "RandomAI",
    "HeuristicAI",
    "SearchAI",
    "create_ai",
    "normalize_difficulty",
    "legal_actions_from_view",
    "sample_opponent_hand",
]

#: AI 구현 버전. 게임 로그(``games.ai_version``)에 함께 남겨 모델 교체 전후를 구분한다.
AI_VERSION = "1.0.0"
ai_version = AI_VERSION

EASY = "easy"
NORMAL = "normal"
HARD = "hard"
DIFFICULTIES: Tuple[str, str, str] = (EASY, NORMAL, HARD)

#: 난이도별 한 줄 설명(클라이언트 문구·문서용).
AI_THINK_HINT = {
    EASY: "무작위로 둡니다.",
    NORMAL: "색·점수·남은 타일을 계산해 둡니다.",
    HARD: "상대 손패를 추정해 몇 수 앞을 내다봅니다.",
}

#: 난이도 표기 별칭(한글·구 표기 허용).
_DIFFICULTY_ALIASES = {
    "easy": EASY,
    "쉬움": EASY,
    "normal": NORMAL,
    "medium": NORMAL,  # 2단계 storage 스키마의 표기
    "보통": NORMAL,
    "hard": HARD,
    "어려움": HARD,
    "difficult": HARD,
}


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------
class AIError(Exception):
    """AI 가 수를 고를 수 없을 때(차례 아님·합법 수 없음) 발생한다."""


class UnknownDifficultyError(AIError, ValueError):
    """알 수 없는 난이도 이름."""


def normalize_difficulty(difficulty: Optional[str]) -> str:
    """난이도 문자열을 ``easy|normal|hard`` 로 정규화한다(기본 ``normal``)."""

    if difficulty is None:
        return NORMAL
    key = str(difficulty).strip().lower()
    if not key:
        return NORMAL
    resolved = _DIFFICULTY_ALIASES.get(key)
    if resolved is None:
        raise UnknownDifficultyError(
            f"난이도는 {DIFFICULTIES} 중 하나여야 합니다: {difficulty!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# 관측(observation) — view_for() 의 공개 필드만 담는다
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Observation:
    """``game.view_for(viewer)`` 에서 AI 가 쓰는 공개 정보만 뽑아 둔 값 객체.

    숨은 숫자는 담을 자리 자체가 없다(상대는 색과 개수만).
    """

    viewer: str
    phase: str
    set_index: int
    round: int
    rounds_per_set: int
    turn: Optional[str]
    first_player: str
    second_player: str
    scores: Tuple[Tuple[str, int], ...]
    own_hand: Tuple[int, ...]
    own_submitted: Optional[int]
    opp_remaining_black: int
    opp_remaining_white: int
    opp_submitted_color: Optional[str]

    # -- 생성 --------------------------------------------------------------
    @classmethod
    def from_view(cls, view: Dict[str, Any]) -> "Observation":
        if not isinstance(view, dict):
            raise AIError("관측은 view_for() 가 돌려준 dict 여야 합니다.")

        def _ints(values: Any) -> Tuple[int, ...]:
            out: List[int] = []
            for value in values or ():
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and value in TILES:
                    out.append(value)
            return tuple(sorted(out))

        you = view.get("you") or {}
        opponent = view.get("opponent") or {}
        scores = view.get("scores") or {}
        viewer = str(view.get("viewer") or "")
        first_player = str(view.get("first_player") or "")
        second_player = str(
            view.get("second_player")
            or next((p for p in PLAYERS if p != first_player), "")
        )

        submitted = you.get("submitted")
        return cls(
            viewer=viewer,
            phase=str(view.get("phase") or ""),
            set_index=int(view.get("set_index") or 0),
            round=int(view.get("round") or 1),
            rounds_per_set=int(view.get("rounds_per_set") or ROUNDS_PER_SET),
            turn=(None if view.get("turn") in (None, "") else str(view.get("turn"))),
            first_player=first_player,
            second_player=second_player,
            scores=tuple(sorted((str(k), int(v)) for k, v in scores.items())),
            own_hand=_ints(you.get("remaining")),
            own_submitted=submitted if isinstance(submitted, int) else None,
            opp_remaining_black=max(0, int(opponent.get("remaining_black") or 0)),
            opp_remaining_white=max(0, int(opponent.get("remaining_white") or 0)),
            opp_submitted_color=(
                str(opponent.get("submitted_color"))
                if opponent.get("submitted_color") in (COLOR_BLACK, COLOR_WHITE)
                else None
            ),
        )

    # -- 파생 값 -----------------------------------------------------------
    @property
    def is_my_turn(self) -> bool:
        return self.turn is not None and self.turn == self.viewer

    @property
    def my_score(self) -> int:
        return dict(self.scores).get(self.viewer, 0)

    @property
    def opp_score(self) -> int:
        other = self.second_player if self.viewer == self.first_player else self.first_player
        return dict(self.scores).get(other, 0)

    @property
    def score_diff(self) -> int:
        """내 점수 - 상대 점수."""

        return self.my_score - self.opp_score

    @property
    def i_am_first(self) -> bool:
        return self.viewer == self.first_player

    @property
    def opp_hand_size(self) -> int:
        return self.opp_remaining_black + self.opp_remaining_white

    @property
    def rounds_left_in_set(self) -> int:
        """이번 라운드를 포함한 남은 라운드 수."""

        return max(0, self.rounds_per_set - (self.round - 1))

    @property
    def legal_actions(self) -> List[int]:
        if not self.is_my_turn:
            return []
        if self.phase not in ("WAITING_FIRST", "WAITING_SECOND"):
            return []
        return list(self.own_hand)

    @property
    def opp_played_tile_unknown(self) -> bool:
        """상대가 이번 라운드에 이미 냈지만 숫자는 아직 모르는 상태인가."""

        return (
            self.phase == "WAITING_SECOND"
            and not self.i_am_first
            and self.opp_submitted_color in (COLOR_BLACK, COLOR_WHITE)
        )

    def as_dict(self) -> Dict[str, Any]:
        """시드 파생·디버깅용 순수 dict(공개 정보만)."""

        return {
            "viewer": self.viewer,
            "phase": self.phase,
            "set_index": self.set_index,
            "round": self.round,
            "rounds_per_set": self.rounds_per_set,
            "turn": self.turn,
            "first_player": self.first_player,
            "second_player": self.second_player,
            "scores": list(self.scores),
            "own_hand": list(self.own_hand),
            "own_submitted": self.own_submitted,
            "opp_remaining_black": self.opp_remaining_black,
            "opp_remaining_white": self.opp_remaining_white,
            "opp_submitted_color": self.opp_submitted_color,
        }


def legal_actions_from_view(view: Dict[str, Any]) -> List[int]:
    """관측에서 합법 수 목록을 구한다(차례가 아니면 빈 리스트)."""

    return Observation.from_view(view).legal_actions


# ---------------------------------------------------------------------------
# 신념(belief) 샘플링
# ---------------------------------------------------------------------------
def sample_opponent_hand(
    observation: Observation,
    rng: random.Random,
) -> Tuple[Tuple[int, ...], Optional[int]]:
    """색상 제약을 만족하는 상대 손패 가설 하나를 뽑는다.

    Returns
    -------
    (hand, played_tile)
        ``hand`` 는 상대의 **남은** 타일(개수·흑백 구성이 관측과 일치),
        ``played_tile`` 은 상대가 이번 라운드에 이미 낸 타일(모르면 ``None``).
        둘 다 실제 값이 아니라 관측을 만족하는 가설이다.
    """

    blacks = list(BLACK_TILES)
    whites = list(WHITE_TILES)
    played: Optional[int] = None

    if observation.opp_played_tile_unknown:
        color = observation.opp_submitted_color
        pool = blacks if color == COLOR_BLACK else whites
        if pool:
            played = rng.choice(pool)
            if color == COLOR_BLACK:
                blacks = [tile for tile in blacks if tile != played]
            else:
                whites = [tile for tile in whites if tile != played]

    k_black = min(observation.opp_remaining_black, len(blacks))
    k_white = min(observation.opp_remaining_white, len(whites))
    hand = tuple(sorted(rng.sample(blacks, k_black) + rng.sample(whites, k_white)))
    return hand, played


def _color_pool(color: Optional[str]) -> Tuple[int, ...]:
    return BLACK_TILES if color == COLOR_BLACK else WHITE_TILES


def opponent_next_tile_weights(observation: Observation) -> List[Tuple[int, float]]:
    """상대가 다음에 낼 타일의 주변 확률(색상 제약 기반 해석적 계산).

    상대 손패를 균등 무작위로 뽑는다고 보면, 특정 숫자가 나올 확률은 그 색의
    정규 타일 집합 위에서 균등하다. 이번 라운드에 이미 냈다면(후공 차례) 그 타일의
    색만 알 뿐이므로 역시 그 색의 타일 위에서 균등하다.
    """

    if observation.opp_played_tile_unknown:
        pool = _color_pool(observation.opp_submitted_color)
        return [(tile, 1.0 / len(pool)) for tile in pool]

    total = observation.opp_hand_size
    if total <= 0:
        return []
    weights: List[Tuple[int, float]] = []
    if observation.opp_remaining_black:
        share = (observation.opp_remaining_black / total) / len(BLACK_TILES)
        weights.extend((tile, share) for tile in BLACK_TILES)
    if observation.opp_remaining_white:
        share = (observation.opp_remaining_white / total) / len(WHITE_TILES)
        weights.extend((tile, share) for tile in WHITE_TILES)
    return weights


# ---------------------------------------------------------------------------
# 기본 인터페이스
# ---------------------------------------------------------------------------
class BaseAI:
    """AI 공통 인터페이스. 학습 모델로 교체할 때 이 클래스만 구현하면 된다."""

    #: 난이도 이름(``easy``/``normal``/``hard``).
    difficulty: str = NORMAL
    #: 구현 이름(로그·디버깅용).
    name: str = "base"

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = 0 if seed is None else int(seed)

    # -- 필수 구현 ---------------------------------------------------------
    def select_action(self, observation: Dict[str, Any]) -> int:
        """관측 하나를 받아 낼 타일(0~8) 하나를 돌려준다."""

        raise NotImplementedError

    # -- 선택 구현 ---------------------------------------------------------
    def reset(self, *, seed: Optional[int] = None) -> None:
        """판이 바뀌었을 때 호출할 수 있는 훅(기본은 시드만 갱신)."""

        if seed is not None:
            self.seed = int(seed)

    def info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "difficulty": self.difficulty,
            "ai_version": AI_VERSION,
        }

    def __call__(self, observation: Dict[str, Any]) -> int:
        return self.select_action(observation)

    # -- 공통 헬퍼 ---------------------------------------------------------
    def _observe(self, observation: Dict[str, Any]) -> Observation:
        obs = Observation.from_view(observation)
        if not obs.is_my_turn:
            raise AIError(
                f"{self.difficulty} AI 차례가 아닙니다(관측 turn={obs.turn!r}, "
                f"viewer={obs.viewer!r})."
            )
        if not obs.legal_actions:
            raise AIError("낼 수 있는 타일이 없습니다.")
        return obs

    def _rng_for(self, observation: Dict[str, Any]) -> random.Random:
        """관측+시드로 결정되는 난수기(같은 관측·같은 시드 → 같은 수)."""

        payload = json.dumps(observation, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        mixed = int.from_bytes(digest[:8], "big") ^ (self.seed & 0xFFFFFFFFFFFFFFFF)
        return random.Random(mixed)


def _sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# 쉬움 — 균등 무작위
# ---------------------------------------------------------------------------
class RandomAI(BaseAI):
    """합법 수 중 하나를 균등하게 고른다."""

    difficulty = EASY
    name = "uniform-random"

    def __init__(self, seed: Optional[int] = None) -> None:
        super().__init__(seed)
        self._rng = random.Random(self.seed)

    def select_action(self, observation: Dict[str, Any]) -> int:
        obs = self._observe(observation)
        return self._rng.choice(obs.legal_actions)

    def reset(self, *, seed: Optional[int] = None) -> None:
        super().reset(seed=seed)
        self._rng = random.Random(self.seed)


# ---------------------------------------------------------------------------
# 보통 — 기댓값 휴리스틱
# ---------------------------------------------------------------------------
class HeuristicAI(BaseAI):
    """색·점수·남은 타일을 반영한 기댓값 휴리스틱.

    각 후보 타일에 대해

    1. 이번 라운드 기대 승점(승 +1 / 무 0 / 패 -1)을 상대 타일의 색상 제약 분포로 계산,
    2. 남은 라운드의 기대 승점을 "내 남은 패 vs 상대 색상 제약 분포"의 쌍 비교
       기대로 계산,
    3. 점수 차에 따라 (뒤지면 공격적으로, 앞서면 안전하게) 가중치를 조정

    한 뒤, 잡음(``noise``)을 더해 고른다.
    """

    difficulty = NORMAL
    name = "expectation-heuristic"

    def __init__(
        self,
        seed: Optional[int] = None,
        *,
        future_weight: float = 0.7,
        noise: float = 0.12,
    ) -> None:
        super().__init__(seed)
        self.future_weight = float(future_weight)
        self.noise = max(0.0, float(noise))

    def select_action(self, observation: Dict[str, Any]) -> int:
        obs = self._observe(observation)
        rng = self._rng_for(observation)
        weights = opponent_next_tile_weights(obs)
        rounds_left = max(0, obs.rounds_left_in_set - 1)

        best_tile = obs.legal_actions[0]
        best_value = float("-inf")
        for tile in obs.legal_actions:
            value = self._value(obs, tile, weights, rounds_left)
            value += rng.uniform(-self.noise, self.noise)
            if value > best_value:
                best_value = value
                best_tile = tile
        return best_tile

    # -- 내부 --------------------------------------------------------------
    def _value(
        self,
        obs: Observation,
        tile: int,
        weights: Sequence[Tuple[int, float]],
        rounds_left: int,
    ) -> float:
        p_win = p_loss = 0.0
        for opponent_tile, weight in weights:
            if tile > opponent_tile:
                p_win += weight
            elif tile < opponent_tile:
                p_loss += weight

        remaining = [t for t in obs.own_hand if t != tile]
        # 점수 차 반영: 뒤지면 승리에 더 큰 가중, 앞서면 패배 회피에 가중.
        behind = max(0, -obs.score_diff)
        ahead = max(0, obs.score_diff)
        span = max(1, obs.rounds_left_in_set)
        attack = 0.25 * min(1.0, behind / span)
        guard = 0.20 * min(1.0, ahead / span)

        immediate = (1.0 + attack) * p_win - (1.0 + guard) * p_loss
        future = self.future_weight * self._future_expectation(remaining, weights, rounds_left)
        return immediate + future

    def _future_expectation(
        self,
        remaining: Sequence[int],
        weights: Sequence[Tuple[int, float]],
        rounds_left: int,
    ) -> float:
        if not remaining or not weights or rounds_left <= 0:
            return 0.0
        per_round = 0.0
        for mine in remaining:
            for theirs, weight in weights:
                per_round += weight * _sign(mine - theirs)
        per_round /= len(remaining)
        return per_round * rounds_left


# ---------------------------------------------------------------------------
# 어려움 — 제약 기반 신념(Belief) + Expectimax/Minimax
# ---------------------------------------------------------------------------
def opponent_marginal(kb: int, kw: int) -> Tuple[Tuple[int, float], ...]:
    """상대가 아직 내지 않았을 때, 다음 타일의 주변 분포.

    상대 손패를 "흑 kb장 + 백 kw장"을 만족하는 균등 무작위 부분집합으로 보면,
    특정 숫자가 다음에 나올 확률은 그 색의 정규 타일 집합 위에서 균등하다
    (색 비율만큼 가중). 제약을 만족하는 모든 손패 조합을 열거한 것과 같은 기댓값이다.
    """

    total = kb + kw
    if total <= 0:
        return ()
    pairs: List[Tuple[int, float]] = []
    if kb:
        share = kb / (total * len(BLACK_TILES))
        pairs.extend((tile, share) for tile in BLACK_TILES)
    if kw:
        share = kw / (total * len(WHITE_TILES))
        pairs.extend((tile, share) for tile in WHITE_TILES)
    return tuple(pairs)


def _aggregate(pairs: Sequence[Tuple[float, float]], model: str) -> float:
    """(값, 확률) 목록을 기대값(``expect``) 또는 최악값(``min``)으로 접는다."""

    if not pairs:
        return 0.0
    if model == "min":
        return min(value for value, _ in pairs)
    return sum(value * weight for value, weight in pairs)


def _tail_value(hand: Tuple[int, ...], kb: int, kw: int, rounds: int) -> float:
    """탐색 깊이를 다 쓴 지점의 평가값(남은 라운드의 쌍 비교 기대 승점)."""

    if rounds <= 0 or not hand:
        return 0.0
    marginal = opponent_marginal(kb, kw)
    if not marginal:
        return 0.0
    per_round = 0.0
    for mine in hand:
        for theirs, weight in marginal:
            per_round += weight * _sign(mine - theirs)
    return per_round / len(hand) * rounds


@lru_cache(maxsize=200_000)
def belief_value(
    hand: Tuple[int, ...],
    kb: int,
    kw: int,
    rounds: int,
    first_is_us: bool,
    depth: int,
    model: str = "expect",
) -> float:
    """색상 제약 신념 위의 Expectimax — **정보집합** 상태의 기대 점수 차.

    상태는 "내 남은 패 + 상대의 남은 흑/백 **개수**"뿐이다. 상대의 실제 숫자는
    어디에도 들어오지 않으므로, 어떤 숨은 패에도 결과가 달라지지 않는다.

    - 내 차례: 가능한 수 중 최대(``max``)
    - 상대 차례: 색상 제약이 만드는 후보 분포에 대한 기대(``expect``) 또는 최악(``min``)

    ``rounds`` 는 이번 라운드를 포함한 남은 라운드, ``depth`` 는 더 전개할 라운드 수다.
    메모이제이션 덕분에 세트 끝까지 전개해도 상태 수가 수천 개 수준이라 즉시 끝난다.
    """

    if rounds <= 0 or depth <= 0 or not hand or (kb + kw) <= 0:
        return _tail_value(hand, kb, kw, rounds)

    if first_is_us:
        # 내가 선공: 내 타일을 고른다 → 상대 응답은 신념 분포 기대.
        best = float("-inf")
        for tile in hand:
            rest = tuple(t for t in hand if t != tile)
            pairs: List[Tuple[float, float]] = []
            for opponent_tile, weight in opponent_marginal(kb, kw):
                delta = _sign(tile - opponent_tile)
                pairs.append(
                    (
                        delta
                        + belief_value(
                            rest,
                            kb - (1 if opponent_tile % 2 == 0 else 0),
                            kw - (1 if opponent_tile % 2 == 1 else 0),
                            rounds - 1,
                            delta >= 0,  # 무승부면 선공(나) 유지
                            depth - 1,
                            model,
                        ),
                        weight,
                    )
                )
            best = max(best, _aggregate(pairs, model))
        return best

    # 상대가 선공: 나는 **색만** 보고 내 수를 고른다 → 색별 조건부로 최선을 고른 뒤
    # 색 확률로 가중한다(숫자를 몰라도 되는 진짜 정보 구조).
    total = kb + kw
    value = 0.0
    for color, count, pool in (
        (COLOR_BLACK, kb, BLACK_TILES),
        (COLOR_WHITE, kw, WHITE_TILES),
    ):
        if count <= 0:
            continue
        kb_after = kb - (1 if color == COLOR_BLACK else 0)
        kw_after = kw - (1 if color == COLOR_WHITE else 0)
        best = float("-inf")
        for tile in hand:
            rest = tuple(t for t in hand if t != tile)
            pairs = []
            for opponent_tile in pool:
                # 상대 타일 숫자는 그 색 풀에서 균등(색만 공개된다).
                delta = _sign(tile - opponent_tile)
                pairs.append(
                    (
                        delta
                        + belief_value(
                            rest,
                            kb_after,
                            kw_after,
                            rounds - 1,
                            delta < 0,  # 무승부면 선공(상대) 유지
                            depth - 1,
                            model,
                        ),
                        1.0 / len(pool),
                    )
                )
            best = max(best, _aggregate(pairs, model))
        value += (count / total) * best
    return value


def count_belief_hands(kb: int, kw: int) -> int:
    """`(흑 kb, 백 kw)` 제약을 만족하는 상대 손패 조합의 수(전수 열거 가능 판단용)."""

    return math.comb(len(BLACK_TILES), min(max(kb, 0), len(BLACK_TILES))) * math.comb(
        len(WHITE_TILES), min(max(kw, 0), len(WHITE_TILES))
    )


class SearchAI(BaseAI):
    """제약 기반 신념(Belief) + Expectimax/Minimax 탐색.

    동작 순서

    1. **신념 구성** — ``game.view_for()`` 의 공개 정보만으로 상대 손패 제약을 세운다.
       (남은 흑/백 개수, 이번 라운드에 낸 타일의 색, 이미 쓴 타일 색 이력)
    2. **후보 손패 열거/샘플링** — 제약을 만족하는 상대 손패 조합을 **전수 열거**해
       그 위의 기대값을 정확히 계산한다. 조합 수가 ``max_hands`` 를 넘으면(또는
       ``belief_mode="sample"``) ``samples`` 개를 시드 기반으로 샘플링해 근사한다.
       어느 쪽이든 AI 가 보는 것은 **후보 조합**일 뿐 실제 패가 아니다.
    3. **Expectimax/Minimax 탐색** — 각 후보 수 이후의 국면을 ``belief_value()`` 로
       전개한다. 내 차례는 최대화, 상대 차례는 제약 분포에 대한 기대(``expect``)
       또는 최악(``min``) 노드로 접는다. 상태가 "내 패 + 상대 흑/백 **개수**"로
       추상화되어 있어 세트 끝까지 전개해도 상태 수가 수천 개 수준이고,
       ``lru_cache`` 메모이제이션 덕분에 한 수 계산이 밀리초 단위로 끝난다.
    4. 기대 점수 차가 가장 큰 수를 고른다(완전 동점이면 작은 타일).

    Parameters
    ----------
    seed:
        샘플링 시드. 같은 관측·같은 시드면 항상 같은 수를 고른다(정보 누수 테스트용).
    samples:
        ``belief_mode="sample"``(또는 조합 수 초과)일 때 쓸 손패 가설 개수.
    max_hands:
        전수 열거할 최대 조합 수(기본 512 — 기본 게임의 최대 60조합은 항상 전수).
    lookahead:
        전개할 라운드 수. 기본값은 남은 세트 전체(최대 9)이다.
    future_weight:
        평가 함수에서 남은 라운드 기대 승점에 곱하는 가중치.
    opponent_model:
        ``"expect"``(상대를 균등 무작위로 보는 Expectimax) 또는
        ``"min"``(상대 최선 응답을 가정하는 Minimax).
    belief_mode:
        ``"enumerate"``(기본, 제약 조합 전수) 또는 ``"sample"``(시드 기반 표본 근사).
    """

    difficulty = HARD
    name = "belief-expectimax"

    def __init__(
        self,
        seed: Optional[int] = None,
        *,
        samples: int = 64,
        max_hands: int = 512,
        lookahead: int = ROUNDS_PER_SET,
        future_weight: float = 1.0,
        opponent_model: str = "expect",
        belief_mode: str = "enumerate",
    ) -> None:
        super().__init__(seed)
        self.samples = max(1, int(samples))
        self.max_hands = max(1, int(max_hands))
        self.lookahead = max(1, int(lookahead))
        self.future_weight = float(future_weight)
        model = str(opponent_model).strip().lower()
        if model not in ("expect", "min"):
            raise AIError("opponent_model 은 'expect' 또는 'min' 입니다.")
        self.opponent_model = model
        mode = str(belief_mode).strip().lower()
        if mode not in ("enumerate", "sample"):
            raise AIError("belief_mode 는 'enumerate' 또는 'sample' 입니다.")
        self.belief_mode = mode

    # -- 공개 API ----------------------------------------------------------
    def select_action(self, observation: Dict[str, Any]) -> int:
        obs = self._observe(observation)
        worlds = self._sample_worlds(obs, self._rng_for(observation))
        values = {
            tile: self._action_value(obs, tile, worlds) for tile in obs.legal_actions
        }
        # 완전 동점이면 작은 타일(결정적). 값 차이는 실제로 거의 항상 존재한다.
        best_tile = obs.legal_actions[0]
        best_value = float("-inf")
        for tile in obs.legal_actions:
            value = values[tile]
            if value > best_value:
                best_value = value
                best_tile = tile
        return best_tile

    def belief(self, observation: Dict[str, Any], *, samples: int = 4) -> List[Tuple[int, ...]]:
        """디버깅/검증용: 이 관측에서 AI 가 상정하는 상대 손패 가설들."""

        obs = Observation.from_view(observation)
        rng = self._rng_for(observation)
        return [sample_opponent_hand(obs, rng)[0] for _ in range(max(1, samples))]

    def action_values(self, observation: Dict[str, Any]) -> Dict[int, float]:
        """디버깅/검증용: 후보 수별 기대 점수 차."""

        obs = self._observe(observation)
        worlds = self._sample_worlds(obs, self._rng_for(observation))
        return {
            tile: self._action_value(obs, tile, worlds) for tile in obs.legal_actions
        }

    # -- 내부 --------------------------------------------------------------
    def _sample_worlds(
        self, obs: Observation, rng: random.Random
    ) -> Optional[List[Tuple[Tuple[int, ...], Optional[int]]]]:
        """표본 근사가 필요하면 손패 가설 목록을, 정확 계산이면 ``None`` 을 돌려준다."""

        combos = count_belief_hands(obs.opp_remaining_black, obs.opp_remaining_white)
        if self.belief_mode != "sample" and combos <= self.max_hands:
            return None
        return [sample_opponent_hand(obs, rng) for _ in range(self.samples)]

    def _action_value(
        self,
        obs: Observation,
        tile: int,
        worlds: Optional[Sequence[Tuple[Tuple[int, ...], Optional[int]]]],
    ) -> float:
        rest = tuple(t for t in obs.own_hand if t != tile)
        kb, kw = obs.opp_remaining_black, obs.opp_remaining_white
        rounds = max(0, obs.rounds_left_in_set - 1)
        depth = self.lookahead
        model = self.opponent_model

        if obs.opp_played_tile_unknown:
            # 우리가 후공: 상대가 이미 낸 타일은 색만 공개됐다 → 그 색 풀에서 균등.
            pool = _color_pool(obs.opp_submitted_color)
            if worlds is not None:
                total = 0.0
                counted = 0
                for _, played in worlds:
                    if played is None:
                        continue
                    delta = _sign(tile - played)
                    total += delta + belief_value(rest, kb, kw, rounds, delta < 0, depth, model)
                    counted += 1
                if counted:
                    return total / counted
                return _tail_value(hand=rest, kb=kb, kw=kw, rounds=rounds)
            pairs = []
            for opponent_tile in pool:
                delta = _sign(tile - opponent_tile)
                pairs.append(
                    (
                        delta + belief_value(rest, kb, kw, rounds, delta < 0, depth, model),
                        1.0 / len(pool),
                    )
                )
            return _aggregate(pairs, model)

        # 우리가 선공: 상대 응답은 신념 손패 가설에서 균등하게 뽑힌다.
        if worlds is not None:
            total = 0.0
            counted = 0
            for hand, _ in worlds:
                if not hand:
                    continue
                acc = 0.0
                for opponent_tile in hand:
                    delta = _sign(tile - opponent_tile)
                    acc += delta + belief_value(
                        rest,
                        max(0, kb - (1 if opponent_tile % 2 == 0 else 0)),
                        max(0, kw - (1 if opponent_tile % 2 == 1 else 0)),
                        rounds,
                        delta >= 0,
                        depth,
                        model,
                    )
                total += acc / len(hand)
                counted += 1
            if counted:
                return total / counted
            return _tail_value(hand=rest, kb=kb, kw=kw, rounds=rounds)
        pairs = []
        for opponent_tile, weight in opponent_marginal(kb, kw):
            delta = _sign(tile - opponent_tile)
            pairs.append(
                (
                    delta
                    + belief_value(
                        rest,
                        max(0, kb - (1 if opponent_tile % 2 == 0 else 0)),
                        max(0, kw - (1 if opponent_tile % 2 == 1 else 0)),
                        rounds,
                        delta >= 0,
                        depth,
                        model,
                    ),
                    weight,
                )
            )
        return _aggregate(pairs, model)


# ---------------------------------------------------------------------------
# 팩토리
# ---------------------------------------------------------------------------
def create_ai(
    difficulty: Optional[str] = "normal",
    *,
    seed: Optional[int] = None,
    **options: Any,
) -> BaseAI:
    """난이도에 맞는 AI 인스턴스를 만든다.

    Parameters
    ----------
    difficulty:
        ``easy`` / ``normal`` / ``hard`` (별칭: ``쉬움``·``보통``·``어려움``·``medium``).
    seed:
        샘플링·무작위 시드(재현용).
    **options:
        각 AI 클래스의 생성자 옵션(``samples``, ``lookahead`` 등).
    """

    resolved = normalize_difficulty(difficulty)
    if resolved == EASY:
        return RandomAI(seed=seed, **options)
    if resolved == NORMAL:
        return HeuristicAI(seed=seed, **options)
    return SearchAI(seed=seed, **options)
