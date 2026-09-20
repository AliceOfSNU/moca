###
운영 agent는 어떤 목표(Goal)가 있을 때 그 목표가 달성될 때까지 loop하며
액션을 실행한다. 액션은 하네스의 결정(step)과 그걸 harness가 받아 실제 실행되는 작업
(delegate, tool call, separate llm call 등)인 Task로 이루어져 있다.
그리고 결정(step)을 하기 위해 필요한 정보는 knowledge로 관리한다.
아래에서 개념들의 정의를 살펴볼 수 있다.

| 개념          | 표현                             |
| ----------- | ------------------------------ |
| `goal`      | 완료 조건을 가진 목표 레코드               |
| `subgoal`   | `parent_id`가 있는 goal. 별도 타입 없음 |
| `step`      | LLM이 이번 판단에서 선택한 다음 진행 방법      |
| `task`      | step에 따라 실제로 생성·실행되는 작업 레코드    |
| `knowledge` | 출처가 있는 관찰·보고·추론 레코드            |

우리는 당장 goal loop를 시작하기 보다, bottom-up으로 가장 구체적인 task부터 시작할 것이다.
### llm: next step proposal
resume_when: 
```json
{
  "goal_proposals": [],
  "next_step": {
    "kind": "wait",
    "goal_id": "g_find_time",
    "subgoal_id": "sg_find_time",
    "reason": "공통 시간대 분석 작업이 진행 중이다",
    "resume_when": {
      "type": "task_terminal",
      "task_ids": ["t_compare_times"]
    }
  }
}
```
- goal_proposals는 최종 goal 달성을 위한 subgoal들을 추가하는 기능이고, 아직 쓰이지 않는다.
- next_step이 중요하다.
  - kind: 
     | `kind`                 | 의미                     |
    | ---------------------- | ---------------------- |
    | `start_task`           | 작업을 실행하거나 다른 에이전트에 맡긴다 |
    | `wait`                 | 기존 작업 결과나 특정 사건을 기다린다  |
    | `propose_goal_outcome` | 목표의 달성 또는 종료를 제안한다     |
  - goal_id/subgoal_id: 지금은 하나의 고정된 예시 goal/subgoal을 내가 만들어줄것이다.
  - reason: 해당 kind를 고른 근거.
  - resume_when: 이건 wait step에만 해당하는 항목이다.
llm이 next_step을 위와같은 형식으로 제시하면 harness가 이걸 받아 처리한다.
예를 들어 위의 wait 스텝에서는 resume_when조건을 읽고 재개 조건을 기록한다.

#### start_task step
```json
{
  "goal_proposals": [],
  "next_step": {
    "kind": "start_task",
    "goal_id": "g_find_time",
    "subgoal_id": "sg_find_time",
    "reason": "공통 시간대를 물어서 확인해야한다.",
    "spec": {
        "executor": {
        "type": "agent",
        "name": "moca_think"
        },
        "instruction": "허용된 멤버별 가능 시간을 비교해 공통 시간대 후보를 찾아라",
        "input_refs": ["k_member_a_time", "k_member_b_time"],
    }
  }
}
```

harness action: task생성하고 실행.
result가 도착하면 wait의 resume_when 조건을 확인하고 해당되면 운영 에이전트를 깨움(해당 result 전달)

#### propose_goal_outcome step
목표가 달성되었는지 확인한다.
```json
{
  "kind": "propose_goal_outcome",
  "goal_id": "g_find_time",
  "reason": "공통 시간대 후보와 근거를 확보했고, 아직 확인되지 않은 범위도 구분했다.",
  "proposed_status": "achieved",
  "outcome": {
    "summary": "A와 B가 함께 참여할 수 있는 시간대 후보로 토요일 오후를 확인했다.",
    "limitations": [
      "다른 멤버들의 참여 가능 시간은 아직 확인되지 않았다.",
      "특정 날짜의 참석이 확정된 것은 아니다."
    ]
  }
}
```
- proposed_status: enum. achieved와 closed를 두자.
- outcome: 말그대로 결과
  
harness action: 이 시점에서 모든 subgoal이 완료되었고 추가 subgoal제안이 없으면, 루프를 빠져나올 수 있다. 이 때, goal schema에서 status와 outcome필드를 업데이트 한다. 

#### 테스트를 위한 예시 goals
```json
[
  {
    "id": "g_first_study",
    "parent_id": null,
    "objective": "첫 자율스터디 정모를 개설한다",
    "completion_criteria": [
      "정모가 등록되었음을 확인했다"
    ],
    "status": "active",
    "outcome": null
  },
  {
    "id": "g_find_time",
    "parent_id": "g_first_study",
    "objective": "첫 스터디의 공통 시간대 후보를 파악한다",
    "completion_criteria": [
      "공통 시간대 후보와 이를 뒷받침하는 멤버별 근거가 있다",
      "확인된 범위와 아직 모르는 범위가 구분되어 있다"
    ],
    "status": "active",
    "outcome": null
  }
]
```
- parent_id가 설정되어 있으면 subgoal이다.
- completion_criteria는 이 goal을 달성했다고 판단하는 기준이다.
  - propose_goal_outcome step에서 여기에 비춰 완료를 판단한다.
