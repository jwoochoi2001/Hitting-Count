"""경기 진행 상태(라인스코어/아웃/주자/득점)와 탑다운 타구 판정."""
import math
import random

from . import config as C
from . import field as F


def _new_batter_stat(name):
    return {"name": name, "ab": 0, "r": 0, "h": 0, "hr": 0, "rbi": 0, "bb": 0, "k": 0}


def _new_pitcher_stat(name):
    return {"name": name, "outs": 0, "bf": 0, "ab": 0, "h": 0, "hr": 0,
            "r": 0, "er": 0, "bb": 0, "k": 0,
            "decision": None, "save_situation": False,
            "entry_lead": 0, "entry_tying": False}


def solo_target_for_round(round_num):
    """라운드별 누적 목표 점수(구간마다 필요 점수가 점점 증가, 상승폭은 최대치에서 고정)."""
    if round_num <= 1:
        return C.SOLO_ROUND_BASE
    target = C.SOLO_ROUND_BASE
    for i in range(round_num - 1):
        gap = min(C.SOLO_ROUND_GAP_START + C.SOLO_ROUND_GAP_STEP * i,
                  C.SOLO_ROUND_GAP_MAX)
        target += gap
    return target


class LiveGame:
    def __init__(self, one_player, lineups, team_names):
        self.one_player = one_player
        self.lineups = lineups
        self.team_names = team_names
        self.innings = C.INNINGS            # 정규 이닝(9)
        self.max_innings = C.MAX_INNINGS    # 2인 모드 연장 최대 이닝(12)

        self.inning = 1
        self.half = "top"          # top=원정(팀0) / bottom=홈(팀1)
        self.batting_team = 0
        self.outs = 0
        # 각 베이스는 빈 자리면 None, 주자가 있으면 "그 주자가 누구인지"를 담는다
        # (2인 모드는 batter_stats 항목 자체를 참조로 들고 있어 득점을 그 선수
        # 개인 기록으로 돌릴 수 있고, 1인 모드는 선수별 기록이 없어 그냥 True).
        self.bases = [None, None, None]
        self.score = [0, 0]
        self.hits = [0, 0]
        self.errors = [0, 0]
        self.walks = [0, 0]
        self.batter_idx = [0, 0]
        self.line = [[], []]       # 이닝별 득점
        self.last_runs = 0
        self._play_scored = []   # 이번 플레이에서 득점한 주자 토큰(득점 순서대로)
        self.pending_inning_change = False
        self.walk_off = False
        self.skipped_bottom_9th = False
        self.game_tied = False
        # 경기가 끝난 순간의 이닝 표시를 그대로 고정(9회 이후로 넘어가
        # "10회 초" 처럼 보이지 않도록)
        self._final_half_label = None
        # 팀별 좌우타 정보
        self.right = [list(C.DEFAULT_RIGHT), list(C.DEFAULT_RIGHT)]
        # 팀별 선발투수 이름·투구 손(표시용, 실제 투구 로직에는 영향 없음)
        self.pitchers = ["", ""]
        self.pitcher_right = [True, True]
        # 경기 중 투수 교체가 일어나도 바뀌지 않는 "선발투수" 이름·투구 손
        # (다시하기 시 최종 등판 투수가 아니라 이 값을 라인업 프리필에 사용한다)
        self.starting_pitchers = ["", ""]
        self.starting_pitcher_right = [True, True]

        if one_player:
            self.batting_team = 1
            self.half = "bottom"
            self.solo_points = 0
            self.solo_round = 1
            self.solo_round_start_points = 0
            self.solo_outs_used = 0
            self.solo_last_gain = 0
            self.solo_game_over = False
            self.solo_round_clear_pending = False
        else:
            # 타순 슬롯별 타격 기록. 대타가 들어오면 같은 슬롯에 새 기록을
            # 이어붙여, 교체 전/후 선수의 기록이 각자 분리되어 남는다.
            self.batter_stats = [
                [[_new_batter_stat(lineups[t][slot])] for slot in range(9)]
                for t in range(2)
            ]
            # 팀별 투수 등판 기록(교체 전/후 별도). game.pitchers 는 LineupScene
            # 에서 LiveGame 생성 "이후"에 채워지므로, 여기서는 빈 리스트로 두고
            # 실제 투구 결과를 처음 기록하는 시점에 선발투수 이름으로 지연 생성한다.
            self.pitcher_stats = [[], []]
            # 승/패 판정용: 리드가 바뀔 때마다 "그 순간 마운드에 있던 투수"를
            # 각 팀의 승/패 후보로 갱신해두고, 경기가 끝나면 이긴 팀 쪽 후보를
            # 승, 진 팀 쪽 후보를 패로 확정한다(_finalize_decisions).
            self._leader = None
            self._win_candidate = [None, None]
            self._loss_candidate = [None, None]
            self._decisions_finalized = False

        self._begin_half()

    # ----------------------------------------------------------- 조회
    @property
    def defending_team(self):
        return 1 - self.batting_team

    def current_batter_name(self):
        idx = self.batter_idx[self.batting_team]
        lineup = self.lineups[self.batting_team]
        return lineup[idx % len(lineup)], (idx % len(lineup)) + 1

    def batter_is_right(self):
        idx = self.batter_idx[self.batting_team]
        r = self.right[self.batting_team]
        return r[idx % len(r)]

    def runners_on(self):
        return sum(1 for b in self.bases if b)

    def _current_pitcher_entry(self, team):
        """그 팀의 현재 투수 기록. 아직 한 번도 기록된 적이 없으면(이번 경기
        첫 투구) 그 시점의 선발투수 이름으로 새로 만든다."""
        if not self.pitcher_stats[team]:
            self.pitcher_stats[team].append(_new_pitcher_stat(self.pitchers[team]))
        return self.pitcher_stats[team][-1]

    def substitute_pitcher(self, team, name):
        """투수 교체: 그 순간부터 새 투수 기록을 이어붙여 시작한다(교체 전
        투수의 기록은 그대로 남는다). 나가는 투수가 세이브 상황에 들어와
        리드를 지킨 채 내려가면 홀드를 확정하고, 들어오는 투수는 등판 시점의
        리드 점수·동점 주자 상황을 기록해둔다."""
        outgoing = self._current_pitcher_entry(team)
        if (outgoing["save_situation"] and outgoing["outs"] > 0
                and self.score[team] > self.score[1 - team]
                and outgoing["decision"] is None):
            outgoing["decision"] = "H"
        self.pitchers[team] = name
        entry = _new_pitcher_stat(name)
        lead = self.score[team] - self.score[1 - team]
        entry["entry_lead"] = max(0, lead)
        # 동점 주자가 루상/타석/대기타석에 있는 상황(리드 <= 주자수 + 2)
        entry["entry_tying"] = 0 < lead <= self.runners_on() + 2
        entry["save_situation"] = 0 < lead and (lead <= 3 or entry["entry_tying"])
        self.pitcher_stats[team].append(entry)

    def _update_decision_candidates(self, run_token=None):
        """리드가 바뀔 때(동점/열세 → 우세)마다 승/패 후보를 갱신한다.

        승 후보: 리드를 잡은 팀의 그 순간 투수. 패 후보: 결승점(리드를 만든
        득점)을 허용한 책임 투수(그 주자를 내보낸 투수). 동점이 되면 리드가
        사라진 것이므로 기준을 초기화해, 나중에 다시 앞서는 순간의 투수가
        새로 후보가 된다. 같은 팀이 계속 앞서기만 하면 후보는 그대로다."""
        if self.score[0] == self.score[1]:
            self._leader = None
            return
        leader = 0 if self.score[0] > self.score[1] else 1
        if self._leader == leader:
            return
        trailer = 1 - leader
        self._leader = leader
        self._win_candidate[leader] = self._current_pitcher_entry(leader)
        if isinstance(run_token, dict):
            self._loss_candidate[trailer] = run_token["pitcher"]
        else:
            self._loss_candidate[trailer] = self._current_pitcher_entry(trailer)

    def _finalize_decisions(self):
        """경기 종료 시점에 승/패/세이브를 확정한다(여러 번 불려도 안전)."""
        if self.one_player or self._decisions_finalized:
            return
        self._decisions_finalized = True
        wt = self.winning_team()
        if wt is None:
            return  # 무승부는 승/패/세이브 없음
        lt = 1 - wt
        win_p = self._win_candidate[wt]
        loss_p = self._loss_candidate[lt]
        # 선발투수는 5이닝(15아웃) 이상 던져야 승리투수 자격이 있다. 못 채웠으면
        # 던진 구원투수 중 가장 잘 던진 투수(실점 적고 아웃 많은 순)가 승리투수.
        team_list = self.pitcher_stats[wt]
        if (win_p is not None and team_list and win_p is team_list[0]
                and win_p["outs"] < 15):
            relievers = [p for p in team_list[1:] if p["outs"] > 0]
            if relievers:
                win_p = min(relievers, key=lambda p: (p["r"], -p["outs"]))
        if win_p is not None:
            win_p["decision"] = "W"
        if loss_p is not None:
            loss_p["decision"] = "L"
        closer = team_list[-1] if team_list else None
        if closer is not None and closer is not win_p and closer["decision"] is None:
            lead = closer["entry_lead"]
            outs = closer["outs"]
            if lead > 0 and (
                    (closer["entry_tying"] and outs > 0)
                    or (lead <= 3 and outs >= 3)
                    or outs >= 9):
                closer["decision"] = "S"

    def current_batter_token(self):
        """주자가 베이스에 나갈 때 함께 들고 다닐 신원.

        1인 모드는 선수별 기록이 없으므로 단순 True(점유 표시용).
        2인 모드는 이 주자가 득점하면 누구(타자) 개인 기록에 득점을 더할지,
        그리고 나중에 실제로 득점했을 때 그 자책점을 "지금 이 순간 마운드에
        있는 투수"에게 물릴지를 함께 담는다 — 이후 이 주자가 다른 투수로
        교체된 뒤에 득점해도(승계주자) 자책은 원래 내보낸 이 투수 몫으로
        고정된다. earned 는 실책으로 출루한 주자일 때만 apply_plan 에서
        False 로 내려 비자책 처리한다.
        """
        if self.one_player:
            return True
        t = self.batting_team
        slot = self.batter_idx[t] % len(self.lineups[t])
        batter_stat = self.batter_stats[t][slot][-1]
        pitcher_stat = self._current_pitcher_entry(self.defending_team)
        return {"batter": batter_stat, "pitcher": pitcher_stat, "earned": True}

    def _credit_run(self, token):
        if not isinstance(token, dict):
            return
        self._play_scored.append(token)
        token["batter"]["r"] += 1
        token["pitcher"]["r"] += 1
        if token["earned"]:
            token["pitcher"]["er"] += 1

    def is_over(self):
        """정규 9회(연장 시 최대 12회) 종료 또는 1인 모드 탈락."""
        if self.one_player:
            return self.solo_game_over
        # _end_half / _maybe_walk_off 가 경기 종료 시점에만 라벨을 고정한다.
        return self._final_half_label is not None

    @property
    def outs_for_rules(self):
        """병살·태그업 등 이닝 내 아웃 규칙용(1인은 3아웃 단위로 순환)."""
        if self.one_player:
            return self.solo_outs_used % C.OUTS_PER_INNING
        return self.outs

    def solo_target(self):
        """현재 라운드 누적 목표 점수."""
        return solo_target_for_round(self.solo_round)

    def solo_prev_target(self):
        """이전 라운드까지의 누적 목표."""
        return solo_target_for_round(self.solo_round - 1) if self.solo_round > 1 else 0

    @property
    def solo_rounds_cleared(self):
        """클리어한 라운드 수(탈락 시 현재 라운드 미포함)."""
        return max(0, self.solo_round - 1)

    def solo_outs_limit(self):
        """현재 라운드 허용 아웃 수(SOLO_OUTS_ROUND_INTERVAL 라운드 클리어마다 감소)."""
        cleared = self.solo_round - 1
        steps = cleared // C.SOLO_OUTS_ROUND_INTERVAL
        return max(C.SOLO_OUTS_MIN, C.SOLO_OUTS_START - steps * C.SOLO_OUTS_STEP)

    def solo_outs_left(self):
        return max(0, self.solo_outs_limit() - self.solo_outs_used)

    def _solo_hit_points(self, plan, runs):
        kind = plan["kind"]
        bases = plan.get("bases", 0)
        if kind == "hr":
            pts = C.SOLO_HIT_POINTS[4]
        elif kind in ("hit", "error"):
            pts = C.SOLO_HIT_POINTS.get(bases, C.SOLO_HIT_POINTS[1])
        else:
            pts = 0
        return pts + runs * C.SOLO_RUN_POINTS

    def _solo_award_points(self, pts):
        self.solo_last_gain = pts
        if pts > 0:
            self.solo_points += pts
        if self.solo_points >= self.solo_target():
            # 목표를 초과 달성해도 초과분은 버리고 방금 클리어한 목표 지점에서
            # 다음 라운드를 시작한다(만루 홈런 등으로 목표를 훌쩍 넘겨도
            # 그 초과분이 다음 라운드로 그대로 넘어가지 않도록).
            self.solo_points = self.solo_target()
            self._solo_advance_round()

    def _solo_advance_round(self):
        self.solo_round += 1
        self.solo_outs_used = 0
        self.solo_round_start_points = self.solo_points
        self.solo_round_clear_pending = True
        self.bases = [None, None, None]

    def _solo_consume_outs(self, n=1):
        if n <= 0:
            return
        self.solo_outs_used += n
        if (self.solo_outs_used >= self.solo_outs_limit()
                and self.solo_points < self.solo_target()):
            self.solo_game_over = True

    def half_label(self):
        if self._final_half_label is not None:
            return self._final_half_label
        return f"{self.inning}회 {'초' if self.half == 'top' else '말'}"

    def format_batter_stats_compact(self, team_idx, slot):
        """타수·득점·안타·타점·홈런·볼넷·삼진 (볼넷은 타수 미포함). 대타 투입
        후엔 새로 들어온 선수의 기록만(교체 시점부터) 보여준다."""
        st = self.batter_stats[team_idx][slot][-1]
        return (f"{st['ab']}타수 {st['r']}득점 {st['h']}안타 {st['rbi']}타점 "
                f"{st['hr']}홈런 {st['bb']}볼넷 {st['k']}삼진")

    def format_batter_stats_lines(self, team_idx, slot):
        """인게임 표시용 — 화면(경기장)을 침범하지 않도록 두 줄로 나눠 반환."""
        st = self.batter_stats[team_idx][slot][-1]
        line1 = f"{st['ab']}타수 {st['h']}안타 {st['r']}득점 {st['rbi']}타점"
        line2 = f"{st['hr']}홈런 {st['bb']}볼넷 {st['k']}삼진"
        return line1, line2

    def format_batter_stat(self, team_idx, slot, name=None):
        """팀·타순별 타격 기록 문자열 (MVP 등)."""
        if name is None:
            name = self.lineups[team_idx][slot]
        return f"{name}  {self.format_batter_stats_compact(team_idx, slot)}"

    def current_batter_stats_compact(self):
        """현재 타석 타자의 팀별 기록(숫자만)."""
        t = self.batting_team
        slot = self.batter_idx[t] % len(self.lineups[t])
        return self.format_batter_stats_compact(t, slot)

    def current_batter_stats_lines(self):
        """현재 타석 타자의 기록을 두 줄(타수~홈런 / 볼넷~삼진)로 반환."""
        t = self.batting_team
        slot = self.batter_idx[t] % len(self.lineups[t])
        return self.format_batter_stats_lines(t, slot)

    def winning_team(self):
        if self.one_player or self.score[0] == self.score[1]:
            return None
        return 0 if self.score[0] > self.score[1] else 1

    def pick_mvp(self):
        """승리팀 MVP. 대타로 들어온 선수는 교체 전 선수와 별개로 집계된다.
        무승부면 None."""
        wt = self.winning_team()
        if wt is None:
            return None
        best_st = None
        best_key = (-1, -1, -1, -1)
        for slot_entries in self.batter_stats[wt]:
            for st in slot_entries:
                if st["ab"] == 0:
                    continue
                key = (st["rbi"], st["hr"], st["h"], st["ab"])
                if key > best_key:
                    best_key = key
                    best_st = st
        if best_st is None or best_key == (0, 0, 0, 0):
            return None
        line = (f"{best_st['name']}  {best_st['ab']}타수 {best_st['r']}득점 "
                f"{best_st['h']}안타 {best_st['rbi']}타점 {best_st['hr']}홈런 "
                f"{best_st['bb']}볼넷 {best_st['k']}삼진")
        return dict(team=wt, name=best_st["name"], stats=best_st, line=line)

    def substitute_batter(self, team, slot, name, right):
        """대타 투입: 그 순간부터 같은 타순에 새 기록을 이어붙여 시작한다
        (교체 전 선수의 기록은 그대로 남고, 이후 타석만 새 선수 몫으로 쌓인다)."""
        self.lineups[team][slot] = name
        self.right[team][slot] = right
        self.batter_stats[team][slot].append(_new_batter_stat(name))

    def _record_plate_appearance(self, *, walk=False, hit=False, hr=False,
                                 runs=0, k=False):
        """팀별 타자 기록(동일 이름이라도 팀마다 분리, 대타 이후는 새 기록에 집계).
        같은 타석 결과를 상대 투수(현재 마운드에 있는 투수)의 기록에도 반영한다."""
        if self.one_player:
            return
        t = self.batting_team
        slot = self.batter_idx[t] % len(self.lineups[t])
        st = self.batter_stats[t][slot][-1]
        pst = self._current_pitcher_entry(self.defending_team)
        pst["bf"] += 1
        if walk:
            st["bb"] += 1
            pst["bb"] += 1
            return
        st["ab"] += 1
        pst["ab"] += 1
        if hit:
            st["h"] += 1
            pst["h"] += 1
        if hr:
            st["hr"] += 1
            pst["hr"] += 1
        if k:
            st["k"] += 1
            pst["k"] += 1
        st["rbi"] += runs

    # ----------------------------------------------------------- 이닝 진행
    def _begin_half(self):
        t = self.batting_team
        while len(self.line[t]) < self.inning:
            self.line[t].append(0)

    def _end_half(self):
        pre_label = self.half_label()
        self.outs = 0
        self.bases = [None, None, None]
        reg = self.innings          # 정규 이닝(9)
        cap = self.max_innings      # 연장 포함 최대 이닝(12)
        if self.half == "top":
            # 정규(또는 연장) 회초 3아웃: 홈팀이 앞서면 그 회말 없이 경기 종료
            if self.inning >= reg and self.score[1] > self.score[0]:
                self.skipped_bottom_9th = True
                self.inning += 1
                self._final_half_label = pre_label
                return
            self.half = "bottom"
            self.batting_team = 1
            self.pending_inning_change = True
            self._begin_half()
            return
        # 회말 종료
        completed = self.inning
        tied = self.score[0] == self.score[1]
        self.half = "top"
        self.batting_team = 0
        self.inning += 1
        # 정규 이닝 종료 후 승부가 갈렸거나, 연장 최대 이닝까지 동점이면 경기 종료
        game_end = ((completed >= reg and not tied)
                    or (completed >= cap and tied))
        if game_end:
            if tied:
                self.game_tied = True
            self._final_half_label = pre_label
        else:
            self.pending_inning_change = True
            self._begin_half()

    def next_batter(self):
        self.batter_idx[self.batting_team] += 1

    def _add_runs(self, r):
        if r <= 0:
            return
        t = self.batting_team
        while len(self.line[t]) < self.inning:
            self.line[t].append(0)
        self.line[t][self.inning - 1] += r
        scored, self._play_scored = self._play_scored, []
        for i in range(r):
            self.score[t] += 1
            if not self.one_player:
                self._update_decision_candidates(
                    scored[i] if i < len(scored) else None)

    def _add_out(self):
        self.outs += 1
        if self.outs >= C.OUTS_PER_INNING:
            self._end_half()

    def _maybe_walk_off(self, score_before=None):
        """정규 9회말(또는 연장 회말) 홈팀이 1점이라도 앞서면 끝내기(홈팀 승리)."""
        if (self.inning >= self.innings and self.half == "bottom"
                and self.batting_team == 1 and self.score[1] > self.score[0]):
            self._final_half_label = self.half_label()
            self.walk_off = True
            self.inning += 1
            return True
        return False

    def _advance(self, n, batter_token):
        """주자를 n루씩 진루시키고, 홈을 밟은 주자에게 개인 득점을 돌려준 뒤
        득점 수를 반환한다."""
        runners = [(i + 1, tok) for i, tok in enumerate(self.bases) if tok]
        new_bases = [None, None, None]
        runs = 0
        for p, tok in sorted(runners, key=lambda x: x[0], reverse=True):
            np = p + n
            if np >= 4:
                runs += 1
                self._credit_run(tok)
            else:
                new_bases[np - 1] = tok
        if n >= 4:
            runs += 1
            self._credit_run(batter_token)
        else:
            new_bases[n - 1] = batter_token
        self.bases = new_bases
        return runs

    # ----------------------------------------------------------- 결과 반영
    def strikeout(self):
        self._record_plate_appearance(k=True)
        self.next_batter()
        if self.one_player:
            self.solo_last_gain = 0
            self._solo_consume_outs(1)
        else:
            self._current_pitcher_entry(self.defending_team)["outs"] += 1
            self._add_out()

    def walk(self):
        """4구 볼넷: 타자 1루 진루(주자는 포스 상황만 진루)."""
        self._play_scored = []
        if not self.one_player:
            self.walks[self.batting_team] += 1
        else:
            self.walks[1] += 1
        runs = self._walk_advance(self.current_batter_token())
        score_before = tuple(self.score)
        if self.one_player:
            self._solo_award_points(C.SOLO_WALK_POINTS + runs * C.SOLO_RUN_POINTS)
        elif runs:
            self._add_runs(runs)
        self._record_plate_appearance(walk=True)
        self.next_batter()
        self.last_runs = runs
        if not self.one_player:
            self._maybe_walk_off(score_before)
        return runs

    def _walk_advance(self, batter_token):
        """볼넷: 포스 진루만. 만루 볼넷은 1점(3루 주자 개인 득점) + 만루 유지."""
        b0, b1, b2 = self.bases
        if b0 and b1 and b2:
            self._credit_run(b2)
            self.bases = [batter_token, b0, b1]
            return 1
        if b0 and b1:
            self.bases = [batter_token, b0, b1]
            return 0
        if b0:
            self.bases = [batter_token, b0, b2]
            return 0
        self.bases = [batter_token, b1, b2]
        return 0

    def apply_plan(self, plan):
        kind = plan["kind"]
        runs = 0
        self._play_scored = []
        batter_token = self.current_batter_token()
        if kind in ("hit", "hr", "error"):
            if kind == "error" and not self.one_player:
                # 실책으로 출루한 주자는 나중에 득점해도 비자책(투수 책임 아님)
                batter_token["earned"] = False
            runs = self._advance(plan["bases"], batter_token)
            if self.one_player:
                if kind != "error":
                    self.hits[1] += 1
                self._solo_award_points(self._solo_hit_points(plan, runs))
            else:
                if kind == "error":
                    self.errors[self.defending_team] += 1
                else:
                    self.hits[self.batting_team] += 1
                score_before = tuple(self.score)
                self._add_runs(runs)
                self._maybe_walk_off(score_before)
            self._record_plate_appearance(
                hit=(kind in ("hit", "hr")), hr=(kind == "hr"), runs=runs)
            self.next_batter()
        elif kind == "out":
            rules_outs = self.outs_for_rules
            outs_to_add = plan.get("outs", 1)
            if self.one_player:
                # 1인 모드는 실제 이닝 교대가 없다 — 라운드에 배정된 아웃을
                # 전부 소진하는 '진짜 마지막 아웃'일 때만 그 플레이의 득점이
                # 무효가 되어야 한다(3아웃마다 무효가 되면 안 됨).
                ends_inning = (self.solo_outs_used + outs_to_add
                              >= self.solo_outs_limit())
            else:
                ends_inning = rules_outs + outs_to_add >= C.OUTS_PER_INNING
                self._current_pitcher_entry(self.defending_team)["outs"] \
                    += outs_to_add
            score_before = tuple(self.score)
            pre = plan.get("bases_at_pitch", list(self.bases))
            scored_tokens = []
            if plan.get("tag_up") and rules_outs < C.OUTS_PER_INNING - 1:
                if self.bases[2]:
                    scored_tokens.append(self.bases[2])
                    self.bases[2] = None
                    runs += 1
                if self.bases[1]:
                    self.bases[2] = self.bases[1]
                    self.bases[1] = None
            elif plan.get("double_play"):
                self.bases, dp_runs, dp_scored = _apply_double_play_bases(list(pre))
                if pre[2] and dp_runs < 1:
                    dp_runs = 1
                    dp_scored = [pre[2]]
                    self.bases = [None, None, pre[1]]
                if dp_runs:
                    runs += dp_runs
                    scored_tokens.extend(dp_scored)
            elif plan.get("force_out_2nd"):
                self.bases, fc_runs, fc_scored = _apply_fc_second_bases(
                    list(pre), batter_token)
                if fc_runs:
                    runs += fc_runs
                    scored_tokens.extend(fc_scored)
            elif plan.get("out_at_first"):
                self.bases, adv_runs, adv_scored = _apply_ground_out_first(list(pre))
                if adv_runs:
                    runs += adv_runs
                    scored_tokens.extend(adv_scored)
            # 3아웃으로 이닝이 끝나는 플레이에서는 득점(개인 득점 포함) 무효
            if ends_inning:
                runs = 0
            else:
                for tok in scored_tokens:
                    self._credit_run(tok)
            if runs and not self.one_player:
                self._add_runs(runs)
            self._record_plate_appearance(runs=runs)
            self.next_batter()
            if self.one_player:
                self._solo_award_points(runs * C.SOLO_RUN_POINTS)
                self._solo_consume_outs(outs_to_add)
            else:
                for _ in range(outs_to_add):
                    if self.outs < C.OUTS_PER_INNING and not self.is_over():
                        self._add_out()
                if runs:
                    self._maybe_walk_off(score_before)
        self.last_runs = runs
        return runs


