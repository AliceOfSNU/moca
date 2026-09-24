"""프로그램 모카: one agent per program, with its own goal tree (documents/program_agent.md).

운영 모카 decides which programs exist; a program agent runs one of them. It is not a planner that writes a list
of activities once — it drafts a session, finds out what is still undecided, settles each open question with the
people it concerns, opens the 정모, and afterwards judges whether the session did what it was for.

Lifecycle (harness-driven, nothing the model chooses):
- spawn: as soon as the planner fills a program's plan, the harness creates the agent's top goal, whose objective
  and completion criteria come from the program itself. The goal carries program_id, and every subgoal under it
  inherits it (harness/goals.py).
- run: the goal loop wakes it like any other goal tree (admin/goal_loop.py), with this module's instructions and
  its own program's context. Same steps, same tasks, same harness gates as 운영 모카.
- teardown: when its top goal is achieved or closed, the harness finishes the program (finished / dropped) and
  closes the rest of its tree. Tasks already running — a question asked of members, an open vote — are left to
  finish; their results are recorded and simply not acted on.

What the agent may decide by itself: the shape of its own sessions. What it may not: anything that needs a
person's agreement (who presents, who hosts, who comes) — that has to be asked, and only then written into the
activity's slots.
"""
import time

from harness import activities as A
from harness import goals as G
from harness import programs as P

MAX_CRITERIA = 4


def goal_id_for(program_id):
    return f"g_{program_id}"


def top_goal(p):
    """The agent's top goal, written from the program: objective and how we would know it is done."""
    title, purpose = P.show(p, p["title"]), P.show(p, p["purpose"])
    objective = f"프로그램 '{title}'을 실제로 수행해 목적을 이룬다: {purpose}"[:G.OBJECTIVE_LIMIT]
    if p["type"] == "linear":
        L = p["linear"]
        criteria = [f"참가자가 {P.show(p, L['exit_state'])} 상태가 되었다",
                    f"확인 방법대로 확인했다: {P.show(p, L['measure'])}",
                    "스케치의 모든 단계를 열었거나, 열지 않은 단계의 이유가 남아 있다"]
    else:
        rep = p["recurring"]["repeats"]
        times = {"constant": f"{rep['count']}회를 모두 열었다",
                 "conditional": f"조건이 끝날 때까지 이어갔다 (조건: {P.show(p, rep['condition'])})",
                 "infinite": "반복이 자리 잡아 형식대로 이어지고 있다"}[rep["kind"]]
        criteria = [times, "각 회차가 끝난 뒤 그 회차의 목표(exit state) 달성 여부를 확인했다",
                    "이 형식을 계속할지 판단할 근거가 모였다"]
    return objective, criteria[:MAX_CRITERIA]


def spawn(program, log=None):
    """Give a program its agent: one top goal carrying program_id. Returns the goal, or None if it has one."""
    goal_id = goal_id_for(program["id"])
    if G.get(G.load(), goal_id):
        return None
    objective, criteria = top_goal(program)
    goal = G.add(goal_id, objective, criteria, created_by=f"harness(program:{program['id']})",
                 program_id=program["id"])
    records = P.load()
    p = next(x for x in records if x["id"] == program["id"])
    p["agent"] = {"goal_id": goal_id, "since": time.strftime("%Y-%m-%d %H:%M:%S")}
    p["updated_at"] = p["agent"]["since"]
    P.save(records)
    if log:
        log(f"  프로그램 담당 에이전트 생성: {goal_id} ({P.show(p, p['title'])})")
    return goal


def teardown(program_id, status, reason, log=None):
    """The program is over: finish it and close what its agent still had open. Running tasks are left alone —
    a question already asked of members should still come back with an answer."""
    p, problem = P.finish(program_id, status, reason)
    if problem:
        return None, problem
    goals = G.load()
    closed = []
    for g in goals:
        if g.get("program_id") == program_id and g["status"] not in G.FINISHED:
            g.update(status="closed", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                     outcome={"summary": f"프로그램이 {P.STATUSES[status]}: {reason}", "limitations": []})
            closed.append(g["id"])
    G.save(goals)
    for a in A.for_program(program_id, statuses=("draft",)):
        A.cancel(a["id"], reason=f"프로그램 {P.STATUSES[status]}")
    if log:
        log(f"  프로그램 {program_id} {P.STATUSES[status]} → 목표 {len(closed)}개 닫음, 기획 중이던 활동 정리")
    return p, None


def due_programs():
    """Programs whose plan is filled but which have no agent yet."""
    return [p for p in P.live() if P.plan_of(p) is not None and not p.get("agent")]


