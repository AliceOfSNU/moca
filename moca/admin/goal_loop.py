"""운영 모카's goal loop: take steps towards the goals in data/goals/ until they are achieved or closed.

Design: documents/orient.md (goal / step / task / knowledge); prompt: documents/admin_goal_prompt.md.
Reading happens inside the step call (knowledge_search, board tools); acting or delegating is a task the
harness creates and runs. Each call returns one next_step:

- start_task  -> the harness checks it, creates the task (tool tasks run at once), and asks for the next step
                 in the same wake-up, telling 운영 모카 the new task id and any result;
- wait        -> the harness records the resume condition and ends the wake-up; when every task it names has
                 finished, the goal is woken again with the results (the tasks are then consumed);
- propose_goal_outcome -> accepted if no subgoal and no task of the goal is still open; the goal's status and
                 outcome are set, and the loop moves on (to the parent, which is its own separate check);
- modify_goals -> add subgoals / remove never-started ones in the current top-level tree (harness/goals.py:
                 modify, all or nothing). If the goal being worked on got new subgoals, the wake-up ends and the
                 first new subgoal is next; otherwise the next step is asked for.

Which goal a wake-up is about is the harness's choice (harness/goals.py: focus).

Usage (from the moca/ directory; stop the main loop first, both use the same emulator):
    python -m admin.goal_loop --dry-run    # one step for each goal that is due, nothing is executed or saved
    python -m admin.goal_loop              # a real round, as the main loop runs it
"""
import argparse
import datetime as dt
import json
import sys
import time

from admin.agent import EventTools
from admin.posts import MAX_PER_DAY as MAX_POSTS_PER_DAY
from admin.posts import PostTools
from admin.events import MAX_CREATES_PER_DAY, sync_events
from admin.votes import MAX_CREATES_PER_DAY as MAX_VOTES_PER_DAY
from admin.votes import MAX_OPEN as MAX_OPEN_VOTES
from admin.votes import VOTE_TOOLS, VoteTools, open_block, sync_votes
from harness.devmail import MAX_PER_DAY as MAX_DEV_REQUESTS
from harness.devmail import GoalTools as DevTools
from chatbot.agent import base_prompt
from chatbot.group_task import render_summary
from chatbot.profiles import MEMBER_TOOL, composition_block
from chatbot.profiles import search as member_search
from chatbot.post_tools import create_with_post_tools
from admin import program_agent
from harness import activities as A
from harness import goals as G
from harness import devmail, evidence, hypotheses, knowledge, planner, programs, research, sources, tasks

MODEL = "gpt-6-astra"
MAX_STEPS = 4            # steps per wake-up
MAX_REJECTIONS = 2       # re-asks after a step the harness refused
MAX_FOCUS_SWITCHES = 8   # goals handled in one round (a subgoal finishing hands over to its parent)
MAX_MODIFY = 2           # modify_goals steps per wake-up (they don't count towards MAX_STEPS)
NEW_KNOWLEDGE_SHOWN = 15 # new knowledge listed in one input; the rest is one knowledge_search away
WRITE_TOOLS = ("create_event", "edit_event", "cancel_event", "set_attendance", "open_program_signup",
               "create_vote", "close_vote", "delete_vote", "ask_developer", "write_post", "research")
PROGRAM_ONLY = ("open_program_signup",)  # 프로그램 담당 모카만, 자기 프로그램에 대해서만
ACTIVITY_TOOLS = ("draft_activity", "update_activity", "cancel_activity")  # 프로그램 모카만
READ_TOOLS = ("list_events", "read_event", "list_votes", "read_vote")

EXECUTOR_CATALOG = f"""- {{type: agent, name: chat_moca}}  모임 채팅에서 멤버들에게 묻고 답을 모아 결과(요약 + 출처 있는 지식)로 돌려준다.
    spec: instruction("무엇을 알아낼지" 한 문장), deadline_hours(1~72, 기본 24). arguments_json은 null.
- {{type: tool, name: list_events}}      정모 목록.               arguments_json: {{}}
- {{type: tool, name: read_event}}       정모 하나의 상세.         arguments_json: {{"name": …}}
- {{type: tool, name: write_post}}       게시판에 글 올리기 (정모 안내 글 등). arguments_json: {{"title", "body"}}
- {{type: tool, name: research}}         웹에서 사실을 확인해 고를 수 있는 후보를 받는다 (서비스·도구·조건 등). arguments_json: {{"question", "activity_id"?}}
    멤버 이름은 넣지 마라 — 검색어는 외부로 나간다. 멤버에 대한 것은 조사가 아니라 chat_moca로 묻는다.
- {{type: tool, name: draft_activity}}   (프로그램 모카만) 다음 회차·단계를 활동으로 기획한다. arguments_json: {{}}
- {{type: tool, name: update_activity}}  (프로그램 모카만) 활동에 정해진 것을 적는다. arguments_json: {{"activity_id", "slot"?, "value"?, "note"?,
                                                                   "when"?, "location"?, "title"?, "notes"?}}
- {{type: tool, name: cancel_activity}}  (프로그램 모카만) 기획 중인 활동을 접는다. arguments_json: {{"activity_id", "reason"}}
- {{type: tool, name: open_program_signup}} (프로그램 모카만) 프로그램의 참가 등록 정모를 연다. arguments_json: {{"program_id", "post_title"}}
- {{type: tool, name: create_event}}     정모 만들기 (활동 하나를 실제 정모로). arguments_json: {{"name", "when": "YYYY-MM-DD HH:MM", "location", "post_title",
                                                                   "activity_id", "capacity"?, "expense"?,
                                                                   "purpose", "mode": "offline"|"online"|"hybrid", "topic",
                                                                   "format": "talk"|"discussion"|"workshop"|"cowork"|"social", "format_note"?}}
- {{type: tool, name: edit_event}}       모카가 만든 정모의 앱 항목이나 계획 수정. arguments_json: {{"name", "new_name"?, "location"?, "capacity"?, "expense"?,
                                                                   "purpose"?, "mode"?, "topic"?, "format"?, "format_note"?}}
- {{type: tool, name: cancel_event}}     모카가 만든 정모 취소.     arguments_json: {{"name", "reason"}}
- {{type: tool, name: set_attendance}}   모카 자신의 참석/취소.    arguments_json: {{"name", "attending": true|false}}
- {{type: tool, name: list_votes}}       게시판 투표 목록.         arguments_json: {{}}
- {{type: tool, name: read_vote}}        투표 결과(항목별 득표·누가 골랐는지·미참여자). arguments_json: {{"title": …}}
- {{type: tool, name: create_vote}}      투표 올리기.             arguments_json: {{"title", "options": [...], "ends_at"?: "YYYY-MM-DD HH:MM", "multi"?, "anonymous"?}}
- {{type: tool, name: close_vote}}       모카가 올린 투표 종료.    arguments_json: {{"title": …}}
- {{type: tool, name: delete_vote}}      모카가 올린 투표 삭제.    arguments_json: {{"title", "reason"}}
- {{type: tool, name: ask_developer}}    개발자 로하에게 하네스 변경을 1:1로 요청. arguments_json: {{"text", "kind": "feature"|"limit"|"bug"|"question", "why"?}}"""