# ================================================================ 타구 판정
RUN_HOME_TO_FIRST = 4.40    # 타자주자 1루 도달 시간(초)
THROW_SPEED = 115.0          # 송구 속도(ft/s)
FIELDER_SPEED = 27.0         # 수비 이동 속도(ft/s)
FLY_CARRY_BASE = 160.0
FLY_CARRY_MULT = 315.0
FLY_LB_PEAK = 32.0
FLY_LB_WIDTH = 30.0
FLY_LB_FLOOR = 0.4
FLY_CATCH_RADIUS = 34.0     # BABIP .315 근처로 맞춤(캐치 판정 반경)
LINER_CATCH_RADIUS = 33.5
_XBH_CUSHION_BASE_FT = 8.0     # 외야수 정면 바로 앞 기본 여유(ft) — 정면은 이 정도만
_XBH_CUSHION_LATERAL_FT = 3.9  # 정면 각도에서 벗어난 정도(도)당 추가 여유(ft, 갭/라인일수록 커짐)
RELEASE = 0.55               # 포구 후 송구까지 지연
CATCH_TIME_CAP = 2.86        # 낙구 지점까지 달릴 수 있는 시간 상한
PITCHER_GROUND_REACH_FT = 6.0  # 투수 땅볼 수비 폭(정면 강습타만, 좌우로 넓게 못 감)

