/* 「흑과 백」 클라이언트.
 *
 * - WebSocket 주소는 현재 도메인 기준으로 자동 계산한다(터널/호스팅에서도 그대로).
 * - 플레이어 토큰은 sessionStorage 에 방 코드별 키(bw_token_${roomId})로 저장한다.
 *   → 한 브라우저에 탭 2개를 열어 혼자 테스트해도 토큰이 서로 덮어쓰지 않는다.
 * - onclose 시 1초 → 2초 → 4초 백오프로 자동 재연결하고, 15초마다 ping 을 보낸다.
 * - 타일은 [터치해 선택(강조)] → [제출하기] 2단계로 낸다(모바일 오터치 방지).
 */
(function () {
  'use strict';

  var PING_INTERVAL_MS = 15000;
  var PONG_TIMEOUT_MS = 45000;
  var RECONNECT_DELAYS = [1000, 2000, 4000];
  var DEFAULT_ROUND_RESULT_SECONDS = 2.5;

  var app = {
    roomId: null,
    token: null,
    player: null,
    mode: 'human',
    difficulty: null,
    view: null,
    meta: {},
    ws: null,
    selectedTile: null,
    submitting: false,
    reconnectAttempt: 0,
    reconnectTimer: null,
    pingTimer: null,
    lastPongAt: 0,
    fatal: false,
    userLeft: false,
    screen: 'home',
    rrTimer: null,
    creating: false
  };

  var el = {};

  // ── 유틸 ────────────────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }

  function colorLabel(color) {
    if (color === 'black') return '흑';
    if (color === 'white') return '백';
    return '–';
  }

  function colorClass(color) {
    return color === 'white' ? 'is-white' : 'is-black';
  }

  function tokenKey(roomId) { return 'bw_token_' + roomId; }

  function saveToken(roomId, token) {
    try { sessionStorage.setItem(tokenKey(roomId), token); } catch (e) { /* 사생활 보호 모드 */ }
  }

  function loadToken(roomId) {
    try { return sessionStorage.getItem(tokenKey(roomId)); } catch (e) { return null; }
  }

  function clearToken(roomId) {
    try { sessionStorage.removeItem(tokenKey(roomId)); } catch (e) { /* noop */ }
  }

  var toastTimer = null;
  function toast(message) {
    var box = el.toast;
    box.textContent = message;
    box.hidden = false;
    // 강제 리플로우 후 클래스 부여 → 애니메이션 재생
    void box.offsetWidth;
    box.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      box.classList.remove('is-visible');
      setTimeout(function () { box.hidden = true; }, 220);
    }, 2400);
  }

  function show(name) {
    if (app.screen === name) return;
    app.screen = name;
    document.querySelectorAll('.screen').forEach(function (section) {
      section.classList.toggle('is-active', section.dataset.screen === name);
    });
    window.scrollTo(0, 0);
  }

  function setConn(text, ok) {
    if (el.connState) {
      el.connState.textContent = text;
      el.connState.classList.toggle('is-off', !ok);
    }
  }

  // ── 방 만들기 / 입장 ────────────────────────────────────────────────
  function createRoom(mode, difficulty) {
    if (app.creating) return;
    var isAi = mode === 'ai';
    app.creating = true;
    setConn('연결 중…', false);
    fetch('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(isAi ? { mode: 'ai', difficulty: difficulty } : { mode: 'human' })
    })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, status: res.status, body: body }; });
      })
      .then(function (out) {
        if (!out.ok) {
          var detail = out.body && out.body.detail;
          if (detail && detail.code === 'INVALID_DIFFICULTY') {
            toast('난이도를 다시 골라 주세요.');
            show('difficulty');
            return;
          }
          toast((detail && detail.message) || '방을 만들지 못했습니다.');
          return;
        }
        var data = out.body || {};
        app.mode = data.mode || (isAi ? 'ai' : 'human');
        app.difficulty = data.difficulty || difficulty || null;
        enterRoom(data.room_id, data.token);
      })
      .catch(function () { toast('서버에 연결할 수 없습니다.'); })
      .finally(function () { app.creating = false; });
  }

  function enterRoom(roomId, token) {
    app.roomId = roomId;
    app.token = token || loadToken(roomId) || null;
    app.view = null;
    app.selectedTile = null;
    app.submitting = false;
    app.fatal = false;
    app.userLeft = false;
    app.reconnectAttempt = 0;
    if (token) saveToken(roomId, token);
    renderRoomShell();
    if (app.mode === 'ai') {
      // AI 대전은 초대 링크가 필요 없다 → 바로 게임 화면으로.
      show('game');
    } else {
      show('room');
    }
    // 새로고침해도 같은 방으로 돌아오도록 주소에 방 코드를 남긴다.
    try {
      history.replaceState({ room: roomId }, '', '/?room=' + encodeURIComponent(roomId));
    } catch (e) { /* file:// 등에서 실패 무시 */ }
    connect();
  }

  function renderRoomShell() {
    el.roomCode.textContent = app.roomId || '------';
    var url = location.origin + '/?room=' + encodeURIComponent(app.roomId || '');
    el.inviteUrl.value = url;
  }

  // ── WebSocket ──────────────────────────────────────────────────────
  function wsUrl() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws?room=' + encodeURIComponent(app.roomId);
    if (app.token) url += '&token=' + encodeURIComponent(app.token);
    return url;
  }

  function connect() {
    if (!app.roomId || app.userLeft) return;
    clearTimeout(app.reconnectTimer);
    setConn('연결 중…', false);

    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (e) {
      scheduleReconnect();
      return;
    }
    app.ws = ws;

    ws.addEventListener('open', function () {
      app.reconnectAttempt = 0;
      app.lastPongAt = Date.now();
      setConn('연결됨', true);
      startPing();
    });

    ws.addEventListener('message', function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      handleMessage(msg);
    });

    ws.addEventListener('close', function () {
      stopPing();
      if (app.userLeft) return;
      setConn('연결 끊김 — 재연결 중…', false);
      scheduleReconnect();
    });

    ws.addEventListener('error', function () { /* close 이벤트가 뒤따른다 */ });
  }

  function scheduleReconnect() {
    if (app.userLeft || app.fatal) return;
    var delay = RECONNECT_DELAYS[Math.min(app.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    app.reconnectAttempt += 1;
    clearTimeout(app.reconnectTimer);
    app.reconnectTimer = setTimeout(connect, delay);
  }

  function startPing() {
    stopPing();
    app.pingTimer = setInterval(function () {
      if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - app.lastPongAt > PONG_TIMEOUT_MS) {
        // 응답이 없으면 죽은 연결로 보고 닫아 재연결을 유도한다.
        try { app.ws.close(); } catch (e) { /* noop */ }
        return;
      }
      send({ type: 'ping', t: Date.now() });
    }, PING_INTERVAL_MS);
  }

  function stopPing() {
    if (app.pingTimer) {
      clearInterval(app.pingTimer);
      app.pingTimer = null;
    }
  }

  function send(payload) {
    if (!app.ws || app.ws.readyState !== WebSocket.OPEN) return false;
    try {
      app.ws.send(JSON.stringify(payload));
      return true;
    } catch (e) {
      return false;
    }
  }

  // ── 메시지 처리 ────────────────────────────────────────────────────
  function handleMessage(msg) {
    if (!msg || typeof msg !== 'object') return;
    switch (msg.type) {
      case 'welcome':
        onWelcome(msg);
        break;
      case 'state':
        applyPayload(msg);
        break;
      case 'ack':
        app.submitting = false;
        break;
      case 'pong':
        app.lastPongAt = Date.now();
        break;
      case 'ping':
        send({ type: 'pong', t: msg.t });
        break;
      case 'error':
        onServerError(msg);
        break;
      default:
        break;
    }
  }

  function onWelcome(msg) {
    app.player = msg.player || app.player;
    app.roomId = msg.room || app.roomId;
    app.token = msg.token || app.token;
    if (msg.mode) app.mode = msg.mode;
    if (msg.difficulty) app.difficulty = msg.difficulty;
    if (app.roomId && app.token) saveToken(app.roomId, app.token);
    app.fatal = false;
    applyPayload(msg);
  }

  function onServerError(msg) {
    var code = msg.code || 'ERROR';
    if (code === 'ROOM_FULL') {
      app.fatal = true;
      closeSocket();
      showHomeNotice('이미 두 명이 대전 중인 방입니다. 새 방을 만들어 주세요.');
      show('home');
      return;
    }
    if (code === 'ROOM_CLOSED') {
      app.fatal = true;
      closeSocket();
      if (app.roomId) clearToken(app.roomId);
      showHomeNotice('이미 끝난 방입니다. 새 방을 만들어 초대해 주세요.');
      show('home');
      return;
    }
    if (code === 'ROOM_NOT_FOUND') {
      app.fatal = true;
      closeSocket();
      if (app.roomId) clearToken(app.roomId);
      app.roomId = null;
      showHomeNotice('방을 찾을 수 없습니다. 초대 링크가 만료되었거나 코드가 잘못되었습니다.');
      show('home');
      return;
    }
    if (code === 'INVALID_TOKEN') {
      // 저장된 토큰이 이 방의 것이 아니다 → 토큰 없이 새 좌석으로 재시도
      app.fatal = true;
      closeSocket();
      if (app.roomId) clearToken(app.roomId);
      app.token = null;
      app.fatal = false;
      app.reconnectAttempt = 0;
      connect();
      return;
    }
    toast(msg.message || '요청을 처리하지 못했습니다.');
  }

  function closeSocket() {
    stopPing();
    if (app.ws) {
      try { app.ws.close(); } catch (e) { /* noop */ }
      app.ws = null;
    }
  }

  // ── 상태 반영 ──────────────────────────────────────────────────────
  function applyPayload(p) {
    if (!p) return;
    app.player = p.you || p.player || app.player;
    app.roomId = p.room || app.roomId;
    if (p.mode) app.mode = p.mode;
    if (p.difficulty) app.difficulty = p.difficulty;
    app.meta = {
      status: p.status || 'waiting',
      mode: p.mode || app.mode,
      difficulty: p.difficulty || app.difficulty,
      ended: !!p.ended,
      endReason: p.end_reason || null,
      winner: p.winner || null,
      loser: p.loser || null,
      opponentConnected: p.opponent_connected !== false,
      opponentPresent: p.opponent_present !== false,
      opponentIsAi: !!p.opponent_is_ai || (p.mode === 'ai'),
      aiThinking: !!p.ai_thinking,
      rematchVotes: p.rematch_votes || [],
      nextVotes: p.next_votes || [],
      roundResultSeconds: typeof p.round_result_seconds === 'number'
        ? p.round_result_seconds : DEFAULT_ROUND_RESULT_SECONDS,
      seatsFilled: p.seats_filled || 1
    };
    if (p.state) app.view = p.state;
    app.submitting = false;
    render();
  }

  function render() {
    if (app.meta.ended) {
      renderOver();
      show('over');
      return;
    }
    if (app.view) {
      renderGame();
      show('game');
      return;
    }
    if (app.mode === 'ai') {
      // AI 대전은 방장 혼자이므로 초대 대기 화면 대신 게임 화면에서 기다린다.
      renderAiWaiting();
      show('game');
      return;
    }
    renderRoom();
    show('room');
  }

  function renderAiWaiting() {
    el.setLabel.textContent = '본 게임';
    el.roundLabel.textContent = '준비 중';
    el.turnBanner.textContent = 'AI 대전을 준비하고 있어요…';
    el.turnBanner.className = 'turn-banner is-waiting';
    el.firstBadge.hidden = true;
    setOpponentLabels();
    el.myTiles.innerHTML = '';
    el.btnSubmit.disabled = true;
    el.btnSubmit.textContent = '제출하기';
    el.submitHint.textContent = '조금만 기다려 주세요.';
    el.roundResult.hidden = true;
    el.oppSubmitted.hidden = true;
  }

  function renderRoom() {
    renderRoomShell();
    var waiting = document.querySelector('.waiting');
    if (waiting) {
      waiting.lastElementChild.textContent = app.meta.seatsFilled >= 2
        ? '상대가 들어왔습니다. 곧 시작합니다…'
        : '상대를 기다리는 중…';
    }
  }

  // ── 게임 화면 ──────────────────────────────────────────────────────
  var DIFFICULTY_LABEL = { easy: '쉬움', normal: '보통', hard: '어려움' };

  function isAiOpponent() {
    return !!(app.meta.opponentIsAi || app.mode === 'ai');
  }

  function opponentLabel() {
    if (isAiOpponent()) {
      var tag = DIFFICULTY_LABEL[app.difficulty] || DIFFICULTY_LABEL[app.meta.difficulty] || '';
      return tag ? 'AI · ' + tag : 'AI';
    }
    return '상대';
  }

  function setOpponentLabels() {
    var name = opponentLabel();
    if (el.oppScoreName) el.oppScoreName.textContent = name;
    if (el.finalOppName) el.finalOppName.textContent = name;
    if (el.oppPanelTitle) el.oppPanelTitle.textContent = name + ' 남은 타일';
  }

  function renderGame() {
    var v = app.view;
    if (!v) return;
    var me = v.viewer;
    var opp = v.opponent.id;
    var myScore = v.scores[me] || 0;
    var oppScore = v.scores[opp] || 0;
    var myTurn = v.turn === me;
    var phase = v.phase;

    setOpponentLabels();
    el.scoreMe.textContent = String(myScore);
    el.scoreOpp.textContent = String(oppScore);
    el.setLabel.textContent = v.set_index === 0 ? '본 게임' : '연장전 ' + v.set_index;
    el.roundLabel.textContent = v.round + ' / ' + v.rounds_per_set + ' 라운드';
    el.firstBadge.hidden = v.first_player !== me;

    el.oppBlack.textContent = String(v.opponent.remaining_black);
    el.oppWhite.textContent = String(v.opponent.remaining_white);
    el.oppStatus.className = 'conn-dot ' + (app.meta.opponentConnected ? 'on' : 'off');
    el.oppStatus.title = app.meta.opponentConnected
      ? (isAiOpponent() ? 'AI 준비됨' : '상대 연결됨')
      : '상대 연결 끊김';

    var oppSubmitted = v.opponent.submitted_color;
    if (oppSubmitted) {
      el.oppSubmitted.hidden = false;
      el.oppSubmitted.textContent = colorLabel(oppSubmitted) + ' 제출';
      el.oppSubmitted.className = 'chip-note ' + colorClass(oppSubmitted);
    } else {
      el.oppSubmitted.hidden = true;
    }

    renderTiles(v, me, myTurn);
    renderTurnBanner(v, me, myTurn);
    renderRoundResult(v, me, opp);
  }

  function renderTiles(v, me, myTurn) {
    var remaining = (v.you.remaining || []).slice();
    var alreadySubmitted = v.you.submitted !== null && v.you.submitted !== undefined;
    var canPick = myTurn && !alreadySubmitted && !app.submitting &&
      (v.phase === 'WAITING_FIRST' || v.phase === 'WAITING_SECOND');

    if (app.selectedTile !== null && remaining.indexOf(app.selectedTile) === -1) {
      app.selectedTile = null;
    }

    el.myTiles.innerHTML = '';
    if (!remaining.length) {
      var empty = document.createElement('p');
      empty.className = 'tile-empty';
      empty.textContent = alreadySubmitted ? '이번 라운드 타일을 냈습니다.' : '남은 타일이 없습니다.';
      el.myTiles.appendChild(empty);
    }

    remaining.forEach(function (tile) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tile ' + (tile % 2 === 0 ? 'is-black' : 'is-white');
      btn.dataset.tile = String(tile);
      btn.disabled = !canPick;
      if (canPick) btn.classList.add('is-mine-turn');
      if (app.selectedTile === tile) btn.classList.add('is-selected');
      btn.setAttribute('aria-pressed', app.selectedTile === tile ? 'true' : 'false');
      btn.setAttribute('aria-label', colorLabel(tile % 2 === 0 ? 'black' : 'white') + ' ' + tile);

      var num = document.createElement('span');
      num.className = 'tile-num';
      num.textContent = String(tile);
      var tag = document.createElement('span');
      tag.className = 'tile-tag';
      tag.textContent = tile % 2 === 0 ? '흑' : '백';
      btn.appendChild(tag);
      btn.appendChild(num);

      btn.addEventListener('click', function () { onTilePick(tile); });
      el.myTiles.appendChild(btn);
    });

    var submitReady = canPick && app.selectedTile !== null;
    el.btnSubmit.disabled = !submitReady;
    el.btnSubmit.textContent = app.submitting ? '제출 중…' : '제출하기';

    var hint;
    if (alreadySubmitted) {
      hint = '타일을 냈습니다. 상대를 기다리는 중…';
    } else if (!myTurn) {
      if (isAiOpponent()) {
        hint = 'AI가 타일을 고르는 중입니다…';
      } else {
        hint = v.phase === 'WAITING_FIRST'
          ? '상대(선공)가 타일을 고르는 중입니다.'
          : '상대 차례입니다.';
      }
    } else if (app.selectedTile === null) {
      hint = v.phase === 'WAITING_FIRST'
        ? '선공입니다. 낼 타일을 고르세요.'
        : '상대가 낸 색을 보고 타일을 고르세요.';
    } else {
      hint = colorLabel(app.selectedTile % 2 === 0 ? 'black' : 'white') + ' ' +
        app.selectedTile + ' 을(를) 내려면 ‘제출하기’를 누르세요.';
    }
    el.submitHint.textContent = hint;
  }

  function renderTurnBanner(v, me, myTurn) {
    var text = '';
    var mine = false;
    var thinking = false;
    if (v.phase === 'ROUND_RESULT') {
      text = '라운드 결과 확인 중…';
    } else if (v.phase === 'GAME_OVER') {
      text = '게임이 끝났습니다.';
    } else if (myTurn) {
      mine = true;
      text = v.you.submitted === null || v.you.submitted === undefined
        ? (v.phase === 'WAITING_FIRST' ? '내 차례 · 선공' : '내 차례 · 후공')
        : '제출 완료 — 상대를 기다리는 중';
    } else if (isAiOpponent()) {
      thinking = true;
      text = 'AI가 생각 중…';
    } else {
      text = v.phase === 'WAITING_FIRST' ? '상대 차례 · 선공' : '상대 차례 · 후공';
    }
    el.turnBanner.textContent = text;
    el.turnBanner.classList.toggle('is-mine', mine);
    el.turnBanner.classList.toggle('is-waiting', !mine);
    el.turnBanner.classList.toggle('is-thinking', thinking);
  }

  function renderRoundResult(v, me, opp) {
    var rr = v.round_result;
    clearTimeout(app.rrTimer);
    if (!rr) {
      el.roundResult.hidden = true;
      return;
    }
    el.roundResult.hidden = false;

    var title = '';
    var cls = 'draw';
    if (rr.winner === me) { title = '이번 라운드 승리'; cls = 'win'; }
    else if (rr.winner === 'draw') { title = '무승부'; cls = 'draw'; }
    else { title = '이번 라운드 패배'; cls = 'lose'; }
    el.rrTitle.textContent = title;
    el.rrTitle.className = 'rr-title ' + cls;

    var parts = [];
    parts.push('선공 ' + colorLabel(rr.first_color));
    parts.push('후공 ' + colorLabel(rr.second_color));
    parts.push('점수 ' + (v.scores[me] || 0) + ' : ' + (v.scores[opp] || 0));
    el.rrDetail.textContent = parts.join(' · ');

    var bar = el.rrProgress;
    var seconds = app.meta.roundResultSeconds || DEFAULT_ROUND_RESULT_SECONDS;
    bar.classList.remove('is-running');
    void bar.offsetWidth;
    bar.style.animationDuration = seconds + 's';
    bar.classList.add('is-running');

    var iVoted = (app.meta.nextVotes || []).indexOf(app.player) !== -1;
    el.btnNext.disabled = iVoted;
    el.btnNext.textContent = iVoted ? '다음 라운드 준비됨' : '다음 라운드';
  }

  // ── 게임 종료/복기 ─────────────────────────────────────────────────
  function reasonText(reason, iWon) {
    switch (reason) {
      case 'early_win': return '역전이 불가능해 조기 종료되었습니다.';
      case 'normal': return '9라운드가 모두 끝났습니다.';
      case 'forfeit': return iWon ? '상대가 기권했습니다.' : '기권으로 종료되었습니다.';
      case 'disconnect': return iWon ? '상대가 재접속하지 않아 부전승 처리되었습니다.'
        : '연결이 끊겨 부전승 처리되었습니다.';
      default: return '게임이 종료되었습니다.';
    }
  }

  function renderOver() {
    var v = app.view;
    var winner = app.meta.winner || (v && v.game_over ? v.game_over.winner : null);
    var reason = app.meta.endReason || (v && v.game_over ? v.game_over.end_reason : null);

    var me = app.player;
    var opp = v ? v.opponent.id : (me === 'A' ? 'B' : 'A');
    var iWon = !!winner && winner === me;
    var draw = !winner;

    el.overTitle.className = '';
    if (draw) {
      el.overTitle.textContent = '무승부';
      el.overTitle.classList.add('draw');
    } else if (iWon) {
      el.overTitle.textContent = '승리';
      el.overTitle.classList.add('win');
    } else {
      el.overTitle.textContent = '패배';
      el.overTitle.classList.add('lose');
    }
    el.overKicker.textContent = reason === 'forfeit' || reason === 'disconnect' ? '게임 중단' : '게임 종료';
    el.overReason.textContent = reasonText(reason, iWon);

    var scores = (v && v.scores) || {};
    setOpponentLabels();
    el.finalMe.textContent = String(scores[me] || 0);
    el.finalOpp.textContent = String(scores[opp] || 0);

    renderReplay(v, me, opp);

    if (isAiOpponent()) {
      // AI 는 투표하지 않는다 → 누르면 바로 새 판.
      el.rematchStatus.hidden = true;
      el.btnRematch.disabled = false;
      el.btnRematch.textContent = '다시 대결';
      return;
    }

    var votes = app.meta.rematchVotes || [];
    var status = el.rematchStatus;
    if (votes.indexOf(app.player) !== -1) {
      status.hidden = false;
      status.textContent = app.meta.opponentConnected
        ? '상대의 응답을 기다리는 중…'
        : '상대가 나갔습니다.';
      el.btnRematch.disabled = true;
      el.btnRematch.textContent = '재대결 요청됨';
    } else if (votes.length) {
      status.hidden = false;
      status.textContent = '상대가 재대결을 원합니다.';
      el.btnRematch.disabled = false;
      el.btnRematch.textContent = '재대결 수락';
    } else {
      status.hidden = true;
      el.btnRematch.disabled = !app.meta.opponentConnected;
      el.btnRematch.textContent = '재대결';
    }
  }

  function tilePill(tile, color) {
    var span = document.createElement('span');
    span.className = 'mini-tile ' + colorClass(color || (tile % 2 === 0 ? 'black' : 'white'));
    span.textContent = String(tile);
    return span;
  }

  function addSetRow(body, label) {
    var tr = document.createElement('tr');
    tr.className = 'set-row';
    var td = document.createElement('td');
    td.colSpan = 6;
    td.textContent = label;
    tr.appendChild(td);
    body.appendChild(tr);
  }

  function renderReplay(v, me, opp) {
    var body = el.replayBody;
    body.innerHTML = '';
    var rows = [];
    if (v && v.replay && v.replay.length) {
      rows = v.replay.map(function (r) {
        var iWasFirst = r.first_player === me;
        return {
          setIndex: r.set_index,
          round: r.round,
          first: r.first_player,
          myTile: iWasFirst ? r.first_tile : r.second_tile,
          oppTile: iWasFirst ? r.second_tile : r.first_tile,
          oppColor: iWasFirst ? r.second_color : r.first_color,
          winner: r.winner,
          myScore: r.scores[me] || 0,
          oppScore: r.scores[opp] || 0
        };
      });
      el.replayTitle.textContent = '라운드 복기 (전체 공개)';
    } else if (v && v.rounds && v.rounds.length) {
      rows = v.rounds.map(function (r) {
        return {
          setIndex: v.set_index,
          round: r.round,
          first: r.first_player,
          myTile: r.your_tile,
          oppTile: null,
          oppColor: r.opponent_color,
          winner: r.winner,
          myScore: r.scores[me] || 0,
          oppScore: r.scores[opp] || 0
        };
      });
      el.replayTitle.textContent = '라운드 기록 (상대 숫자는 공개되지 않음)';
    }

    if (!rows.length) {
      el.replayEmpty.hidden = false;
      el.replayTable.hidden = true;
      return;
    }
    el.replayEmpty.hidden = true;
    el.replayTable.hidden = false;

    var lastSet = null;
    rows.forEach(function (r) {
      if (r.setIndex !== lastSet) {
        addSetRow(body, r.setIndex === 0 ? '본 게임' : '연장전 ' + r.setIndex);
        lastSet = r.setIndex;
      }
      var tr = document.createElement('tr');

      var tdRound = document.createElement('td');
      tdRound.textContent = String(r.round);

      var tdFirst = document.createElement('td');
      tdFirst.textContent = r.first === me ? '나' : '상대';

      var tdMine = document.createElement('td');
      var cellMine = document.createElement('span');
      cellMine.className = 'cell-tile';
      if (typeof r.myTile === 'number') cellMine.appendChild(tilePill(r.myTile, r.myTile % 2 === 0 ? 'black' : 'white'));
      tdMine.appendChild(cellMine);

      var tdOpp = document.createElement('td');
      var cellOpp = document.createElement('span');
      cellOpp.className = 'cell-tile';
      if (typeof r.oppTile === 'number') {
        cellOpp.appendChild(tilePill(r.oppTile, r.oppTile % 2 === 0 ? 'black' : 'white'));
      } else {
        cellOpp.appendChild(tilePill('?', r.oppColor));
      }
      tdOpp.appendChild(cellOpp);

      var tdResult = document.createElement('td');
      if (r.winner === 'draw') {
        tdResult.textContent = '무';
        tdResult.className = 'draw';
      } else if (r.winner === me) {
        tdResult.textContent = '승';
        tdResult.className = 'win';
      } else {
        tdResult.textContent = '패';
        tdResult.className = 'lose';
      }

      var tdScore = document.createElement('td');
      tdScore.textContent = r.myScore + ':' + r.oppScore;

      tr.appendChild(tdRound);
      tr.appendChild(tdFirst);
      tr.appendChild(tdMine);
      tr.appendChild(tdOpp);
      tr.appendChild(tdResult);
      tr.appendChild(tdScore);
      body.appendChild(tr);
    });
  }

  // ── 사용자 동작 ────────────────────────────────────────────────────
  function onTilePick(tile) {
    if (app.submitting) return;
    app.selectedTile = app.selectedTile === tile ? null : tile;
    renderGame();
  }

  function submitTile() {
    if (app.selectedTile === null || !app.view) return;
    if (app.submitting) return;
    var ok = send({
      type: 'submit',
      set: app.view.set_index,
      round: app.view.round,
      tile: app.selectedTile
    });
    if (!ok) {
      toast('연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.');
      return;
    }
    app.submitting = true;
    el.btnSubmit.disabled = true;
    el.btnSubmit.textContent = '제출 중…';
    app.selectedTile = null;
  }

  function requestNext() {
    if (!send({ type: 'next' })) return;
    el.btnNext.disabled = true;
    el.btnNext.textContent = '다음 라운드 준비됨';
  }

  function requestForfeit() {
    if (!app.view) return;
    if (!window.confirm('기권하면 이번 판은 패배로 기록됩니다. 기권할까요?')) return;
    send({ type: 'forfeit' });
  }

  function requestRematch() {
    if (!send({ type: 'rematch' })) {
      toast('연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.');
      return;
    }
    el.btnRematch.disabled = true;
    el.btnRematch.textContent = '재대결 요청됨';
  }

  function leaveRoom() {
    app.userLeft = true;
    clearTimeout(app.reconnectTimer);
    send({ type: 'leave' });
    closeSocket();
    app.roomId = null;
    app.player = null;
    app.view = null;
    app.meta = {};
    app.selectedTile = null;
    app.submitting = false;
    app.mode = 'human';
    app.difficulty = null;
    show('home');
  }

  function showHomeNotice(message) {
    el.homeNotice.hidden = false;
    el.homeNotice.textContent = message;
  }

  function copyInvite() {
    var url = el.inviteUrl.value;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        toast('초대 링크를 복사했습니다.');
      }).catch(function () { fallbackCopy(); });
    } else {
      fallbackCopy();
    }
  }

  function fallbackCopy() {
    el.inviteUrl.removeAttribute('readonly');
    el.inviteUrl.select();
    el.inviteUrl.setSelectionRange(0, el.inviteUrl.value.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    el.inviteUrl.setAttribute('readonly', 'readonly');
    toast(ok ? '초대 링크를 복사했습니다.' : '링크를 길게 눌러 복사해 주세요.');
  }

  // ── 초기화 ─────────────────────────────────────────────────────────
  function cacheElements() {
    el.toast = $('#toast');
    el.btnCreate = $('#btn-create');
    el.btnJoin = $('#btn-join');
    el.homeNotice = $('#home-notice');
    el.roomCode = $('#room-code');
    el.inviteUrl = $('#invite-url');
    el.btnCopy = $('#btn-copy');
    el.setLabel = $('#set-label');
    el.roundLabel = $('#round-label');
    el.scoreMe = $('#score-me');
    el.scoreOpp = $('#score-opp');
    el.turnBanner = $('#turn-banner');
    el.firstBadge = $('#first-badge');
    el.myTiles = $('#my-tiles');
    el.btnSubmit = $('#btn-submit');
    el.submitHint = $('#submit-hint');
    el.oppBlack = $('#opp-black');
    el.oppWhite = $('#opp-white');
    el.oppSubmitted = $('#opp-submitted');
    el.oppStatus = $('#opp-status');
    el.oppScoreName = $('#opp-score-name');
    el.oppPanelTitle = $('#opp-panel-title');
    el.finalOppName = $('#final-opp-name');
    el.roundResult = $('#round-result');
    el.rrTitle = $('#round-result-title');
    el.rrDetail = $('#round-result-detail');
    el.rrProgress = $('#rr-progress');
    el.btnNext = $('#btn-next');
    el.btnForfeit = $('#btn-forfeit');
    el.connState = $('#conn-state');
    el.overKicker = $('#over-kicker');
    el.overTitle = $('#over-title');
    el.overReason = $('#over-reason');
    el.finalMe = $('#final-me');
    el.finalOpp = $('#final-opp');
    el.replayTitle = $('#replay-title');
    el.replayTable = $('#replay-table');
    el.replayBody = $('#replay-body');
    el.replayEmpty = $('#replay-empty');
    el.btnRematch = $('#btn-rematch');
    el.btnHome = $('#btn-home');
    el.rematchStatus = $('#rematch-status');
  }

  function bindEvents() {
    el.btnCreate.addEventListener('click', function () { show('mode'); });

    document.querySelectorAll('[data-nav]').forEach(function (btn) {
      btn.addEventListener('click', function () { show(btn.dataset.nav); });
    });

    document.querySelectorAll('.mode-card[data-mode]').forEach(function (card) {
      card.addEventListener('click', function () {
        var mode = card.dataset.mode;
        if (mode === 'ai') {
          show('difficulty');
          return;
        }
        createRoom('human');
      });
    });

    document.querySelectorAll('[data-difficulty]').forEach(function (card) {
      card.addEventListener('click', function () {
        createRoom('ai', card.dataset.difficulty);
      });
    });

    document.querySelectorAll('[data-leave]').forEach(function (btn) {
      btn.addEventListener('click', leaveRoom);
    });

    el.btnCopy.addEventListener('click', copyInvite);
    el.btnSubmit.addEventListener('click', submitTile);
    el.btnNext.addEventListener('click', requestNext);
    el.btnForfeit.addEventListener('click', requestForfeit);
    el.btnRematch.addEventListener('click', requestRematch);
    el.btnHome.addEventListener('click', leaveRoom);

    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && app.ws && app.ws.readyState === WebSocket.OPEN) {
        send({ type: 'sync' });
      }
    });

    window.addEventListener('beforeunload', function () {
      // 브라우저를 닫으면 소켓이 끊기고 서버가 유예 시간 뒤 부전승 처리한다.
      if (app.ws) app.ws.close();
    });
  }

  function boot() {
    cacheElements();
    bindEvents();

    var params = new URLSearchParams(location.search);
    var invited = params.get('room');
    if (invited) {
      var code = invited.trim();
      if (!/^[A-Za-z0-9]{4,12}$/.test(code)) {
        showHomeNotice('초대 코드가 올바르지 않습니다.');
        show('home');
        return;
      }
      el.btnJoin.hidden = false;
      el.btnJoin.addEventListener('click', function () { enterRoom(code); });
      enterRoom(code);
      return;
    }
    show('home');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