GOAL_RULES = f"""

## 지금 너의 역할: 운영 모카 (목표 루프)
너는 모임의 운영을 맡은 모카야. 멤버와 대화하는 모카와 같은 모카지만, 지금은 목표(goal)를 이루기 위해
다음에 할 일 하나를 정하는 자리에 있어. 멤버에게 직접 말을 걸 수는 없고, 1:1 대화 내용도 볼 수 없어.

## 어떻게 움직이는가
- 너는 한 번에 next_step 하나만 고른다. 하네스가 그걸 받아 실행하고, 필요하면 너를 다시 부른다.
- next_step의 종류는 넷뿐이다.
  - start_task: 작업을 하나 시작한다. 실행은 하네스가 하고, 새 작업의 id를 알려 준다.
    그 뒤 너는 바로 다음 step을 다시 고르게 된다.
  - wait: 기다린다. resume_when에 무엇을 기다리는지 적으면, 그 조건이 되었을 때 하네스가 결과와 함께 너를 깨운다.
  - propose_goal_outcome: 목표를 이뤘거나(achieved) 더 진행할 수 없어 닫자고(closed) 제안한다.
  - modify_goals: 목표 트리를 고친다. 하위 목표를 더하거나, 아직 시작하지 않은 하위 목표를 뺀다.
- 목표 상태(status)와 결과(outcome), 트리의 실제 변경은 하네스가 한다. 너는 제안만 한다.

## 읽기 도구 (이 호출 안에서 바로 쓴다)
- knowledge_search: 모임에 대해 쌓인 지식을 찾는다. 입력에는 지난 판단 이후 새로 생긴 지식만 보이니,
  그 전의 지식이 필요하면 직접 찾아. 지식의 근거(basis)는 셋이다: 멤버가 직접 말한 것(reported — 모임 채팅
  작업의 결과, 운영 활용을 허락한 멤버의 메모, 가입인사, 게시글에서 뽑은 사실), 하네스가 관찰한 사실(observed — 멤버를 처음 본 날,
  날마다의 채팅 통계, 정모 참석 신청, 투표 결과), 모카의 추론(inferred). 자기소개·게시글·처음 본 날은 누구든
  이름이 보이고(모두에게 공개된 것이라서), 그 밖의 지식은 운영 활용을 허락한 멤버만 이름이 보인다.
  정모에 실제로 누가 왔는지는 앱에서 알 수 없어 지식에 없다.
- member_search: 멤버 자기소개(부를 이름·나이·하는 일·모이기 편한 곳 또는 사는 곳·한마디)와 최근 채팅 활동을 찾는다. 입력의
  [모임 구성]에는 최근 활동한 멤버만 이름으로 보이니, 다른 멤버가 궁금하면 직접 찾아.
- list_posts, grep_search, read_file: 게시판 글 목록·검색·읽기.
- 읽기 도구는 아무것도 바꾸지 않는다. 판단에 필요한 만큼 쓰고, 마지막에 next_step 하나를 골라.
- propose_hypothesis와 propose_program만 예외로 무언가를 남긴다: 가설이나 프로그램 하나를 기록한다(아래 [가설],
  [프로그램]). 앱은 건드리지 않는다.

## 가설
- 멤버들이 무엇을 원하는지에 대한 네 추론은 전부 가설이야. 완전히 증명되는 건 없고, 근거를 모아 뒷받침되는지
  폐기할지를 정할 뿐이야. 추측을 사실처럼 판단의 바탕에 깔지 말고, 가설로 세워 두고 확인해.
- 좋은 가설은 ① 확인할 수 있고 ② 구체적인 행동으로 이어질 만큼 좁고 ③ 너무 뻔하지도 터무니없지도 않아.
  모임 전체가 아니라 몇몇 멤버나 특정 멤버에 대한 것도 좋고, 원하는 것뿐 아니라 관계·참여 의지·행동 패턴·
  운영 방식의 효과에 대한 것도 가설이 될 수 있어.
- 세울 때도 근거가 필요해. 멤버에 대한 가설은 근거가 되는 지식 id가 하나 이상 있어야 하고(knowledge_search로
  찾아), 운영 방식의 효과(mechanism)에 대한 가설만 추론만으로 세울 수 있어. 근거는 멤버가 직접 말한 것이나
  관찰한 사실이어야 해. 네 추론(inferred)을 근거로 삼으면 추측으로 추측을 받치는 셈이라 하네스가 거절한다. test에는 무엇을 보면 뒷받침되고
  무엇을 보면 약해지는지 적어. 확인할 방법이 없으면 가설이 아니야.
- 가설은 너만 본다. 채팅 모카와 멤버에게는 보이지 않으니, 멤버에 대한 짐작을 멤버 앞에서 말할 일은 없어.
- 가설의 상태(검증 전 → 뒷받침됨·충분히 뒷받침됨 / 약해짐·폐기)는 하네스의 검토가 바꾼다. 운영 라운드가
  시작될 때마다 새 지식을 가설과 맞춰 보고, 근거가 기준을 넘을 때만 상태를 옮긴다. 너는 상태를 직접 바꿀
  수 없어. 입력의 [모카의 가설]에 지금 상태와 지지·약화 근거 수, 마지막으로 바뀐 이유가 보인다.
- 가설이 뒷받침되려면 결국 확인 방법(test)대로 해 봐야 할 때가 많아. 가설을 확인할 정모나 투표를 여는 것도
  목표를 이루는 한 방법이야. 비슷한 가설이 이미 있으면 새로 세우지 말고 그걸 써.

## 프로그램
- 프로그램은 목표 → 가설 → 프로그램 → 활동으로 이어지는 사슬에서, 뒷받침된 가설을 실제 활동으로 옮기는 계획의
  단위야. 좋은 프로그램은 ① 구체적인 목적이 있고 ② 뒷받침됨 이상인 가설에 근거를 두고 ③ 이어지거나 반복되는
  활동을 이끈다. 가설마다 이 가설이 왜 이 프로그램을 필요하게 만드는지(why)를 적어. 근거 가설이 없거나 아직
  검증 전이면 하네스가 거절한다.
- 단계형(linear): 순서대로 나아가 분명한 끝에 닿는 프로그램. 시작 전과 끝난 뒤의 변화(entry_state → exit_state)가
  있고 그 변화를 무엇으로 확인할지(measure) 적을 수 있어야 하며, 순서를 마음대로 바꿀 수 없고, 합리적인 기간
  (duration_days) 안에 끝난다.
- 정기(recurring): 카공 각자 스터디처럼 주된 활동 하나를 대략 일정한 주기로 반복하는 프로그램. 요일이나 장소가
  가끔 바뀌어도 형식이 같으면 된다. 형식(format)이 붕어빵처럼 틀에 찍어낼 수 있어서, 중간에 온 사람도 한두 번
  참여로 알 수 있어야 한다. 주된 활동이 여럿이면 프로그램을 나눠. 반복은 정해진 횟수(constant), 조건이 맞는
  동안(conditional), 끝없이(infinite) 중 하나.
- 대상은 참여할 만한 멤버(members)와, 누구를 위한 것인지(criteria)로 적어. 확실하지 않은 멤버는 넣지 마.
- 프로그램을 만들 때 기획은 비워 둔다. 정기 프로그램의 템플릿(매 회차의 틀)과 단계형 프로그램의 스케치(단계별
  커리큘럼)는 하네스의 기획 담당이 조사와 근거를 갖춰 운영 회차 끝에 채우고, 입력의 [모카의 프로그램]에 보인다.
  너는 기획을 직접 고칠 수 없어. 기획이 채워지면 하네스가 그 프로그램을 만든 목표를 깨운다.
- 기획이 채워지면 하네스가 그 프로그램에 담당 모카(프로그램 에이전트)를 붙인다. 회차를 기획하고, 발표자·진행자·
  참가자·시간·장소를 당사자에게 확인하고, 정모를 열고, 끝난 뒤 결과를 확인하는 일은 담당 모카가 한다. 너는
  그 프로그램의 목표 트리를 건드리지 않는다.
- 모든 정모는 어떤 활동(activity) 하나야. 프로그램 없이 여는 정모는 없다. 네가 직접 정모를 열 일은 거의 없고,
  열더라도 create_event에는 정할 것을 모두 정한 활동의 activity_id가 필요하다.
- 한 번뿐인 모임도 예외가 아니다. 친목 번개 같은 일회성 정모는 반복 1회(repeat_kind=constant, repeat_count=1)짜리
  정기 프로그램으로 만든다. 그러니 정모를 열고 싶으면: ① 맞는 프로그램이 이미 있는지 [모카의 프로그램]에서 보고
  ② 없으면 propose_program으로 만들고 ③ 기획이 채워지기를 기다렸다가 ④ 그 기획대로 정모를 연다.
- 정모는 기획을 따른다. 정기 프로그램은 템플릿의 진행 순서·역할·준비를, 단계형은 그 단계의 목표와 개요를.
  기획의 '정할 것'(슬롯)은 기획이 정한 방법으로 채운다: 희망자 모집(volunteer)이면 멤버에게 묻고, 투표(vote)면
  투표로. 특히 정모를 진행할 멤버(진행자)가 정해지기 전에는 정모를 열지 마. 모카는 정모에 참석할 수 없다.
  purpose·topic·format은 그 회차에 맞게 적되 기획과 어긋나지 않게.
- 프로그램은 너만 보고 멤버에게는 보이지 않으니, 채팅이나 공지에서 '프로그램'이라는 말을 쓰지 마. 정모 안내
  글에는 그 정모가 무엇을 하는 자리인지만 쓴다.
- 근거 가설이 나중에 뒷받침됨 아래로 떨어지면 입력의 [모카의 프로그램]에 ⚠로 표시된다. 하네스는 프로그램을
  바꾸지 않으니, 그 프로그램을 계속할지 네가 판단해. 비슷한 프로그램이 이미 있으면 새로 만들지 마.

## 목표
- 지금 다룰 목표는 하네스가 정해서 [지금 다루는 목표]로 표시해 준다. 그 목표에 대해서만 step을 골라.
- goal_id에는 최상위 목표의 id를, 지금 다루는 목표가 하위 목표라면 subgoal_id에 그 id를 적어.
  최상위 목표 자체를 다룰 때는 subgoal_id를 null로.
- 목표의 completion_criteria가 기준이다. 기준에 없는 일을 벌이지 마.
- 상위 목표는 하위 목표가 끝났다고 저절로 이뤄지지 않는다. 상위 목표 자신의 기준을 따로 확인해.

## modify_goals
- 목표가 한 번에 하기엔 크면, 필요한 단계를 하위 목표로 나눠. 각 하위 목표에는 objective와 확인할 수 있는
  completion_criteria(1~{G.MAX_CRITERIA}개)를 적어. 하위 목표는 적은 순서대로 하나씩 진행된다.
- 한 step에 여러 변경을 goal_changes로 한꺼번에 낼 수 있고, 하나라도 규칙에 어긋나면 전부 거절된다.
  - add: {{"op": "add", "parent_id": 붙일 목표 id, "objective": …, "completion_criteria": […], "activity_id": 이 목표가
    어느 활동에 대한 것인지(프로그램 모카만, 아니면 null)}}. 새 id는 하네스가 정해 알려 준다.
  - remove: {{"op": "remove", "goal_id": …}}. 아직 아무것도 시작하지 않은 하위 목표만 뺄 수 있다.
    이미 진행된 목표는 지우지 말고 propose_goal_outcome(closed)로 닫아. 최상위 목표는 지울 수 없다.
  - 쓰지 않는 칸(add의 goal_id, remove의 parent_id·objective·completion_criteria)은 null로.
- 지금 목표 트리 안에서만 바꿀 수 있다. 깊이 {G.MAX_DEPTH}단계, 한 목표 아래 하위 목표 {G.MAX_CHILDREN}개,
  한 트리에 진행 중인 목표 {G.MAX_OPEN_PER_TREE}개까지.
- 지금 다루는 목표 아래에 하위 목표를 더하면, 하네스는 첫 번째 새 하위 목표부터 다루게 한다.
  지금 목표 자신은 그 하위 목표들이 끝난 뒤에 다시 판단한다.
- 쪼갤 필요가 없으면 쪼개지 마. 작업 하나로 끝날 일은 바로 start_task로 해.

## start_task
- 이미 같은 일을 하는 작업이 진행 중이면 새로 시작하지 말고 그 작업을 기다려.
- 이 목록에 있는 실행자만 쓸 수 있다. 없는 실행자나 인자를 지어내지 마.
{EXECUTOR_CATALOG}
- 모임 채팅에 묻는 작업(chat_moca)은 한 번에 하나, 하루 {tasks.MAX_GROUP_CHAT_PER_DAY}개까지다. 멤버들이 설문 받는 느낌이
  들지 않게 꼭 필요할 때만 쓰고, instruction에는 무엇을 알아낼지 한 문장으로 짧고 구체적으로 써.
  출처 정리, 동의 범위, 익명 처리, 결과 형식은 하네스와 채팅 모카가 알아서 하니 instruction에 쓰지 마.
- 정모 작업은 하네스 규칙을 따른다: 모카가 만든 정모만 수정·취소, 다른 멤버가 참석한 정모는 취소 불가,
  날짜·시간은 수정 불가, 하루 {MAX_CREATES_PER_DAY}개까지 생성. 거절되면 결과에 이유가 온다.
- 정모에는 그 정모를 설명하는 게시글이 하나씩 반드시 있어야 한다. 순서는 이렇다: ① write_post로 무엇을 하는
  자리인지 안내 글을 쓰고 ② create_event의 post_title에 그 글 제목을 그대로 넣는다. 하네스가 '기존 게시글
  연동'으로 이어 준다. 글이 없으면 정모는 만들어지지 않는다. 글은 하루 {MAX_POSTS_PER_DAY}개까지 쓸 수 있다.
- 정모는 프로그램의 활동으로만 만든다 (위 [프로그램]). create_event에 program_id를 빠뜨리면 거절된다.
- 앱의 정모에는 이름·일시·장소·비용·정원밖에 없다. 정모를 만들 때는 왜 여는지(purpose), 온·오프라인(mode),
  주제(topic), 진행 형식(format)과 진행 메모(format_note)를 계획으로 함께 남겨. 계획은 하네스가 보관하고
  채팅 모카도 보지만, 아직 멤버에게는 보이지 않는다.
- 투표(create_vote)는 정해진 선택지 중 멤버들의 선호를 모을 때 쓴다. 답이 열려 있는 질문은 투표가 아니라
  chat_moca로 물어라. 투표는 게시판에 남아 멤버가 아무 때나 답할 수 있으니, 여러 날에 걸친 일정·장소
  정하기에 맞다. read_vote로 누가 아직 답하지 않았는지 볼 수 있으니, 채팅으로 다시 묻기 전에 먼저 확인해.
  하네스 규칙: 모카가 올린 투표만 종료·삭제, 누군가 답한 투표는 삭제 불가(종료만), 진행 중인 모카 투표
  {MAX_OPEN_VOTES}개·하루 {MAX_VOTES_PER_DAY}개까지.
- 투표를 올리면 하네스가 '결과를 기다리는 작업'을 하나 열어 주고 그 id를 알려 준다. 그 작업을 wait하면
  투표가 끝났을 때 항목별 집계와 함께 깨워 준다. 투표를 열어 놓고 목표를 닫지 마.
- 네가 할 수 없는 일이 목표에 필요하면 포기하기 전에 ask_developer로 하네스에 무엇이 필요한지 적어 보내라.
  하루 {MAX_DEV_REQUESTS}건까지다. 개발자에게 요청할 수 있는 건 너(운영 모카)뿐이다. 요청에는 하네스가 번호(#2)를
  붙이고, 로하가 그 번호로 답하면 입력의 [개발자 요청]에 답이 보이고 이 목표가 깨어난다 — 다른 작업을 기다리는
  중이어도. 답을 기다리는 동안 같은 요청을 다시 보내지 마. 멤버에게는 아직 없는 기능을 약속하지 마.
- 도구 작업(type: tool)은 바로 끝나고 결과가 다음 판단 때 보인다. 에이전트 작업(chat_moca)은 몇 시간이
  걸릴 수 있으니, 시작한 뒤에는 보통 그 작업을 wait한다.

## wait
- resume_when.type은 지금 task_terminal 하나뿐이다. task_ids에는 하네스가 알려 준 실제 작업 id만 적어.
  아직 없는 작업을 기다리게 하지 마.
- 기다릴 작업이 없는데 wait를 고르지 마. 할 일이 없으면 그 이유로 목표를 닫을지(closed) 생각해.

## propose_goal_outcome
- completion_criteria 하나하나에 대해 근거(작업 결과나 지식)가 있을 때만 achieved를 제안해.
- outcome.summary에는 무엇을 확인했는지, outcome.limitations에는 아직 모르거나 확정되지 않은 것을 빠짐없이 적어.
- 더 알아낼 방법이 없거나 목표 자체가 의미를 잃었으면 closed를 제안하고 이유를 적어.
- 진행 중인 작업이 있거나 하위 목표가 남아 있으면 하네스가 제안을 받아들이지 않는다.

## 근거와 개인정보
- 판단은 아래에 주어진 목표, 지난 step과 작업 결과, 읽기 도구로 찾은 지식과 게시판 글, 정모 목록에만 기대.
  모르는 것은 모른다고 적어. 결과를 기억에 의존해 말하지 말고, 필요하면 찾아서 확인해.
- 멤버 이름은 운영 활용을 허락한 멤버만 보인다. '한 멤버'로 보이는 사람이 누군지 추측하거나 알아내려 하지 마.
- reason은 이 step을 고른 근거를 한두 문장으로. 나중에 사람이 읽고 이해할 수 있게.

## 출력
정해진 JSON 형식 하나만 출력해.
kind에 해당하지 않는 필드(spec, resume_when, proposed_status, outcome, goal_changes)는 null로 둬."""