INFIELDERS = ("투수", "1루수", "2루수", "유격수", "3루수")
INFIELDER_SET = frozenset(INFIELDERS)


def _foul_plan(landing):
    return dict(kind="foul", label="FOUL", ball_type="foul",
                landing=landing, field_by=None, throw_to=None,
                bases=0, outs=0, double_play=False, sac_fly=False, error=False)


def _double_play_details(fielder):
    """병살 연출용: 표기 코드, 2루 커버 수비수."""
    if fielder == "유격수":
        return "6-4-3", "2루수"
    if fielder == "2루수":
        return "4-6-3", "유격수"
    if fielder == "3루수":
        return "5-4-3", "2루수"
    if fielder == "1루수":
        return "3-6-3", "유격수"
    return "병살", "2루수"


def _fc_second_cover(fielder):
    """1루 주자 포스 아웃(2루 송구) 시 2루 커버 수비수."""
    if fielder == "2루수":
        return "유격수"
    if fielder == "유격수":
        return "2루수"
    return "2루수"


def _apply_double_play_bases(bases):
    """병살: 1루·타자 아웃, 2루→3루, 3루 주자는 홈인.

    (new_bases, runs, scored_tokens) — scored_tokens 는 득점 개인 기록용으로,
    호출 쪽에서 이닝이 그 아웃으로 끝나는 상황이 아닐 때만 실제로 반영한다.
    """
    b0, b1, b2 = bases
    scored = [b2] if b2 else []
    runs = 1 if b2 else 0
    return [None, None, b1], runs, scored