- status와 outcome은 goal이 완료됨에 따라 하네스에 의해 수정된다.

이 goal을 운영모카 loop에 넣는 방법을 나와 의논하고, 운영모카의 루프 시스템 프롬프트 초안을 작성하라.

### harness: creates a task
Moca가 start task를 next step으로 지정하면 하네스가 받아서 task객체를 만들고 실행한다. 그리고 output을 감싸 task result 객체로 만든다.
task와 task result객체는 task가 생성되어서 끝날때까지만 존재하면 된다. 남는 기록일 필요가 없다.
task객체는 아래의 형식을 가진다 (진짜 json이어야 할 필요는 없다. 스키마를 설명하기 위한 도구이다.)

```json
{
  "id": "t_compare_times",
  "spec": {
    "executor": {
      "type": "agent",
      "name": "moca_think"
    },
    "instruction": "허용된 멤버별 가능 시간을 비교해 공통 시간대 후보를 찾아라",
    "input_refs": ["k_member_a_time", "k_member_b_time"],
  },
  "status": "running",
  "result": null
}
```
- spec,status,result가 top-level 필수필드이다.
- spec에서는 executor 항목만 필수필드이다.
- type과 name은 harness가 실행 자원을 찾는데 사용되며, 예를 들어 type:tool,name:post_grep_search 같은 식이다.
- spec의 나머지 필드들은 task executor의 input이다.
  - tool의 경우, 아래와 같은 spec형식을 갖는다.
  - ```json
    {
        "executor": {
            "type": "tool",
            "name": "post_grep_search"
        },
        "arguments": {
            //arguments to the grep_search tool
        }
    }
    ```
- result객체는 TaskResult 객체의 id를 가리킨다. 아니면 Task Result객체를 저기에 바로 박아넣어도 된다. 어쨌거나 Moca가 이 Task Result객체를 받기만 하면 된다.
- task 상태로 가능한 필드는: queued → running → succeeded 이다. 
  
Task의 한 종류만 구현해보자. 바로 Group Chat Task이다.
spec은 아래와 같다.
```json
{
  "executor": {
    "type": "agent",
    "name": "chat_moca"
  },
  "target": {
    "channel": "group_chat"
  },
  "instruction": "자율스터디에 참여하기 편한 대략적인 시간대를 확인하라.",
  "input_refs": ["k_first_study_purpose"]
}
```
- input_refs는 아직 신경 안써도 된다. 생략해도 좋다.
- 이 task는 챗모카에게 지시를 전달하면 챗모카가 적당히 채팅을 통해 지시를 수행하고 결과를 반환하는 task이다. 우리 시스템의 첫번째 agent간 통신이다.
 
### result
챗모카가 작성하는 output은 다음과 같다.
```json
{
  "outcome": "answer_available",
  "summary": "멤버 A의 대략적인 참여 가능 시간과 선호를 확인했다.",
  "knowledge_candidates": [
    {
      "statement": "멤버 A는 주말 오후에 대체로 참여 가능하다고 밝혔다.",
      "subjects": ["member_a"],
      "basis": "reported",
      "source_refs": ["record_31"]
    },
    {
      "statement": "멤버 B는 주말 중 토요일을 더 선호한다고 밝혔다.",
      "subjects": ["member_b"],
      "basis": "reported",
      "source_refs": ["record_32"]
    }
  ]
}
```
- outcome: 성공 실패여부. 부분성공(partial_answer)도 있으면 좋겠다.
- summary: 결론
- knowledge_candidates: 새로 얻게된 지식. 
- source_refs: 출처. 여기서는 채팅의 특정 메시지를 감싼 객체이다.
- basis: enum. reported/inferred 등을 두자.
실패했을 경우엔 아래와 같다.
```json
{
  "outcome": "no_shareable_answer",
  "summary": "이 작업에서 운영에 제공할 수 있는 참여 가능 시간 정보를 확보하지 못했다.",
  "knowledge_candidates": []
}
```
### 챗모카의 수신함
harness가 group chat task를 생성했을 때 어떻게 그걸 챗모카한테 넘겨줄지
정해야 한다. 아마 일종의 메시지함을 사용해서 요청들을 쌓고, 챗모카가 루프에서 주기적으로 체크하면 될 것 같다. 챗모카가 하네스한테 결과를 돌려주는 건 어떻게 할지 정해보라.



---