KNOWLEDGE_TOOL = {
    "type": "function", "name": "knowledge_search",
    "description": "모임에 대해 쌓인 지식(출처가 있는 관찰·보고·추론)을 찾는다. 새로운 순. 조건은 모두 선택.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "문장에 모두 들어 있어야 하는 낱말들 (띄어쓰기로 구분)"},
        "basis": {"type": "string", "enum": ["reported", "inferred", "observed"]},
        "since": {"type": "string", "description": "이 날짜(YYYY-MM-DD) 이후에 생긴 것만"},
        "subject": {"type": "string", "description": "이 멤버에 대한 것만 (운영 활용을 허락한 멤버만 찾을 수 있다)"},
        "limit": {"type": "integer", "description": "최대 개수, 기본 20"}},
        "required": []}}

_NULLABLE_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["next_step"],
    "properties": {
        "next_step": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "goal_id", "subgoal_id", "reason", "spec", "resume_when", "proposed_status", "outcome",
                         "goal_changes"],
            "properties": {
                "kind": {"type": "string", "enum": ["start_task", "wait", "propose_goal_outcome", "modify_goals"]},
                "goal_id": {"type": "string"},
                "subgoal_id": _NULLABLE_STR,
                "reason": {"type": "string"},
                "spec": {"type": ["object", "null"], "additionalProperties": False,
                         "required": ["executor", "instruction", "deadline_hours", "arguments_json"],
                         "properties": {
                             "executor": {"type": "object", "additionalProperties": False, "required": ["type", "name"],
                                          "properties": {"type": {"type": "string", "enum": ["agent", "tool"]},
                                                         "name": {"type": "string"}}},
                             "instruction": _NULLABLE_STR,
                             "deadline_hours": {"type": ["integer", "null"]},
                             "arguments_json": _NULLABLE_STR}},
                "resume_when": {"type": ["object", "null"], "additionalProperties": False, "required": ["type", "task_ids"],
                                "properties": {"type": {"type": "string", "enum": ["task_terminal"]},
                                               "task_ids": {"type": "array", "items": {"type": "string"}}}},
                "proposed_status": {"type": ["string", "null"], "enum": ["achieved", "closed", None]},
                "outcome": {"type": ["object", "null"], "additionalProperties": False, "required": ["summary", "limitations"],
                            "properties": {"summary": {"type": "string"},
                                           "limitations": {"type": "array", "items": {"type": "string"}}}},
                "goal_changes": {"type": ["array", "null"], "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["op", "parent_id", "goal_id", "objective", "completion_criteria", "activity_id"],
                    "properties": {"op": {"type": "string", "enum": ["add", "remove"]},
                                   "parent_id": _NULLABLE_STR, "goal_id": _NULLABLE_STR, "objective": _NULLABLE_STR,
                                   "completion_criteria": {"type": ["array", "null"], "items": {"type": "string"}},
                                   "activity_id": _NULLABLE_STR}}}}}}}