def _apply_fc_second_bases(bases, batter_token):
    """2루 포스 아웃: 1루 주자 아웃, 타자 1루, 2루→3루. 3루 주자는 홈인."""
    b0, b1, b2 = bases
    scored = [b2] if b2 else []
    runs = 1 if b2 else 0
    return [batter_token, None, b1], runs, scored


def _apply_ground_out_first(bases):
    """1루 송구 아웃: 타자 아웃, 주자 1루씩 진루. 3루 주자는 홈인."""
    b0, b1, b2 = bases
    scored = [b2] if b2 else []
    runs = 1 if b2 else 0
    if not b0:
        return [None, None, b1], runs, scored
    return [None, b0, b1], runs, scored


def _hr_presentation(bases):
    """홈런 연출 문구 (영문 타이틀 + 한글 부제)."""
    r = sum(1 for b in bases if b)
    if r >= 3:
        return "GRAND SLAM", "만루 홈런"
    if r == 2:
        return "THREE-RUN HOMER", "쓰리런 홈런"
    if r == 1:
        return "TWO-RUN HOMER", "투런 홈런"
    return "HOME RUN", "솔로 홈런"


def _nearest_catcher(point, time_budget, radius=7.0, candidates=None):
    """time_budget 안에 point 에 닿을 수 있는 수비수와, 잡을 수 있는지.

    time_budget 은 CATCH_TIME_CAP 로 제한해 외야 사이(갭)를 뚫는 안타가 나오게 한다.
    """
    cand = candidates or F.FIELDERS_HOME
    tb = min(time_budget, CATCH_TIME_CAP)
    best, bestgap, caught = None, 1e9, False
    for name, pos in cand.items():
        d = F.dist_ft(pos, point)
        reach = FIELDER_SPEED * tb + radius
        gap = d - reach
        if gap < bestgap:
            bestgap, best = gap, name
        if d <= reach:
            caught = True
    return best, caught


