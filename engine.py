"""「흑과 백 (Black & White)」 순수 게임 엔진.

이 모듈은 UI·네트워크·저장소를 전혀 모르는 순수 파이썬 모듈입니다.
FastAPI 서버는 '딜러'로서 이 엔진만 호출하고, RL/Self-Play 는
``reset()`` · ``legal_actions(player)`` · ``step(player, tile)`` 표준
인터페이스로 같은 엔진을 그대로 재사용합니다.

규칙 요약 (노션 「게임 룰」 명세 준수)
------------------------------------------------
- 양 플레이어가 0~8 타일 9장(짝수=흑 5장, 홀수=백 4장)을 보유한다.
- 한 세트 = 9라운드. 매 라운드 선공이 1장을 내고 그 **색만** 공개되며,
  이어 후공이 1장을 내고 역시 **색만** 공개된다. 숫자를 아는 것은 딜러(=이 엔진)뿐이다.
- 숫자가 큰 쪽이 승점 +1, 같으면 무승부(양측 0점).
- 다음 라운드 선공은 직전 라운드의 승자. 무승부면 직전 선공이 그대로 유지된다.
- 라운드 종료 시점에 ``|scoreA - scoreB| > (9 - current_round)`` 이면
  남은 라운드를 모두 이겨도 역전·동점이 불가능하므로 즉시 종료한다(조기 종료).
- 9라운드 종료 후 점수가 같으면 연장전(``set_index`` 1, 2, ...)을 치른다.
  연장전은 타일 0~8을 새로 지급하고 점수를 0:0 으로 되돌린다.
- 0이 8을 이기는 예외 규칙은 없다. 순수하게 숫자 대소 비교만 한다.

정보 은닉
------------------------------------------------
- ``view_for(player)`` 는 진행 중(``WAITING_FIRST`` / ``WAITING_SECOND`` /
  ``ROUND_RESULT``)에는 상대의 실제 타일 숫자를 절대 담지 않는다.
  상대 정보는 제출한 타일의 **색상**과 남은 **흑/백 개수**뿐이다.
- ``GAME_OVER`` 상태에서만 ``replay`` 로 전체 라운드의 실제 숫자를 공개한다.
- ``full_state()`` 는 딜러 전용 내부 상태이므로 클라이언트로 절대 보내지 않는다.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ENGINE_VERSION",
    "engine_version",
    "Phase",
    "PLAYER_A",
    "PLAYER_B",
    "PLAYERS",
    "ROUNDS_PER_SET",
    "TILES",
    "BLACK_TILES",
    "WHITE_TILES",
    "COLOR_BLACK",
    "COLOR_WHITE",
    "DRAW",
    "tile_color",
    "BlackWhiteGame",
    "RoundRecord",
    "SetRecord",
    "EngineError",
    "UnknownPlayerError",
    "InvalidTileError",
    "NotYourTurnError",
    "WrongPhaseError",
    "StaleRequestError",
    "DuplicateSubmissionError",
    "TileAlreadyUsedError",
]

#: 엔진(규칙) 버전. 게임 로그에 함께 남겨 규칙 수정 전후 데이터를 구분한다.
ENGINE_VERSION = "1.0.0"
engine_version = ENGINE_VERSION

PLAYER_A = "A"
PLAYER_B = "B"
PLAYERS: Tuple[str, str] = (PLAYER_A, PLAYER_B)

#: 한 세트의 라운드 수(정규 9라운드, 연장전도 동일).
ROUNDS_PER_SET = 9

#: 보유 타일 0~8 (사용한 타일은 영구 소멸).
TILES: Tuple[int, ...] = tuple(range(9))
#: 흑색 타일 = 짝수 {0, 2, 4, 6, 8} (5장)
BLACK_TILES: Tuple[int, ...] = tuple(t for t in TILES if t % 2 == 0)
#: 백색 타일 = 홀수 {1, 3, 5, 7} (4장)
WHITE_TILES: Tuple[int, ...] = tuple(t for t in TILES if t % 2 == 1)

COLOR_BLACK = "black"
COLOR_WHITE = "white"

#: 무승부 표기 (라운드 레코드의 winner 값).
DRAW = "draw"


# ---------------------------------------------------------------------------
# 예외
# ---------------------------------------------------------------------------
class EngineError(Exception):
    """게임 엔진 규칙 위반 예외의 공통 상위 클래스."""


class UnknownPlayerError(EngineError):
    """존재하지 않는 플레이어 식별자."""


class InvalidTileError(EngineError):
    """0~8 범위의 정수가 아닌 타일 값."""


class NotYourTurnError(EngineError):
    """지금 제출 차례가 아닌 플레이어의 제출."""


class WrongPhaseError(EngineError):
    """현재 상태 머신 단계에서 허용되지 않는 동작."""


class StaleRequestError(EngineError):
    """이미 지난 세트/라운드를 가리키는(또는 형식이 잘못된) 요청."""


class DuplicateSubmissionError(EngineError):
    """같은 라운드에 다른 타일로 두 번 제출."""


class TileAlreadyUsedError(EngineError):
    """이미 사용했거나 보유하지 않은 타일 제출."""


# ---------------------------------------------------------------------------
# 내부 레코드
# ---------------------------------------------------------------------------
@dataclass
class RoundRecord:
    """완료된 라운드 1건의 딜러 기록(숫자 포함 — 절대 클라이언트로 보내지 않는다)."""

    set_index: int
    round: int
    first: str
    second: str
    first_tile: int
    second_tile: int
    winner: str  # 플레이어 id 또는 DRAW
    scores_after: Dict[str, int]


@dataclass
class SetRecord:
    """완료된 세트 1건의 기록(본 게임 0, 연장전 1, 2, ...)."""

    set_index: int
    scores: Dict[str, int]
    winner: Optional[str]  # 무승부로 끝난 세트(연장전 진입)면 None
    end_reason: Optional[str]  # "early_win" | "normal" | "drawn"


class Phase(str, Enum):
    """게임 상태 머신.

    ``WAITING_FIRST``  → 선공 타일 제출 대기
    ``WAITING_SECOND`` → 선공 색상 공개 후, 후공 타일 제출 대기
    ``ROUND_RESULT``   → 판정 완료. 결과 확인 대기(양측이 결과를 인지할 시간)
    ``GAME_OVER``      → 승부 확정. 복기(replay) 데이터 공개
    """

    WAITING_FIRST = "WAITING_FIRST"
    WAITING_SECOND = "WAITING_SECOND"
    ROUND_RESULT = "ROUND_RESULT"
    GAME_OVER = "GAME_OVER"


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def tile_color(tile: int) -> str:
    """타일 숫자의 색상(흑/백)을 돌려준다. 짝수=흑, 홀수=백."""

    if isinstance(tile, bool) or not isinstance(tile, int) or tile not in TILES:
        raise InvalidTileError(f"타일은 0~8 사이의 정수여야 합니다: {tile!r}")
    return COLOR_BLACK if tile % 2 == 0 else COLOR_WHITE


def _count_black(tiles: Sequence[int]) -> int:
    return sum(1 for t in tiles if t % 2 == 0)


def _check_player_id(player: str) -> None:
    if player not in PLAYERS:
        raise UnknownPlayerError(f"플레이어는 {PLAYERS} 중 하나여야 합니다: {player!r}")


def _check_tile_value(tile: int) -> None:
    if isinstance(tile, bool) or not isinstance(tile, int) or tile not in TILES:
        raise InvalidTileError(f"타일은 0~8 사이의 정수여야 합니다: {tile!r}")


def _opponent_of(player: str) -> str:
    return PLAYER_B if player == PLAYER_A else PLAYER_A


# ---------------------------------------------------------------------------
# 엔진
# ---------------------------------------------------------------------------
class BlackWhiteGame:
    """「흑과 백」 한 판(본 게임 + 연장전 세트들)을 진행하는 순수 상태 머신.

    Parameters
    ----------
    first_player:
        ``"A"`` 또는 ``"B"`` 를 주면 1라운드 선공을 고정한다(테스트·방장 선택용).
        ``None`` 이면 코인 토스처럼 무작위로 정한다.
    rng:
        무작위 선공 결정에 쓸 ``random.Random`` 인스턴스. 재현 가능한 테스트를 위해
        시드를 넣은 인스턴스를 넘길 수 있다.
    """

    def __init__(
        self,
        first_player: Optional[str] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        if first_player is not None:
            _check_player_id(first_player)
        self._default_first_player = first_player
        self._rng = rng if rng is not None else random.Random()

        # reset() 이 채우는 상태 (선언은 __init__ 에서 한 번)
        self._hands: Dict[str, List[int]] = {p: list(TILES) for p in PLAYERS}
        self._set_scores: Dict[str, int] = {p: 0 for p in PLAYERS}
        self._set_index: int = 0
        self._round: int = 1
        self._first: str = PLAYER_A
        self._submitted: Dict[str, Optional[int]] = {p: None for p in PLAYERS}
        self._rounds: List[RoundRecord] = []
        self._sets: List[SetRecord] = []
        self._round_result: Optional[Dict[str, object]] = None
        self._phase: Phase = Phase.WAITING_FIRST
        self._winner: Optional[str] = None
        self._end_reason: Optional[str] = None

        self.reset()

    # -- 생성/초기화 ---------------------------------------------------------
    def reset(self, *, first_player: Optional[str] = None) -> "BlackWhiteGame":
        """새 게임으로 되돌린다(세트 0, 1라운드, 타일 0~8, 점수 0:0).

        ``first_player`` 를 주지 않으면 생성자에 넘긴 값(있으면)을 쓰고,
        그것도 없으면 무작위(코인 토스)로 선공을 정한다.
        """

        if first_player is not None:
            _check_player_id(first_player)
        resolved_first = first_player or self._default_first_player
        if resolved_first is None:
            resolved_first = self._rng.choice(PLAYERS)

        self._hands = {p: list(TILES) for p in PLAYERS}
        self._set_scores = {p: 0 for p in PLAYERS}
        self._set_index = 0
        self._round = 1
        self._first = resolved_first
        self._submitted = {p: None for p in PLAYERS}
        self._rounds = []
        self._sets = []
        self._round_result = None
        self._phase = Phase.WAITING_FIRST
        self._winner = None
        self._end_reason = None
        return self

    # -- 읽기 전용 상태 ------------------------------------------------------
    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def set_index(self) -> int:
        """세트 번호. 본 게임 0, 연장전 1, 2, ..."""

        return self._set_index

    @property
    def round(self) -> int:
        """현재(또는 마지막으로 진행한) 세트 안의 라운드 번호 1~9."""

        return self._round

    @property
    def rounds_played(self) -> int:
        return len(self._rounds)

    @property
    def first_player(self) -> str:
        return self._first

    @property
    def second_player(self) -> str:
        return _opponent_of(self._first)

    @property
    def current_player(self) -> Optional[str]:
        """지금 타일을 내야 하는 플레이어. 제출 대기가 아니면 ``None``."""

        if self._phase is Phase.WAITING_FIRST:
            return self._first
        if self._phase is Phase.WAITING_SECOND:
            return self.second_player
        return None

    @property
    def turn(self) -> Optional[str]:
        """``current_player`` 의 별칭(RL/서버 양쪽에서 쓰기 쉬운 이름)."""

        return self.current_player

    @property
    def scores(self) -> Dict[str, int]:
        """현재 세트의 누적 승점(연장전이면 0:0 부터 다시 센다)."""

        return dict(self._set_scores)

    @property
    def winner(self) -> Optional[str]:
        """확정된 최종 승자. ``GAME_OVER`` 전에는 ``None``."""

        return self._winner

    @property
    def end_reason(self) -> Optional[str]:
        """``"normal"``(9라운드 종료) 또는 ``"early_win"``(조기 종료)."""

        return self._end_reason

    @property
    def is_over(self) -> bool:
        return self._phase is Phase.GAME_OVER

    def remaining_tiles(self, player: str) -> List[int]:
        """해당 플레이어의 남은 타일(숫자). 딜러/본인 시점에서만 쓴다."""

        _check_player_id(player)
        return sorted(self._hands[player])

    # -- 제출 ----------------------------------------------------------------
    def submit_tile(
        self,
        player: str,
        tile: int,
        *,
        set_index: Optional[int] = None,
        round_number: Optional[int] = None,
    ) -> Dict[str, object]:
        """``player`` 가 이번 라운드에 낼 ``tile`` 을 제출한다.

        서버는 클라이언트 요청의 ``set_index`` / ``round_number`` 를 함께 넘겨
        **Idempotency**(중복·지연 패킷 방어)를 얻는다. 이미 처리된 요청이
        같은 타일로 다시 오면 상태를 바꾸지 않고 ``duplicate=True`` 로 돌려준다.

        Raises
        ------
        UnknownPlayerError, InvalidTileError, NotYourTurnError, WrongPhaseError,
        StaleRequestError, DuplicateSubmissionError, TileAlreadyUsedError
        """

        _check_player_id(player)
        _check_tile_value(tile)

        # 1) 명시적 set/round 가 붙은 재전송: 기록과 정확히 일치하면 중복으로 흡수.
        if set_index is not None or round_number is not None:
            req_set = self._set_index if set_index is None else set_index
            req_round = self._round if round_number is None else round_number
            previous = self._submitted_tile_in(player, req_set, req_round)
            if previous is not None and previous == tile:
                return self._event(player, tile, duplicate=True)
            self._validate_request_ids(set_index, round_number)

        # 2) 지금 라운드에 이미 제출한 경우(두 번째 요청).
        live = self._submitted[player]
        if live is not None:
            if live == tile:
                return self._event(player, tile, duplicate=True)
            raise DuplicateSubmissionError(
                f"{player} 는 이번 라운드({self._set_index}-{self._round})에 이미 "
                f"타일 {live} 를 냈습니다. 다른 타일 {tile} 로 다시 낼 수 없습니다."
            )

        # 3) 상태 머신 / 턴 / 보유 타일 검증.
        if self._phase is Phase.GAME_OVER:
            raise WrongPhaseError("게임이 끝났습니다(GAME_OVER). 더 이상 제출할 수 없습니다.")
        if self._phase is Phase.ROUND_RESULT:
            raise WrongPhaseError(
                "라운드 판정 결과를 확인하는 단계(ROUND_RESULT)입니다. "
                "advance_round() 로 다음 라운드로 넘긴 뒤 제출하세요."
            )
        if player != self.current_player:
            raise NotYourTurnError(
                f"지금은 {self.current_player} 의 차례입니다. {player} 는 낼 수 없습니다."
            )
        if tile not in self._hands[player]:
            raise TileAlreadyUsedError(
                f"{player} 는 타일 {tile} 을 보유하고 있지 않습니다(이미 사용)."
            )

        # 4) 실제 반영.
        self._hands[player].remove(tile)
        self._submitted[player] = tile

        if self._phase is Phase.WAITING_FIRST:
            self._phase = Phase.WAITING_SECOND
            return self._event(player, tile)

        record = self._resolve_round()
        return self._event(player, tile, record=record)

    # -- 라운드 결과 -> 다음 라운드 ------------------------------------------
    def advance_round(self) -> Dict[str, object]:
        """``ROUND_RESULT`` 를 확인하고 다음 라운드/세트/게임 종료로 넘어간다.

        Returns
        -------
        dict
            새 상태 요약(``phase``, ``set_index``, ``round``, ``turn``,
            ``started_new_set``, ``game_over``, ``winner``, ``end_reason``).
        """

        if self._phase is not Phase.ROUND_RESULT:
            raise WrongPhaseError(
                f"ROUND_RESULT 상태에서만 호출할 수 있습니다(현재 {self._phase.value})."
            )

        decision = self._round_result or {}
        next_step = decision.get("next")
        started_new_set = False

        if next_step == "game_over":
            set_winner = decision.get("winner")
            reason = decision.get("end_reason")
            self._sets.append(
                SetRecord(
                    set_index=self._set_index,
                    scores=dict(self._set_scores),
                    winner=set_winner if isinstance(set_winner, str) else None,
                    end_reason=reason if isinstance(reason, str) else None,
                )
            )
            self._winner = set_winner if isinstance(set_winner, str) else None
            self._end_reason = reason if isinstance(reason, str) else None
            self._phase = Phase.GAME_OVER
            self._submitted = {p: None for p in PLAYERS}
            self._round_result = decision
        elif next_step == "new_set":
            # 9라운드 동점 → 연장전. 기록은 세트별로 분리 보존한다.
            self._sets.append(
                SetRecord(
                    set_index=self._set_index,
                    scores=dict(self._set_scores),
                    winner=None,
                    end_reason="drawn",
                )
            )
            last = self._rounds[-1]
            # 연장전 1라운드 선공: 직전 세트 마지막 라운드 승자.
            # (무승부로 끝났다면 「무승부 시 선공 유지」 규칙을 그대로 이어받는다.)
            next_first = last.first if last.winner == DRAW else last.winner
            self._set_index += 1
            self._round = 1
            self._hands = {p: list(TILES) for p in PLAYERS}
            self._set_scores = {p: 0 for p in PLAYERS}
            self._first = next_first
            self._submitted = {p: None for p in PLAYERS}
            self._round_result = None
            self._phase = Phase.WAITING_FIRST
            started_new_set = True
        else:
            # 다음 라운드: 직전 승자가 선공, 무승부면 선공 유지.
            winner = decision.get("round_winner")
            if isinstance(winner, str) and winner != DRAW:
                self._first = winner
            self._round += 1
            self._submitted = {p: None for p in PLAYERS}
            self._round_result = None
            self._phase = Phase.WAITING_FIRST

        return {
            "phase": self._phase.value,
            "set_index": self._set_index,
            "round": self._round,
            "turn": self.current_player,
            "first_player": self._first,
            "second_player": self.second_player,
            "set_scores": dict(self._set_scores),
            "started_new_set": started_new_set,
            "game_over": self.is_over,
            "winner": self._winner,
            "end_reason": self._end_reason,
        }

    # 서버 코드에서 의미가 분명한 별칭.
    def confirm_round_result(self) -> Dict[str, object]:
        """``advance_round()`` 의 별칭."""

        return self.advance_round()

    # -- 관측(view) ----------------------------------------------------------
    def view_for(self, player: str) -> Dict[str, object]:
        """``player`` 시점의 공개 뷰를 만든다.

        진행 중에는 상대의 숫자를 절대 담지 않는다. 상대 정보는 제출 색상과
        남은 흑/백 개수뿐이며, ``GAME_OVER`` 에서만 ``replay`` 로 전체 숫자가 열린다.
        반환값은 그대로 ``json.dumps`` 가능한 순수 dict 이다.
        """

        _check_player_id(player)
        opponent = _opponent_of(player)
        own_hand = self._hands[player]
        opp_hand = self._hands[opponent]

        own_tile = self._submitted[player]
        opp_tile = self._submitted[opponent]

        view: Dict[str, object] = {
            "engine_version": ENGINE_VERSION,
            "viewer": player,
            "phase": self._phase.value,
            "set_index": self._set_index,
            "round": self._round,
            "rounds_per_set": ROUNDS_PER_SET,
            "first_player": self._first,
            "second_player": self.second_player,
            "turn": self.current_player,
            "scores": dict(self._set_scores),
            "you": {
                "id": player,
                "remaining": sorted(own_hand),
                "remaining_black": _count_black(own_hand),
                "remaining_white": len(own_hand) - _count_black(own_hand),
                "submitted": own_tile,
                "submitted_color": None if own_tile is None else tile_color(own_tile),
            },
            "opponent": {
                "id": opponent,
                # 상대 남은 패는 '개수'만 공개한다(숫자 목록은 절대 포함하지 않는다).
                "remaining_black": _count_black(opp_hand),
                "remaining_white": len(opp_hand) - _count_black(opp_hand),
                "submitted_color": None if opp_tile is None else tile_color(opp_tile),
            },
            "rounds": [
                self._public_round(record, player)
                for record in self._rounds
                if record.set_index == self._set_index
            ],
            "completed_sets": [
                {
                    "set_index": s.set_index,
                    "scores": dict(s.scores),
                    "winner": s.winner,
                    "end_reason": s.end_reason,
                }
                for s in self._sets
            ],
            "round_result": self._public_round_result(),
            "decided": self._is_decided(),
            "game_over": self._public_game_over() if self.is_over else None,
            "replay": self._replay() if self.is_over else None,
        }
        return view

    def legal_actions(self, player: str) -> List[int]:
        """``player`` 가 지금 낼 수 있는 타일 목록(오름차순).

        제출 차례가 아니거나 ``ROUND_RESULT``/``GAME_OVER`` 이면 빈 리스트.
        RL/Self-Play 에서 그대로 정책의 후보 집합으로 쓴다.
        """

        _check_player_id(player)
        if player != self.current_player:
            return []
        return sorted(self._hands[player])

    def step(self, player: str, tile: int) -> Dict[str, object]:
        """RL 표준 인터페이스: 한 수를 두고 그 플레이어 시점의 다음 관측을 돌려준다.

        Returns
        -------
        dict
            ``view`` (다음 관측), ``reward`` (이번 수로 라운드가 끝났을 때
            승 +1.0 / 무 0.0 / 패 -1.0, 아니면 0.0), ``done`` (게임 종료 여부),
            ``info`` (제출 이벤트), ``turn``, ``legal_actions``.

        Notes
        -----
        라운드가 끝나면 ``ROUND_RESULT`` 로 멈춘다. 결과 확인 단계를 관측하고
        싶지 않은 학습 루프는 ``advance_round()`` 를 이어서 호출하면 된다.
        """

        info = self.submit_tile(player, tile)

        reward = 0.0
        if info.get("round_completed"):
            round_winner = info.get("round_winner")
            if round_winner == player:
                reward = 1.0
            elif round_winner == DRAW:
                reward = 0.0
            else:
                reward = -1.0

        next_turn = self.current_player
        return {
            "view": self.view_for(player),
            "reward": reward,
            "done": self.is_over,
            "info": info,
            "turn": next_turn,
            "legal_actions": [] if next_turn is None else self.legal_actions(next_turn),
        }

    # -- 딜러 전용 전체 상태 -------------------------------------------------
    def full_state(self) -> Dict[str, object]:
        """숫자까지 포함한 전체 내부 상태(로그 저장·테스트·복기용).

        **딜러 전용**. 이 값은 클라이언트(브라우저)로 절대 전송하지 않는다.
        """

        return {
            "engine_version": ENGINE_VERSION,
            "phase": self._phase.value,
            "set_index": self._set_index,
            "round": self._round,
            "first_player": self._first,
            "second_player": self.second_player,
            "scores": dict(self._set_scores),
            "hands": {p: sorted(self._hands[p]) for p in PLAYERS},
            "submitted": dict(self._submitted),
            "rounds": [asdict(record) for record in self._rounds],
            "sets": [asdict(s) for s in self._sets],
            "pending": dict(self._round_result) if self._round_result else None,
            "winner": self._winner,
            "end_reason": self._end_reason,
        }

    # -- 내부 구현 -----------------------------------------------------------
    def _validate_request_ids(
        self, set_index: Optional[int], round_number: Optional[int]
    ) -> None:
        if set_index is not None:
            if (
                isinstance(set_index, bool)
                or not isinstance(set_index, int)
                or set_index < 0
            ):
                raise StaleRequestError(f"set_index 가 올바르지 않습니다: {set_index!r}")
            if set_index != self._set_index:
                raise StaleRequestError(
                    f"set_index 가 현재 세트와 다릅니다(요청 {set_index}, 현재 {self._set_index})."
                )
        if round_number is not None:
            if (
                isinstance(round_number, bool)
                or not isinstance(round_number, int)
                or not 1 <= round_number <= ROUNDS_PER_SET
            ):
                raise StaleRequestError(
                    f"round_number 가 올바르지 않습니다: {round_number!r}"
                )
            if round_number != self._round:
                raise StaleRequestError(
                    f"round_number 가 현재 라운드와 다릅니다"
                    f"(요청 {round_number}, 현재 {self._round})."
                )

    def _submitted_tile_in(
        self, player: str, set_index: int, round_number: int
    ) -> Optional[int]:
        """해당 세트·라운드에서 ``player`` 가 낸 타일(기록 + 진행 중 제출)."""

        for record in self._rounds:
            if record.set_index == set_index and record.round == round_number:
                return record.first_tile if player == record.first else record.second_tile
        if set_index == self._set_index and round_number == self._round:
            return self._submitted[player]
        return None

    def _resolve_round(self) -> RoundRecord:
        """두 타일을 비교해 승패·점수를 확정하고 ``ROUND_RESULT`` 로 전이한다."""

        first, second = self._first, self.second_player
        tile_first = self._submitted[first]
        tile_second = self._submitted[second]
        assert tile_first is not None and tile_second is not None

        if tile_first > tile_second:
            round_winner = first
        elif tile_first < tile_second:
            round_winner = second
        else:
            round_winner = DRAW

        if round_winner != DRAW:
            self._set_scores[round_winner] += 1

        record = RoundRecord(
            set_index=self._set_index,
            round=self._round,
            first=first,
            second=second,
            first_tile=tile_first,
            second_tile=tile_second,
            winner=round_winner,
            scores_after=dict(self._set_scores),
        )
        self._rounds.append(record)
        # 이번 라운드 승자(다음 라운드 선공 결정용) + 세트 종료 판정을 함께 보관한다.
        self._round_result = {
            "round_winner": round_winner,
            **self._judge_after_round(),
        }
        self._phase = Phase.ROUND_RESULT
        return record

    def _judge_after_round(self) -> Dict[str, object]:
        """라운드 종료 시점의 조기 종료/세트 종료/계속 판정.

        명세: ``|scoreA - scoreB| > (9 - current_round)`` 이면 즉시 종료.
        부등호가 **초과(>)** 이므로, 남은 라운드를 모두 이겨 '동점'까지 갈 수
        있는 경우(차이 == 남은 라운드)는 끝내지 않고 계속 진행한다.
        """

        score_a = self._set_scores[PLAYER_A]
        score_b = self._set_scores[PLAYER_B]
        remaining = ROUNDS_PER_SET - self._round
        leader: Optional[str] = None
        if score_a > score_b:
            leader = PLAYER_A
        elif score_b > score_a:
            leader = PLAYER_B

        if remaining > 0 and leader is not None and abs(score_a - score_b) > remaining:
            return {
                "decided": True,
                "winner": leader,
                "end_reason": "early_win",
                "next": "game_over",
            }
        if remaining == 0:
            if leader is None:
                return {
                    "decided": False,
                    "winner": None,
                    "end_reason": None,
                    "next": "new_set",
                }
            return {
                "decided": True,
                "winner": leader,
                "end_reason": "normal",
                "next": "game_over",
            }
        return {
            "decided": False,
            "winner": None,
            "end_reason": None,
            "next": "next_round",
        }

    def _public_round(self, record: RoundRecord, viewer: str) -> Dict[str, object]:
        """관측자 시점의 완료 라운드 공개 정보.

        본인 숫자(``your_tile``)는 공개하지만, 상대 숫자는 **어떤 키로도**
        담지 않는다(상대는 ``opponent_color`` 뿐).
        """

        your_tile = record.first_tile if viewer == record.first else record.second_tile
        opponent_tile = (
            record.second_tile if viewer == record.first else record.first_tile
        )
        return {
            "round": record.round,
            "first_player": record.first,
            "second_player": record.second,
            "you_were_first": record.first == viewer,
            "your_tile": your_tile,
            "your_color": tile_color(your_tile),
            "opponent_color": tile_color(opponent_tile),
            "winner": record.winner,
            "scores": dict(record.scores_after),
        }

    def _public_round_result(self) -> Optional[Dict[str, object]]:
        if self._phase is not Phase.ROUND_RESULT or self._round_result is None:
            return None
        return {
            "round": self._round,
            "winner": self._round_result.get("round_winner"),
            "scores": dict(self._set_scores),
            "first_color": tile_color(self._submitted[self._first])
            if self._submitted[self._first] is not None
            else None,
            "second_color": tile_color(self._submitted[self.second_player])
            if self._submitted[self.second_player] is not None
            else None,
            "decided": bool(self._round_result.get("decided")),
            "set_winner": self._round_result.get("winner"),
            "end_reason": self._round_result.get("end_reason"),
            "next": self._round_result.get("next"),
        }

    def _is_decided(self) -> bool:
        if self.is_over:
            return True
        if self._phase is Phase.ROUND_RESULT and self._round_result is not None:
            return bool(self._round_result.get("decided"))
        return False

    def _public_game_over(self) -> Optional[Dict[str, object]]:
        if not self.is_over:
            return None
        return {
            "winner": self._winner,
            "loser": _opponent_of(self._winner) if self._winner else None,
            "end_reason": self._end_reason,
            "scores": dict(self._set_scores),
            "set_index": self._set_index,
            "total_sets": len(self._sets),
            "sets": [
                {
                    "set_index": s.set_index,
                    "scores": dict(s.scores),
                    "winner": s.winner,
                    "end_reason": s.end_reason,
                }
                for s in self._sets
            ],
            # 복기용: 쓰지 않고 남은 타일도 공개한다.
            "final_hands": {p: sorted(self._hands[p]) for p in PLAYERS},
        }

    def _replay(self) -> Optional[List[Dict[str, object]]]:
        if not self.is_over:
            return None
        return [
            {
                "set_index": record.set_index,
                "round": record.round,
                "first_player": record.first,
                "second_player": record.second,
                "first_tile": record.first_tile,
                "second_tile": record.second_tile,
                "first_color": tile_color(record.first_tile),
                "second_color": tile_color(record.second_tile),
                "winner": record.winner,
                "scores": dict(record.scores_after),
            }
            for record in self._rounds
        ]

    def _event(
        self,
        player: str,
        tile: int,
        *,
        duplicate: bool = False,
        record: Optional[RoundRecord] = None,
    ) -> Dict[str, object]:
        """제출 결과 이벤트(제출한 플레이어에게 돌려줄 요약)."""

        if record is None and duplicate:
            for candidate in self._rounds:
                if (
                    candidate.set_index == self._set_index
                    and candidate.round == self._round
                ):
                    record = candidate
                    break
        return {
            "accepted": True,
            "duplicate": duplicate,
            "player": player,
            "tile": tile,
            "phase": self._phase.value,
            "set_index": self._set_index,
            "round_number": self._round,
            "round_completed": record is not None,
            "round_winner": record.winner if record is not None else None,
            "scores": dict(self._set_scores),
            "first_player": self._first,
            "second_player": self.second_player,
            "turn": self.current_player,
            "game_over": self.is_over,
        }

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의용
        return (
            f"<BlackWhiteGame v{ENGINE_VERSION} phase={self._phase.value} "
            f"set={self._set_index} round={self._round} "
            f"scores={self._set_scores} first={self._first}>"
        )