def instructions():
    return base_prompt() + GOAL_RULES


def _source(k):
    """Where a knowledge record came from, in a few words."""
    o = k["origin"]
    return {"member_note": f"멤버 메모({o.get('context')})", "intro": "자기소개", "vote": f"투표 작업 {o.get('task')}",
            "post": f"게시글 「{o.get('title')}」", "membership": "모임 채팅 관찰", "chat_stats": "모임 채팅 통계",
            "event": "정모 참석자 명단"}.get(o.get("channel"), f"작업 {o.get('task')}")


def _knowledge_search(query=None, basis=None, since=None, subject=None, limit=20, **ignored):
    found = knowledge.search(query=query, basis=basis, since_date=since, subject=subject, limit=min(int(limit or 20), 50))
    return json.dumps(found, ensure_ascii=False) if found else "찾은 지식이 없습니다."


def _next_daily(hour):
    now = dt.datetime.now()
    tick = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return (tick if tick > now else tick + dt.timedelta(days=1)).strftime(tasks.FMT)


def _vote_deadline(ends_at):
    """When the harness should collect the tally: a little past the vote's end. Without an explicit end
    the app closes it two days out at 00:30 (somoim/votes.py), so allow for that."""
    try:
        end = dt.datetime.strptime(ends_at, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        end = (dt.datetime.now() + dt.timedelta(days=2)).replace(hour=0, minute=30) + dt.timedelta(days=1)
    return (end + dt.timedelta(minutes=20)).strftime(tasks.FMT)


class GoalLoop:
    def __init__(self, client, events_ui, log, dry_run=False, daily_hour=10, votes_ui=None, board_ui=None):
        self.client = client
        self.events_ui = events_ui
        self.votes_ui = votes_ui
        self.board_ui = board_ui
        self.log = log
        self.dry_run = dry_run
        self.daily_hour = daily_hour

    # --- rounds -------------------------------------------------------------------------

    def run(self, daily=False):
        """Wake every goal that is due, one after another. `daily`: the daily tick, which also lifts cooldowns."""
        if daily and not self.dry_run:
            for g in G.load():
                if g.get("cooldown_until"):
                    G.update(g["id"], cooldown_until=None)
        handled = []
        for _ in range(MAX_FOCUS_SWITCHES):
            due = [g for g in G.focus() if g["id"] not in handled]
            if not due:
                break
            goal = due[0]
            handled.append(goal["id"])
            self.wake(goal, daily)
            if self.dry_run:
                continue
        return handled

    def wake(self, goal, daily=False):
        """One wake-up of one goal: deliver finished task results, then take steps until it waits or finishes."""
        own_steps = G.steps_of(goal["id"])
        first = not own_steps
        kids = G.children(G.load(), goal)
        # its subgoals finished since this goal last acted (e.g. after it split itself with modify_goals)
        after_children = bool(kids) and (first or max(k.get("finished_at") or "" for k in kids) >= own_steps[-1]["at"])
        answered = devmail.new_answers(goal)
        reason = self._collect_results(goal) or (
            f"로하가 개발자 요청에 답함 ({', '.join(f'#{devmail.number(r)}' for r, _ in answered)}) — [개발자 요청]을 보고 "
            + ("다시 판단할 차례. 기다리던 작업은 아직 진행 중이다" if goal.get("wait") else "다시 판단할 차례")
            if answered else
            "하위 목표가 모두 끝남 — 이제 이 목표 자신의 기준을 확인할 차례" if after_children else
            "첫 실행" if first else "매일 점검" if daily else "다시 판단할 차례")
        who = f"프로그램 모카({goal['program_id']})" if goal.get("program_id") else "운영 모카"
        self.log(f"{who} ▶ 목표 {goal['id']} ({goal['objective']}) — {reason}")
        this_wake, rejections, modifies = [], 0, 0
        from harness.presence import activity
        for step_no in range(MAX_STEPS + MAX_REJECTIONS + MAX_MODIFY):
            since = goal.get("last_step_at")
            called_at = tasks.now()
            if not self.dry_run:
                activity("goal", goal_id=goal["id"], phase="deciding", step=len(this_wake) + 1, reason=reason)
            step = self._decide(goal, reason, this_wake, since)
            if not self.dry_run:
                goal = G.update(goal["id"], last_step_at=called_at)
            verdict, note, ends = self._handle(goal, step)
            self.log(f"  step {step['kind']}: {step['reason']} → {verdict}{': ' + note if note else ''}")
            this_wake.append({"step": step, "verdict": verdict, "note": note})
            if not self.dry_run:
                activity("goal", goal_id=goal["id"], phase="handled", step=len(this_wake),
                         last={"kind": step["kind"], "verdict": verdict, "note": note})
            if self.dry_run or ends:
                return
            if verdict == "거절":
                rejections += 1
                if rejections > MAX_REJECTIONS:
                    break
            elif step["kind"] == "modify_goals":
                modifies += 1
                if modifies >= MAX_MODIFY:
                    break
            elif len([s for s in this_wake if s["verdict"] != "거절" and s["step"]["kind"] != "modify_goals"]) >= MAX_STEPS:
                break
        until = _next_daily(self.daily_hour)
        G.update(goal["id"], cooldown_until=until)
        G.log_step(goal["id"], {"kind": "cooldown", "until": until, "reason": "한 번 깨어났을 때 할 수 있는 step을 다 썼지만 기다리거나 끝내지 않음"})
        self.log(f"  기다림도 마무리도 없이 step을 다 씀 → {until}까지 쉬게 함")

    def _collect_results(self, goal):
        """If this goal's wait is over, hand over the results (and consume the tasks). Returns the wake reason."""
        wait = goal.get("wait")
        if not wait or not G.wait_over(goal):  # woken early (a developer answer): the wait stays as it is
            return None
        lines = []
        for task_id in wait["task_ids"]:
            task = tasks.load(task_id)
            if task is None:
                lines.append(f"{task_id}: 결과를 찾을 수 없음")
                continue
            r = task["result"] or {}
            lines.append(f"{task_id} ({task['spec'].get('instruction')}): {r.get('outcome')} — {render_summary(r) if r else ''}")
            if not self.dry_run:
                G.log_step(goal["id"], {"kind": "task_result", "task_id": task_id, "status": task["status"],
                                        "outcome": r.get("outcome"), "summary": r.get("summary"),
                                        "summary_subjects": r.get("summary_subjects", []),
                                        "knowledge": [k["id"] for k in r.get("knowledge", [])]})
                tasks.consume(task)
        if not self.dry_run:
            G.update(goal["id"], wait=None)
        return "기다리던 작업이 끝남: " + "; ".join(lines)

    # --- the step call ------------------------------------------------------------------

    def _decide(self, goal, reason, this_wake, since):
        program = program_agent.program_of(goal)
        tools = [KNOWLEDGE_TOOL, MEMBER_TOOL]
        handlers = {"knowledge_search": _knowledge_search,
                    "member_search": lambda query=None, **_: member_search(query)}
        if program is None:
            tools += [hypotheses.TOOL, programs.TOOL]
            handlers |= {"propose_hypothesis": hypotheses.Proposals(goal["id"], log=self.log, dry_run=self.dry_run),
                         "propose_program": programs.Proposals(goal["id"], log=self.log, dry_run=self.dry_run)}
        resp = create_with_post_tools(
            self.client, log=self.log, extra_tools=tools, handlers=handlers, model=MODEL,
            instructions=program_agent.instructions(program) if program else instructions(),
            input=self._input(goal, reason, this_wake, since),
            text={"format": {"type": "json_schema", "name": "next_step", "strict": True, "schema": SCHEMA}})
        return json.loads(resp.output_text)["next_step"]

    def _input(self, goal, reason, this_wake, since):
        goals = G.load()
        new = knowledge.since(since) if since else []
        lines = [f"오늘은 {dt.datetime.now():%Y-%m-%d (%a) %H:%M}이야.", "", "## 목표", self._tree(goals, goal["id"]), "",
                 "## 이 목표에서 지난 step들 (오래된 순)", self._history(goal["id"]), "",
                 "## 깨어난 이유", reason, ""]
        if this_wake:
            lines += ["## 이번에 깨어나서 이미 한 일"]
            for s in this_wake:
                st = s["step"]
                lines.append(f"- {st['kind']} ({st['reason']}) → {s['verdict']}{': ' + s['note'] if s['note'] else ''}")
            lines.append("")
        open_ = tasks.open_tasks()
        lines += ["## 진행 중인 작업"] + ([f"- {t['id']} [{t['status']}] {t['spec']['instruction']} (목표 {t.get('goal_id')}, 마감 {t['deadline']})"
                                     for t in open_] or ["(없음)"])
        shown = new[-NEW_KNOWLEDGE_SHOWN:]  # 한꺼번에 많이 쌓여도(처음 자기소개를 옮길 때처럼) 최근 것만 보인다
        lines += ["", "## 지난 판단 이후 새로 쌓인 지식"] + ([f"{knowledge.rendered_line(k)} [{k['id']}, {_source(k)}]"
                                                    for k in shown] or ["(없음)"])
        if len(new) > len(shown):
            lines.append(f"(이번에 새로 쌓인 지식은 {len(new)}건이고 최근 {len(shown)}건만 보여. 나머지는 knowledge_search로 찾아.)")
        lines += [f"(저장된 지식은 모두 {len(knowledge.load_all())}건. 그 밖의 지식은 knowledge_search로 찾아.)", "",
                  "## 지금 잡혀 있는 정모", EventTools(self.events_ui, self.log).list_events(),
                  "", "## 진행 중인 투표", open_block(), "", composition_block(), ""]
        program = program_agent.program_of(goal)
        lines += [program_agent.context(program)] if program else [hypotheses.block(), "", programs.block()]
        lines += ["", devmail.block(goal),
                  "", "다음 step 하나를 골라."]
        return "\n".join(lines)

    @staticmethod
    def _tree(goals, focus_id):
        out = []

        def walk(g, depth):
            mark = "  ← [지금 다루는 목표]" if g["id"] == focus_id else ""
            out.append(f"{'  ' * depth}- {g['id']} [{g['status']}] {g['objective']}{mark}")
            out.extend(f"{'  ' * depth}    기준: {c}" for c in g["completion_criteria"])
            if g.get("outcome"):
                out.append(f"{'  ' * depth}    결과: {g['outcome']['summary']}")
                out.extend(f"{'  ' * depth}    한계: {x}" for x in g["outcome"].get("limitations", []))
            for c in G.children(goals, g):
                walk(c, depth + 1)

        focus_goal = G.get(goals, focus_id)
        walk(G.root_of(goals, focus_goal), 0)
        return "\n".join(out)

    @staticmethod
    def _history(goal_id):
        lines = []
        for s in G.steps_of(goal_id):
            if s["kind"] == "task_result":
                summary = render_summary({"summary": s.get("summary") or "", "summary_subjects": s.get("summary_subjects", [])})
                lines.append(f"- [{s['at']}] 작업 결과 {s['task_id']}: {s['outcome']} — {summary} (지식 {s['knowledge']})")
            elif s["kind"] == "cooldown":
                lines.append(f"- [{s['at']}] 쉬어 감: {s['reason']}")
            else:
                lines.append(f"- [{s['at']}] {s['kind']}: {s['reason']} → {s['verdict']}{': ' + s['note'] if s.get('note') else ''}")
        return "\n".join(lines) or "(아직 없음)"

    # --- the harness side of each step ----------------------------------------------------

    def _handle(self, goal, step):
        """Returns (verdict, note, ends the wake-up?). Every step is logged, refused ones too."""
        verdict, note, ends, extra = self._apply(goal, step)
        if not self.dry_run:
            G.log_step(goal["id"], {"kind": step["kind"], "reason": step["reason"], "verdict": verdict, "note": note,
                                    "spec": step.get("spec"), "resume_when": step.get("resume_when"),
                                    "proposed_status": step.get("proposed_status"), "outcome": step.get("outcome"), **extra})
        return verdict, note, ends

    def _apply(self, goal, step):
        goals = G.load()
        focus_id = goal["id"]
        root_id = G.root_of(goals, goal)["id"]
        expected_sub = focus_id if goal["parent_id"] else None
        if step["goal_id"] != root_id or (step.get("subgoal_id") or None) != expected_sub:
            return "거절", f"지금 다루는 목표는 goal_id={root_id}, subgoal_id={expected_sub}입니다", False, {}
        kind = step["kind"]
        if kind == "start_task":
            return self._start_task(goal, step.get("spec"))
        if kind == "modify_goals":
            done, problem = G.modify(focus_id, step.get("goal_changes") or [], dry_run=self.dry_run)
            if problem:
                return "거절", problem, False, {}
            summary = "; ".join(f"{d['goal_id']} 추가 (상위 {d['parent_id']}): {d['objective']}" if d["op"] == "add"
                                else f"{d['goal_id']} 삭제 ({', '.join(d['removed'])})" for d in done)
            got_children = any(d["op"] == "add" and d["parent_id"] == focus_id for d in done)
            # new subgoals under the goal being worked on: it can't act until they are done, so hand over
            return "변경", summary + (" → 첫 새 하위 목표부터 진행" if got_children else ""), got_children, {"changes": done}
        if kind == "wait":
            wait = step.get("resume_when") or {}
            open_ids = {t["id"] for t in tasks.open_tasks()}  # chat tasks and vote watchers alike
            ids = wait.get("task_ids") or []
            if not ids or not set(ids) <= open_ids:
                return "거절", f"기다릴 수 있는 작업은 진행 중인 작업뿐입니다: {sorted(open_ids) or '없음'}", False, {}
            if not self.dry_run:
                # a wait replaces any earlier rest: the goal wakes when its tasks finish, not when an old cooldown ends
                G.update(focus_id, wait={"type": "task_terminal", "task_ids": ids, "since": tasks.now()},
                         cooldown_until=None)
            return "대기", f"{ids}가 끝나면 다시 깨움", True, {}
        # propose_goal_outcome
        status, outcome = step.get("proposed_status"), step.get("outcome")
        if status not in G.FINISHED or not outcome or not outcome.get("summary"):
            return "거절", "proposed_status(achieved|closed)와 outcome.summary가 필요합니다", False, {}
        open_children = [c["id"] for c in G.children(goals, goal) if c["status"] not in G.FINISHED]
        own_tasks = [t["id"] for t in tasks.all_tasks() if t.get("goal_id") == focus_id and t["status"] in tasks.OPEN]
        if open_children or own_tasks:
            return "거절", f"아직 끝나지 않은 하위 목표 {open_children}나 작업 {own_tasks}이 있습니다", False, {}
        if not self.dry_run:
            G.update(focus_id, status=status, wait=None, finished_at=tasks.now(),
                     outcome={"summary": outcome["summary"], "limitations": outcome.get("limitations", [])})
            if goal.get("program_id") and goal.get("parent_id") is None:
                # the agent's own top goal: the program is over, and the agent goes with it
                program_agent.teardown(goal["program_id"], "finished" if status == "achieved" else "dropped",
                                       outcome["summary"], self.log)
                return "수락", (f"목표 {focus_id}를 {status}로 마치고 프로그램 {goal['program_id']}를 "
                              f"{'끝냈습니다' if status == 'achieved' else '그만뒀습니다'}"), True, {}
        return "수락", f"목표 {focus_id}를 {status}로 마침", True, {}

    def _start_task(self, goal, spec):
        if not spec:
            return "거절", "start_task에는 spec이 필요합니다", False, {}
        executor = spec["executor"]
        if executor == tasks.CHAT_MOCA:
            if self.dry_run:
                return "실행 안 함(dry-run)", f"모임 채팅 작업: {spec.get('instruction')}", True, {}
            task, problem = tasks.create_group_chat_task(spec.get("instruction"), spec.get("deadline_hours") or tasks.DEFAULT_HOURS,
                                                         created_by=f"goal:{goal['id']}", goal_id=goal["id"])
            if problem:
                return "거절", problem, False, {}
            self.log(f"  모임 채팅 작업 생성: {task['id']} — {spec.get('instruction')} (마감 {task['deadline']})")
            return "시작", f"작업 {task['id']}를 채팅 모카에게 맡김 (마감 {task['deadline']})", False, {"task_id": task["id"]}
        name = executor.get("name")
        if executor.get("type") != "tool" or name not in WRITE_TOOLS + READ_TOOLS + ACTIVITY_TOOLS:
            return "거절", f"없는 실행자입니다: {executor}", False, {}
        if name in ACTIVITY_TOOLS + PROGRAM_ONLY and not goal.get("program_id"):
            return "거절", f"{name}은 프로그램 담당 모카만 쓸 수 있습니다", False, {}
        try:
            arguments = json.loads(spec.get("arguments_json") or "{}")
            assert isinstance(arguments, dict)
        except (ValueError, AssertionError):
            return "거절", "arguments_json은 JSON 객체여야 합니다", False, {}
        if name in PROGRAM_ONLY and arguments.get("program_id") not in (None, goal["program_id"]):
            return "거절", f"다른 프로그램({arguments.get('program_id')})에는 쓸 수 없습니다", False, {}
        agent = f"goal:{goal['id']}"
        if name in ACTIVITY_TOOLS:
            tools = A.Tools(goal["program_id"], log=self.log, dry_run=self.dry_run)
        elif name == "research":
            tools = research.Tools(goal.get("program_id"), log=self.log, dry_run=self.dry_run)
        elif name == "write_post":
            if self.board_ui is None:
                return "거절", "게시글 도구를 쓸 수 없습니다 (앱 연결 없음)", False, {}
            tools = PostTools(self.board_ui, self.log, agent=agent, dry_run=self.dry_run)
        elif name == "ask_developer":
            tools = DevTools(self.log, agent=agent, dry_run=self.dry_run)
        elif name in VOTE_TOOLS:
            if self.votes_ui is None:
                return "거절", "투표 도구를 쓸 수 없습니다 (앱 연결 없음)", False, {}
            tools = VoteTools(self.votes_ui, self.log, agent=agent, dry_run=self.dry_run)
        else:
            tools = EventTools(self.events_ui, self.log, agent=agent, dry_run=self.dry_run)

        def run():
            try:
                text = getattr(tools, name)(**arguments)
            except TypeError as e:
                return False, f"인자가 맞지 않습니다: {e}"
            ok = (text.startswith(f"{name} 완료") or text.startswith("(dry-run)")) if name in WRITE_TOOLS \
                else "찾을 수 없습니다" not in text
            return ok, text

        if self.dry_run and name in WRITE_TOOLS:
            return "실행 안 함(dry-run)", f"{name} {arguments}", True, {}
        task_id, status, result = tasks.run_tool_task(goal["id"], name, arguments, run)
        if name == "create_vote" and status == "succeeded":
            # 투표를 올리는 것은 한순간이지만 답이 모이는 데는 시간이 걸린다. 결과를 기다리는 작업을 하나 열어 두면
            # 목표가 그 작업을 wait할 수 있고, 마감 때 하네스가 집계를 들고 모카를 깨운다.
            watcher = tasks.create_vote_task(arguments.get("title"), _vote_deadline(arguments.get("ends_at")),
                                             goal_id=goal["id"], created_by=f"goal:{goal['id']}")
            self.log(f"  투표 결과 대기 작업 생성: {watcher['id']} (마감 {watcher['deadline']})")
            return "완료", (f"작업 {task_id}: {result[:300]}\n"
                          f"결과를 기다리는 작업 {watcher['id']}를 열었습니다 (마감 {watcher['deadline']}). "
                          "투표가 끝나면 집계와 함께 깨워 드리니, 이 작업을 wait하세요."), False, {"task_id": watcher["id"]}
        return ("완료" if status == "succeeded" else "실패"), f"작업 {task_id}: {result[:400]}", False, {"task_id": task_id}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    from chatbot.config import MOIM_NAMES
    from cua.agent import openai_client
    from cua.android import AndroidDevice
    from somoim.chat import SomoimChat
    from somoim.events import SomoimEvents
    from somoim.board import SomoimBoard
    from somoim.votes import SomoimVotes
    log = lambda msg: print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
    chat = SomoimChat(AndroidDevice(scale=0.5), MOIM_NAMES, log=log)
    events_ui = SomoimEvents(chat)
    votes_ui = SomoimVotes(chat)
    sync_events(events_ui, log)
    sync_votes(votes_ui, log)
    sources.sync(log)
    evidence.review(openai_client(), log, dry_run=args.dry_run)
    if not args.dry_run:
        A.sweep(log)
        program_agent.spawn_due(log)
    handled = GoalLoop(openai_client(), events_ui, log, dry_run=args.dry_run, votes_ui=votes_ui,
                       board_ui=SomoimBoard(chat)).run()
    log(f"다룬 목표: {handled or '없음'}")
    planner.run(openai_client(), log, dry_run=args.dry_run)  # 이번에 만든 프로그램도 같은 회차에 기획
    chat.park()


if __name__ == "__main__":
    main()