def resolve_batted_ball(power, spray_deg, launch_deg, bases, outs):
    """타구 물리/수비 판정.

    power 0~1, spray_deg(-=좌/+=우), launch_deg(0=땅볼~높을수록 뜬공)
    반환 plan: kind, label, ball_type, landing(ft), field_by, throw_to(ft),
              bases, outs, double_play, sac_fly, error
    """
    runner_first = bases[0]

    # 파울 (타구 각도)
    if abs(spray_deg) > F.FOUL_DEG:
        return _foul_plan(F.polar(80, spray_deg))

    fence = F.fence_dist(spray_deg)

    # ---------------------------------------------------- 땅볼
    if launch_deg < 10:
        rad = math.radians(spray_deg)
        dx, dy = math.sin(rad), math.cos(rad)
        roll = 90 + power * 190
        ballspeed = 95 + power * 80
        can = []
        for name in INFIELDERS:
            px, py = F.FIELDERS_HOME[name]
            proj = px * dx + py * dy
            perp = abs(px * dy - py * dx)
            if proj <= 6 or proj > roll:
                continue
            t = proj / ballspeed
            # 강한 타구일수록 옆을 빠르게 지나가 잡기 어려움(3-유간/1-2루간)
            reach = FIELDER_SPEED * t + 8 - power * 4
            if name == "투수":
                # 투수는 홈 정면축 바로 위에 있어 perp 가 항상 작게 나와
                # 그대로 두면 '가장 가까운 수비수' 판정에서 거의 항상 이겨버린다.
                # 실제로는 마운드 정면 강습타만 잡을 수 있으므로 커버 폭을 좁힌다.
                reach = min(reach, PITCHER_GROUND_REACH_FT)
            if perp <= reach:
                can.append((proj, name, t))
        # 1-2루간/3-유간, 1루수-1루/3루수-3루 사이 라인 쪽은 폭이 좁아서(1-2/3-유간
        # 약 50ft) reach 계산상 거의 항상 누군가 잡는 걸로 나온다. 실제로는
        # 강하게 잡아당긴 타구가 그 좁은 틈을 그대로 꿰뚫는 경우가 있으므로,
        # 그 방향으로 강하게 맞은 타구는 낮은 확률로 그대로 빠지게 한다.
        # (1-2루간/3-유간이 라인 쪽보다 훨씬 자주 나오도록 확률을 따로 둔다.)
        if can and power > _INFIELD_GAP_POWER_MIN:
            if any(lo <= spray_deg <= hi for lo, hi in _INFIELD_MID_GAP_BANDS):
                if random.random() < _INFIELD_MID_GAP_STEAL_PROB:
                    can = []
            elif any(lo <= spray_deg <= hi for lo, hi in _INFIELD_LINE_GAP_BANDS):
                if random.random() < _INFIELD_LINE_GAP_STEAL_PROB:
                    can = []
        if can:
            can.sort()
            proj, fielder, t = can[0]
            fpos = F.FIELDERS_HOME[fielder]
            # 실책
            if random.random() < 0.06:
                return dict(kind="error", label="실책", ball_type="ground",
                            landing=fpos, field_by=fielder, throw_to=F.B1,
                            bases=1, outs=0, double_play=False,
                            sac_fly=False, error=True)
            throw_t = F.dist_ft(fpos, F.B1) / THROW_SPEED + RELEASE
            def_time = t + throw_t
            safe = def_time > RUN_HOME_TO_FIRST + random.uniform(-0.15, 0.15)
            # 병살: 2루 경유 더블플레이 (6-4-3 / 4-6-3 등)
            if (runner_first and outs < 2 and fielder != "투수"
                    and not safe and random.random() < 0.62):
                dp_code, dp_relay = _double_play_details(fielder)
                return dict(kind="out", label=f"병살타 ({dp_code})",
                            ball_type="ground", landing=fpos, field_by=fielder,
                            throw_to=F.B1, dp_mid=F.B2, dp_relay=dp_relay,
                            dp_code=dp_code, bases=0, outs=2, double_play=True,
                            sac_fly=False, error=False)
            if safe:
                return dict(kind="hit", label="내야 안타", ball_type="ground",
                            landing=fpos, field_by=fielder, throw_to=F.B1,
                            bases=1, outs=0, double_play=False,
                            sac_fly=False, error=False)
            # 투수 땅볼: 대부분 1루 송구(타자 아웃, 1루 주자 2루 진루)
            if fielder == "투수":
                return dict(kind="out", label=f"땅볼 아웃 ({fielder}→1루)",
                            ball_type="ground", landing=fpos, field_by=fielder,
                            throw_to=F.B1, out_at_first=True, bases=0, outs=1,
                            double_play=False, sac_fly=False, error=False)
            # 2아웃이면 1루 송구가 일반적
            if runner_first and outs >= 2:
                return dict(kind="out", label=f"땅볼 아웃 ({fielder}→1루)",
                            ball_type="ground", landing=fpos, field_by=fielder,
                            throw_to=F.B1, out_at_first=True, bases=0, outs=1,
                            double_play=False, sac_fly=False, error=False)
            # 1루 주자 있음 → 2루 포스 아웃 송구, 타자 1루 도착
            if runner_first:
                relay = _fc_second_cover(fielder)
                return dict(kind="out", label=f"땅볼 아웃 ({fielder}→2루)",
                            ball_type="ground", landing=fpos, field_by=fielder,
                            throw_to=F.B2, force_out_2nd=True, fc_relay=relay,
                            bases=0, outs=1, double_play=False,
                            sac_fly=False, error=False)
            return dict(kind="out", label=f"땅볼 아웃 ({fielder})",
                        ball_type="ground", landing=fpos, field_by=fielder,
                        throw_to=F.B1, out_at_first=True, bases=0, outs=1,
                        double_play=False, sac_fly=False, error=False)
        # 내야 사이로 빠지는 안타 — 내야만 살짝 벗어난 정도로는 2루타가 안 되고,
        # 담당 외야수 뒤까지 굴러가야(외야수 수비 위치보다 멀리) 2루타가 된다.
        gb_fielder = _outfielder_for(spray_deg)
        gb_of_depth = F.dist_ft(F.HOME, F.FIELDERS_HOME[gb_fielder])
        # 1-2루간/3-유간(내야수 두 명 사이)은 항상 1루타로만,
        # 1루/3루 라인(베이스와 내야수 사이)은 항상 2루타로만 처리한다.
        in_mid_gap = (_GAP_3B_LINE_DEG < spray_deg <= _GAP_3B_SS_DEG
                     or _GAP_2B_1B_DEG < spray_deg <= _GAP_1B_LINE_DEG)
        in_line = spray_deg <= _GAP_3B_LINE_DEG or spray_deg > _GAP_1B_LINE_DEG
        if in_mid_gap:
            # 내야를 빠져나가는 단타는 외야수 정위치보다 한참 앞(얕은 외야)에서
            # 멈춰야 외야수가 실제로 앞으로 달려나와 잡는 그림이 된다.
            roll_dist = max(120.0, min(roll + 80, gb_of_depth - 40, fence - 10))
        else:
            roll_dist = min(roll + 80, fence - 10)
        roll_pt = F.polar(roll_dist, spray_deg)
        if F.is_foul_point(roll_pt):
            return _foul_plan(roll_pt)
        if in_mid_gap:
            bases_n = 1
        elif in_line:
            bases_n = 2
        else:
            bases_n = (2 if power > 0.58 and abs(spray_deg) > 15
                      and roll_dist > gb_of_depth else 1)
        relay_fielder, relay_final = _relay_plan(bases_n, spray_deg)
        label = _gap_label(spray_deg) if bases_n == 1 else "2루타"
        return dict(kind="hit", label=label,
                    ball_type="gap", landing=roll_pt,
                    field_by=gb_fielder, throw_to=None,
                    bases=bases_n, outs=0, double_play=False,
                    sac_fly=False, error=False,
                    relay_fielder=relay_fielder, relay_final=relay_final,
                    gap_pass_dist=_gap_pass_dist(spray_deg))

    # ---------------------------------------------------- 뜬공 / 라인드라이브 / 팝업
    if launch_deg >= 50:  # 팝업
        carry = 90 + power * 90
        land = F.polar(carry, spray_deg)
        if F.is_foul_point(land):
            return _foul_plan(land)
        best, caught = _nearest_catcher(land, 3.0, radius=10)
        return dict(kind="out", label=f"뜬공 아웃 ({best})", ball_type="fly",
                    landing=land, field_by=best, throw_to=None, bases=0,
                    outs=1, double_play=False, sac_fly=False, error=False)

    liner = launch_deg < 25
    if liner:
        carry = 130 + power * 180
        hang = 0.6 + power * 0.5
        radius = LINER_CATCH_RADIUS
    else:  # 뜬공
        lb = max(FLY_LB_FLOOR, 1 - ((launch_deg - FLY_LB_PEAK) / FLY_LB_WIDTH) ** 2)
        carry = (FLY_CARRY_BASE + power * FLY_CARRY_MULT) * lb
        hang = 1.3 + (launch_deg / 45.0) * 2.0 + power * 0.5
        radius = FLY_CATCH_RADIUS
        # 담장 근처까지 뻗는 깊은 타구는 외야수가 등지고 쫓아야 해서 포구 범위가 좁다
        # (→ 갭을 가르는 2·3루타가 나온다). BABIP 자체엔 영향이 작다.
        if carry > fence * 0.88:
            radius *= 0.78

    # 홈런
    if not liner and carry >= fence:
        land = F.polar(fence + 20, spray_deg)
        hr_title, hr_sub = _hr_presentation(bases)
        return dict(kind="hr", label=hr_title, hr_title=hr_title, hr_sub=hr_sub,
                    ball_type="fly", landing=land,
                    field_by=None, throw_to=None, bases=4, outs=0,
                    double_play=False, sac_fly=False, error=False)

    land = F.polar(carry, spray_deg)
    if F.is_foul_point(land):
        return _foul_plan(land)

    # 강한 직선타가 내야수 정면으로 향하면 정면 캐치(내야 직선타 아웃)
    if liner:
        snag = _infield_liner_snag(spray_deg, carry)
        if snag:
            return dict(kind="out", label=f"{snag} 직선타 아웃",
                        ball_type="liner", landing=F.FIELDERS_HOME[snag],
                        field_by=snag, throw_to=None, bases=0, outs=1,
                        double_play=False, sac_fly=False, error=False)

    # 짧은 라이너: 내야수가 먼저 처리(내야 직선타 vs 땅볼과 별도)
    if liner and carry < 175:
        ifield = {n: F.FIELDERS_HOME[n] for n in INFIELDERS}
        best, caught = _nearest_catcher(land, hang, radius=radius + 2,
                                        candidates=ifield)
    else:
        best, caught = None, False
    if not caught:
        best, caught = _nearest_catcher(land, hang, radius=radius)
    if caught:
        # 깊은 뜬공 태그업·희생플라이는 2아웃 전에만
        deep = (not liner) and carry > 230
        can_tag = deep and outs < 2
        if can_tag and bases[2]:
            label = "희생플라이"
        elif liner and best in INFIELDER_SET:
            label = f"내야 직선타 아웃 ({best})"
        else:
            label = f"뜬공 아웃 ({best})"
        return dict(kind="out", label=label, ball_type="fly", landing=land,
                    field_by=best, throw_to=None, bases=0, outs=1,
                    double_play=False, sac_fly=can_tag and bases[2],
                    tag_up=can_tag, error=False)

    # 안타(뜬공/라인성): 거리로 루타 — 담당 외야수 "정면" 바로 앞(얕게)에 떨어지면
    # 예외 없이 무조건 안타(1루타)다. best 는 내야수일 수도 있으므로(짧은 블루퍼 등),
    # 반드시 "실제 외야수" 기준으로 깊이를 계산해야 내야수만 살짝 넘긴 타구가
    # 2루타로 되는 걸 막을 수 있다.
    of_name = _outfielder_for(spray_deg)
    of_x, of_y = F.FIELDERS_HOME[of_name]
    of_depth = F.dist_ft(F.HOME, (of_x, of_y))
    # 다만 "그 외야수 정면"이 아니라 두 외야수 사이 갭이나 라인 쪽으로 가면
    # 담당 외야수가 비스듬히 뛰어야 해서 커버 범위가 줄어드는 만큼, 그 정도로
    # 갈수록만 살짝 못 미쳐도 이미 빠진 것으로 인정한다(정면은 예외 없이 안타).
    of_center_deg = math.degrees(math.atan2(of_x, of_y))
    lateral_off = abs(spray_deg - of_center_deg)
    cushion = _XBH_CUSHION_BASE_FT + _XBH_CUSHION_LATERAL_FT * lateral_off
    past_fielder = carry > of_depth - cushion
    if past_fielder and carry >= fence * 0.84 and random.random() < 0.55:
        bases_n, label = 3, "3루타"
    elif past_fielder:
        bases_n, label = 2, "2루타"
    else:
        bases_n, label = 1, "안타"
    relay_fielder, relay_final = _relay_plan(bases_n, spray_deg)
    return dict(kind="hit", label=label, ball_type="fly", landing=land,
                field_by=best, throw_to=None, bases=bases_n, outs=0,
                double_play=False, sac_fly=False, error=False,
                relay_fielder=relay_fielder, relay_final=relay_final)