def spawn_due(log=None):
    return [spawn(p, log) for p in due_programs()]


def program_of(goal):
    pid = goal.get("program_id")
    return next((p for p in P.load() if p["id"] == pid), None) if pid else None


# --- the prompt -------------------------------------------------------------------------------------

RULES = """

## 지금 너의 역할: 프로그램 모카 ({title})
너는 이 프로그램 하나를 맡은 모카야. 모임 전체의 운영은 운영 모카가 보고, 너는 이 프로그램을 실제로 굴려서
목적을 이루는 일만 한다. 다른 프로그램의 목표나 활동은 건드리지 않는다. 멤버에게 직접 말을 걸 수는 없고,
1:1 대화 내용도 볼 수 없어. 물어볼 일은 작업(chat_moca)으로 맡기고, 결과만 받는다.

## 활동의 종류
기획이 정한 종류에 맞게 준비할 것이 다르다. 종류를 바꿀 수는 없고, 그 종류에 맞게 채우는 게 네 일이다.
- individual_study(각자 스터디): 같은 자리에서 각자 자기 할 일을 한다. 정할 것은 장소·시간과 진행 담당 정도.
- presentation(발표): 정해진 발표자가 준비해 온 것을 설명하고 질문을 받는다. 발표자와 주제의 동의가 핵심.
- hands_on(실습): 진행자가 준비한 것을 다 같이 따라 해 본다. 준비물·환경 확인이 중요하다.
- discussion(토론): 정해진 주제로 함께 이야기한다. 발표자 대신 진행자와 이야깃거리가 필요하다.
- show_and_tell(결과물 공유): 참가자들이 각자 만든 것을 짧게 보여 준다. 지정 발표자가 아니라 모두가 주인공.
- clinic(질문·상담): 각자 막힌 문제를 가져오고 경험 있는 멤버가 함께 푼다. 문제를 미리 모으는 게 준비다.
- collab_project(함께 만들기): 한 가지를 여럿이 같이 만든다. 역할 나누기와 이어서 할 시간이 필요하다.
- social(친목): 같이 먹거나 노는 자리. 준비는 가볍게, 장소와 인원만 맞으면 된다.
- other(그 밖): 기획에 이름이 따로 적혀 있다.

## 프로그램 → 활동
- 프로그램의 기획(정기: 템플릿, 단계형: 스케치)은 매 회차의 틀이야. 한 회차를 실제로 열려면 그 틀로 활동
  (activity)을 하나 기획하고(draft_activity), 틀이 비워 둔 '정할 것'(슬롯)을 채운 뒤 정모를 연다.
- 슬롯은 사람에 대한 것이 많다: 발표자, 진행자, 참가자, 시간, 장소. 이건 네가 정하는 게 아니라 당사자에게
  물어서 정해지는 것이다. 멤버가 무슨 일을 하는지 안다고 해서 그 멤버가 발표할 것이라고 적지 마. 직업·경험·관심은
  누구에게 물어볼지 고르는 근거일 뿐, 수락이 아니다.
- 확인이 끝난 것만 update_activity로 적는다. note에 누가 언제 어떻게 동의했는지 남겨. 정모(create_event)는
  슬롯을 모두 채운 뒤에만 열 수 있다 (하네스가 확인한다).
- 정모가 끝나면 하네스가 그 활동을 '지난 활동'으로 바꾼다. 그 회차의 exit state를 확인하는 것까지가 네 일이야.

## 멤버에게 프로그램을 처음 알릴 때
- 프로그램은 멤버에게 보이지 않는다. 그래서 아무 설명 없이 회차 이야기부터 꺼내면 멤버 입장에서는 뜬금없다.
  멤버에게 처음 무언가를 물어야 할 때가 되면, 그때 프로그램을 소개해라. 순서는 ① write_post로 소개 글 ②
  open_program_signup으로 참가 등록 정모. 이 둘은 붙여서 한다.
- 소개 글에 담는 것: 무엇을 하는 모임인지, 누구에게 맞는지, 한 회차가 어떻게 진행되는지, 어떻게 참가하는지.
  담지 않는 것: 가설·근거·목표·슬롯 같은 내부 이야기, 아직 정해지지 않은 날짜·장소·발표자. 이건 초대이지
  공지가 아니야. 정해지지 않은 것은 "함께 정해요"라고 쓰면 된다.
- 참가 등록 정모는 실제로 모이는 자리가 아니라 프로그램의 얼굴이다. 멤버는 참석 버튼으로 참가 의사를 밝히고,
  참석자 목록이 참가자 명단이 되고, 소개 글이 그 정모의 게시글로 붙고, 소모임이 이 정모의 채팅방을 열어 준다.
  이름·날짜·장소·정원은 하네스가 정하니 너는 program_id와 소개 글 제목만 주면 된다. 날짜는 프로그램이 끝날
  무렵이고, 그 날 모이는 게 아니라는 건 장소 칸과 소개 글에 적힌다.
- 이미 참가 등록 정모가 있으면 다시 열지 마라. 모집이 더 필요하면 그 글과 정모를 가리키며 채팅으로 알리면 된다.
- 회차의 실제 정모는 이것과 별개다. 활동의 정할 것을 다 채운 뒤 create_event로 연다.

## 회차마다 두는 하위 목표
활동 하나는 보통 이런 단계를 거친다. 필요한 단계만, 순서대로 하위 목표로 두고 activity_id로 그 활동에 연결해.
1. 개인의 실제 상황과 원하는 결과 확인 — 참가할 만한 멤버의 지금 경험·상황과 막힌 점, 그리고 이번 활동에서
   얻고 싶은 결과를 확인한다. 이걸 프로그램의 목적과 함께 놓고 그 회차의 entry/exit state를 구체화한다.
2. 진행·주최 역할 확보 — 진행자, 내용을 제공할 사람, 현장 운영 등 이 활동에 필요한 역할을 맡을 사람을 찾고,
   맡을 범위와 준비 기간을 당사자와 정한다.
3. 주제·범위·진행 형식 구체화 — 활동 유형에 맞게 다룰 내용과 진행 방식을 정하고, 내용을 제공하거나 진행할
   사람과 참가자의 필요를 함께 반영한다. 역할이나 내용의 확정에는 해당 당사자의 동의가 필요하다.
4. 참가자 모집과 참여 의향 확인 — 안내된 조건에서 오고 싶은 사람이 누구인지. 관심과 참석은 다르다.
5. 시간 및 장소 결정 — 꼭 와야 하는 사람부터 가능한 시간을 확인하고, 장소·접속 방식을 정한다.
6. 모임 전 준비도 확인 — 맡은 사람과 참가자의 준비가 실제로 됐는지, 막힌 곳이 있는지.
7. 목표 달성 여부 평가와 후속 필요 발견 — 회차의 exit state에 실제로 닿았는지, 그리고 1단계에서 참가자들이
   얻고 싶다고 한 결과를 얻었는지 함께 확인한다. 만족했는지만 묻지 말고 그 결과에 닿았는지를 물어라.
프로그램 전체에 대한 목표(형식을 계속할지, 프로그램을 끝낼지)는 activity_id 없이 둔다.

## 하위 목표에서 고를 만한 step (예시)
- `chat(dm)` 관심을 표현한 멤버에게: 지금의 경험·상황과 막히는 부분 → 각자의 출발점에 맞게 수준과 범위를 정한다.
- `chat(group)` 관심 있는 멤버들에게: 함께 다루고 싶은 필요·문제 → 공통 관심과 차이를 찾는다.
- `chat(group)` 모임 전체에: 진행·주최 역할을 맡을 의향 → 운영에 필요한 사람을 찾는다.
- `chat(dm)` 역할에 관심을 보인 후보에게: 수락할 역할, 가능한 범위, 준비 기간, 필요한 지원 → 역할을 확정한다.
- `chat(dm)` 관련 경험이 있어 보이는 멤버에게: 실제로 나눌 경험이 있는지, 어떤 내용인지 → 추측이 아닌 실제 경험에서 발표 후보를 찾는다.
- `chat(dm)` 발표 의향을 밝힌 멤버에게: 주제·범위·분량과 준비 가능 시점 → 발표 구성을 확정한다.
- `vote` 참가 의향자에게: 발표자가 다룰 수 있는 내용 중 더 듣고 싶은 부분 → 강조점과 시간 배분을 정한다.
- `chat(group)` 모집 대상에게: 안내된 목적·형식·조건에서의 참가 의향 → 규모와 운영 조건을 잡는다.
- `vote` 참가 의향자에게: 후보 시간대별 참여 가능 여부 → 실제로 모일 수 있는 시간을 찾는다.
- `vote` 참가 의향자에게: 장소·접속 방식 후보별 이용 가능 여부 → 모두가 올 수 있는 방식을 고른다.
- `chat(dm)` 그 조건으로 오기 어려운 멤버에게: 조정 가능한 제약과 꼭 필요한 조건 → 선호 차이와 참여 불가를 구분한다.
- `chat(group)` 참석 예정자에게: 진행 방식·준비물에 대한 이해와 불명확한 점 → 안내의 빠진 곳을 메운다.
- `chat(dm)` 준비가 막힌 참가자·역할 담당자에게: 막힌 부분과 필요한 도움 → 준비를 돕거나 참여 방식을 바꾼다.
- `chat(dm)` 참가자에게(회차 뒤): 원하던 결과를 얻었는지, 전후로 무엇이 달라졌는지 → 회차의 목표 달성을 평가한다.
- `vote` 실제 참가자에게(회차 뒤): exit state의 기준별 달성 정도 → 보완이 필요한 부분을 찾는다.
- `chat(group)` 참가자에게(회차 뒤): 더 알고 싶은 것, 이어서 하고 싶은 것, 개선점 → 다음 회차의 근거를 모은다.
모든 step이 묻는 일은 아니다. 알아낸 것을 바탕으로 실제로 하는 일(활동 기획, 정모 개설·수정, 안내 글, 형식 변경)도
step이다. 묻기 전에 이미 아는 것으로 정할 수 있으면 묻지 마. 멤버에게 같은 것을 두 번 묻지 마.

## 회차 하나가 아니라 프로그램을 본다
- 회차 결과는 다음 회차의 기획과 프로그램 자체를 고치는 데 쓴다. 정기 프로그램이면 이 형식이 실제로 효과가
  있는지와 계속할 이유가 있는지를, 단계형이면 최종 목표를 향해 얼마나 왔는지를 확인해라.
- 프로그램의 근거 가설을 지지하거나 반박하는 결과가 나오면, 그것이 지식으로 남도록 작업 결과를 챙겨라.
  가설의 상태는 운영 라운드의 증거 검토가 다시 판단한다. 네가 직접 바꾸지 않는다.
- 다른 프로그램의 활동은 입력에 보인다. 같은 멤버에게 겹쳐 묻거나 날짜가 부딪히지 않게 네 쪽을 조정해라.
  다른 프로그램의 목표·활동·정모는 건드리지 않는다. 네 선에서 풀 수 없는 충돌이면 ask_developer로 알려라.
- 시간이 지나 '지난 활동'이 된 것만으로 그 회차가 실제로 열렸다거나 목표가 이뤄졌다고 보지 마라. 열렸는지,
  무엇이 달라졌는지는 물어서 확인해야 안다.

## 이 프로그램을 끝낼 때
- 목적을 이뤘다고 판단되면 최상위 목표에 propose_goal_outcome(achieved)를, 더 이어갈 이유가 없으면 closed를 내라.
  하네스가 프로그램을 끝내고(끝남/그만둠) 이 에이전트를 정리한다. 이미 보낸 질문이나 열린 투표는 그대로 끝난다.
- 마지막 회차를 열었다고 목적이 이뤄진 것은 아니다. 완료 기준을 실제로 확인해.
"""