### 구현 메모 (Group Chat Task, 2026-09-19)

- **수신함과 결과 반환**: 두 에이전트가 같은 `run.py` 프로세스에 있어서, 수신함은 `data/tasks/<id>.json` 파일이다
  (`harness/tasks.py`). 루프가 자주 재시작되므로 살아 있는 task는 파일로 남기고, 운영 모카가 결과를 읽으면
  파일을 지우고 `data/tasks/log.jsonl`에 한 줄만 남긴다. 챗모카는 결과를 task 파일의 `result`에 쓰고
  status를 바꾸는 것으로 돌려준다(하네스가 대신 씀). 운영 모카는 다음 점검 때 '끝난 작업'으로 받는다.
- **실행자는 기존 챗모카**: 묻기·조기 마무리 판단·결과 보고·마무리 인사 모두 `ChatAgent`(같은 시스템 프롬프트)가
  한다. `chatbot/group_task.py`는 그 둘레의 하네스일 뿐 모델을 따로 부르지 않는다. 진행 중에는 챗모카의 평소
  답장 프롬프트에도 "지금 알아보는 중인 것"이 붙는다.
- **상태**: queued → running → succeeded | failed. 마감(deadline, 1~72시간, 기본 24)은 하네스가 정하고,
  마감 전이라도 챗모카가 충분하다고 판단하면 조기 마무리. outcome은 answer_available / partial_answer /
  no_shareable_answer. 질문 전송이 3번 실패하면 failed.
- **하네스 규칙**: 모임 채팅 task는 한 번에 하나, 하루 2개. 결과 검증 — source_refs는 task 구간의 실제 메시지
  id(`m<줄번호>`, `chatbot/store.py`)여야 하고, reported는 subjects 본인이 인용 메시지를 쓴 경우만, 문장에
  나온 멤버 이름은 모두 자리표시자({s0})로 바꿔 저장. 통과 못 한 후보는 버린다.
- **개인정보**: 이름은 보여줄 때마다 그 멤버의 현재 활용 범위 동의로 채운다 — 허락한 멤버만 이름,
  나머지는 '한 멤버'. 그래서 /memory off가 이미 쌓인 knowledge에도 바로 적용된다.
- **knowledge**: `data/knowledge/records.jsonl` (`harness/knowledge.py`). 운영 모카 프롬프트에 최근 20개가 붙는다.
- **확인**: `python -m harness.tasks list|new|show`, `python -m harness.knowledge`.
- **아직 없음**: input_refs, moca_think 실행자, 1:1 채널 task.


### 구현 메모 (goal loop, 2026-09-19)

- **파일**: `harness/goals.py`(목표·step 기록, focus 규칙), `admin/goal_loop.py`(step 결정 호출과 하네스 처리, 프롬프트는
  `GOAL_RULES`), 설계·프롬프트 설명은 `documents/admin_goal_prompt.md`.
- **저장**: 목표는 `data/goals/goals.json`, step과 받은 작업 결과는 `data/goals/steps.jsonl`(task 파일은 읽고 나면 지워지므로
  목표의 이력은 여기에 남는다). 도구 task는 즉시 끝나 파일 없이 `data/tasks/log.jsonl`에만 남는다.
- **읽기 vs 행동**: step 호출 안에서 읽기 도구(knowledge_search, 게시판 도구)를 쓰고, 행동·위임(chat_moca, 정모 도구 6개)은
  task. 프롬프트에는 이 목표의 지난 step 호출 이후 새로 생긴 지식만 들어가고, 나머지는 knowledge_search로 찾는다.
- **focus**: 하네스가 정한다. active + 대기 없음(또는 대기 끝) + 하위 목표 모두 끝남. 형제는 만든 순서대로 하나씩.
  상위 목표는 자동 완료되지 않고, 하위 목표가 끝나면 깨어나 자기 기준으로 따로 판단한다.
- **깨우기**: 목표의 wait(task_terminal)가 끝나면 `run.py`가 바로 목표 루프를 돌리고(결과 전달 후 task 소비), 매일 10시에도 돈다
  (쉬는 중인 목표를 깨움). 기존의 자유 형식 하루 점검은 목표 루프로 대체.
- **한도**: 한 번 깨어날 때 step 4개, 거절 후 재질문 2번, 한 라운드에 목표 5개. step을 다 쓰고도 wait/outcome이 없으면 다음 매일 점검까지 쉼.
- **확인**: `python -m harness.goals list|seed|steps <id>`, `python -m admin.goal_loop --dry-run`.
- **하위 목표 제안**: goal_proposals 대신 독립 step `modify_goals`(add / remove, all or nothing, 최상위 트리 안,
  시작 안 한 하위 목표만 삭제). 자세한 규칙은 `documents/admin_goal_prompt.md`의 modify_goals 절.