_LINER_SNAG_FIELDERS = ("1루수", "2루수", "유격수", "3루수")
_LINER_SNAG_PERP_FT = 2.7


def _infield_liner_snag(spray_deg, carry):
    """강한 직선타의 궤적이 내야수 정면 근처를 지나가면 정면에서 캐치해 아웃.

    (하드히트 직선타는 원래 뻗어나가는 거리가 멀어 '뛰어가서 잡는' 판정으로는
    내야수가 절대 따라잡을 수 없다 — 실제로는 정면으로 오는 타구를 서서 잡는
    경우이므로 궤적과 수비수 위치의 수직거리로 따로 판정한다.)
    """
    rad = math.radians(spray_deg)
    dx, dy = math.sin(rad), math.cos(rad)
    best_name, best_perp = None, 1e9
    for name in _LINER_SNAG_FIELDERS:
        px, py = F.FIELDERS_HOME[name]
        proj = px * dx + py * dy
        if proj <= 10 or proj > carry:
            continue
        perp = abs(px * dy - py * dx)
        if perp < best_perp:
            best_perp, best_name = perp, name
    if best_name is not None and best_perp <= _LINER_SNAG_PERP_FT:
        return best_name
    return None


def _outfielder_for(spray_deg):
    if spray_deg < -15:
        return "좌익수"
    if spray_deg > 15:
        return "우익수"
    return "중견수"