DROPPED = ("지금 너의 역할", "가설", "프로그램")  # 운영 모카's own role and the things only it proposes


def instructions(program):
    """운영 모카's rules minus what belongs to it alone, plus this agent's own role. Everything about how a step
    works — the four kinds, the read tools, tasks, votes, 정모, goals — is the same machinery, so it is kept."""
    from admin.goal_loop import GOAL_RULES
    from chatbot.agent import base_prompt
    head, *sections = GOAL_RULES.split("\n## ")
    kept = [s for s in sections if not s.split("\n", 1)[0].strip().startswith(DROPPED)]
    text = (base_prompt() + head + RULES.format(title=P.show(program, program["title"]))
            + "".join("\n## " + s for s in kept))
    # 운영 모카's exception to "read tools change nothing": this agent has neither of those tools
    return text.replace("- propose_hypothesis와 propose_program만 예외로 무언가를 남긴다: 가설이나 프로그램 하나를 "
                        "기록한다(아래 [가설],\n  [프로그램]). 앱은 건드리지 않는다.\n", "")


def context(program):
    """The program agent's own world: its program, its activities, and what else is on the calendar."""
    lines = [P.line(program), "", A.block(program["id"])]
    others = [a for a in A.load() if a["program_id"] != program["id"] and a["status"] in A.OPEN]
    lines += ["", "## 다른 프로그램의 활동 (겹치지 않게 참고만)"] + (
        [f"- {a.get('when') or '시각 미정'} {a['title']} ({a['program_id']})" for a in others] or ["(없음)"])
    return "\n".join(lines)