# 내야수 사이를 빠져나가는 안타의 통과 지점(각도 기준, 실제 내야수 배치각과 매칭)
_GAP_3B_LINE_DEG = -34.0    # 3루수 라인 밖(3루 라인)
_GAP_3B_SS_DEG = -16.5      # 3루수-유격수 사이
_GAP_2B_1B_DEG = 16.5       # 2루수-1루수 사이
_GAP_1B_LINE_DEG = 34.0     # 1루수 라인 밖(1루 라인)
# 3루수-유격수 / 2루수-1루수 사이(1-2루간/3-유간) — 강습타가 두 내야수 사이를
# 그대로 꿰뚫을 수 있는 구간. 라인 쪽보다 훨씬 자주 나오게 확률을 높게 둔다.
_INFIELD_MID_GAP_BANDS = ((16.0, 34.0), (-34.0, -16.0))
_INFIELD_MID_GAP_STEAL_PROB = 0.36
# 1루수-1루 베이스 / 3루수-3루 베이스 사이 좁은 라인 쪽 틈 — 드물게만 나온다.
_INFIELD_LINE_GAP_BANDS = ((34.0, 44.5), (-44.5, -34.0))
_INFIELD_LINE_GAP_STEAL_PROB = 0.24
_INFIELD_GAP_POWER_MIN = 0.45


def _gap_label(spray_deg):
    """내야를 빠져나가는 안타의 통과 구간별 안내 문구."""
    if spray_deg <= _GAP_3B_LINE_DEG:
        return "안타 (3루 라인)"
    if spray_deg <= _GAP_3B_SS_DEG:
        return "안타 (3-유간)"
    if spray_deg <= _GAP_2B_1B_DEG:
        return "안타 (센터 앞)"
    if spray_deg <= _GAP_1B_LINE_DEG:
        return "안타 (1-2루간)"
    return "안타 (1루 라인)"


_CORNER_INFIELD_DEPTH = F.dist_ft(F.HOME, F.FIELDERS_HOME["1루수"])   # == 3루수
_MID_INFIELD_DEPTH = F.dist_ft(F.HOME, F.FIELDERS_HOME["2루수"])      # == 유격수
_GAP_PASS_DEPTH = (_CORNER_INFIELD_DEPTH + _MID_INFIELD_DEPTH) / 2.0


def _gap_pass_dist(spray_deg):
    """내야를 빠져나가는 안타가 실제로 내야수 옆을 스쳐 지나가는 거리(ft).

    라인 쪽(1루/3루수 라인 밖)은 그 코너 내야수 깊이, 1-2루간/3-유간은 두
    내야수 깊이의 중간, 센터는 중견수 쪽 내야수(2루수/유격수) 깊이를 쓴다.
    """
    if spray_deg <= _GAP_3B_LINE_DEG or spray_deg > _GAP_1B_LINE_DEG:
        return _CORNER_INFIELD_DEPTH
    if spray_deg <= _GAP_3B_SS_DEG or spray_deg > _GAP_2B_1B_DEG:
        return _GAP_PASS_DEPTH
    return _MID_INFIELD_DEPTH


def _relay_plan(bases_n, spray_deg):
    """2·3루타 확정 후의 (연출용) 중계 송구 계획 — 베이스가 아니라 그 근처
    내야수(2루수/유격수)에게 공을 준다. 좌측 타구는 유격수, 우측 타구는
    2루수가 중계를 선다(중견수 정면은 유격수 기준). 2루타는 그 내야수가
    받으면 끝, 3루타는 그 내야수를 거쳐 3루수에게 한 번 더 송구한다.
    결과(세이프)에는 영향 없는 연출용 정보.
    """
    if bases_n == 2:
        return _of_relay_infielder(spray_deg), None
    if bases_n == 3:
        return _of_relay_infielder(spray_deg), "3루수"
    return None, None


def _of_relay_infielder(spray_deg):
    return "유격수" if spray_deg <= 0 else "2루수"
