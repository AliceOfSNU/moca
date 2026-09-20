기본 정보
- 이 대시보드는 개발자가 루프를 디버깅 하고 상태를 확인하기 위한 UI이다.
- 기본적으로 모임의 다른 멤버들에게는 보여지지 않는다.
- 모카의 상태를 실시간으로 알려주는 서버와, 프론트엔드 UI dashboard로 이루어져 있다.
- 서버와 frontend는 아무 언어와 프레임워크를 사용해도 좋다. 추천 조합은 python server + javascript frontend이다. 
- 서버 코드는 moca코드와 최대한 분리하라. 이상적으로는 아예 다른 패키지로 구성하라.

## goal setting and list
사용자가 직접 goal json을 입력하게 하라.
form을 사용해서 직접 json 포맷을 입력하지 않고도 빈칸을 채워넣어서 goal을 완성할 수 있게 하라.
이렇게 만들어진 goal은 top-level goal이고, submit하면 운영 agent에 전달하면 된다. (이미 실행중인 goal이 있는 경우 입력을 막아라.)
goal들의 리스트를 만들어라. top-level goal만 표시하면 충분하다. subgoal들은 아래 per-goal page에 간다. 

## per-goal page
goal마다 페이지를 따로 만들어라. page는 아래 항목들을 표시하라.

### visualizing the goal tree
each node is a (sub)goal, clicking on it shows the goal's json with syntax highlighting.
status에 따라 다른 색깔을 사용하라. 완료된 goal에 대해서는 result를 표시하라.

### visualizing the agent status
goal session이 돌고 있는지, chat session이 돌고 있는지 눈에 띄게 표시하라.
현재 goal과 최근 step들을 표시하라. 각 step은 눌러서 expand해서 그 json을 볼 수 있다.
task list를 표시하라. task의 종류와 status, executor는 항상 표시하고, full json을 task item을 눌러 확인할 수 있게 하라. task의 완료 상태에 따라 다른 색으로 표시하라.

## knowledge db
그냥 쭉 목록을 나열하면 된다. sort by created_at.

---

### 구현 메모 (2026-09-19)

- **위치와 실행**: `illit/dashboard/` (moca와 나란한 별도 패키지, moca 코드를 import하지 않음).
  `illit/`에서 `python -m dashboard` → http://127.0.0.1:8765 (localhost에만 열림, 멤버는 접근 불가).
  다른 데이터 폴더나 포트: `python -m dashboard --data PATH --port N`. 표준 라이브러리만 쓰므로 설치할 것 없음.
- **moca와의 계약은 data/ 파일뿐**: goals.json, steps.jsonl, tasks/log.jsonl과 살아 있는 task 파일, knowledge/records.jsonl,
  members/scope_consent.json, runtime/status.json·presence.json을 읽는다.
- **루프 상태**: 목표 세션/채팅 세션이 돌고 있는지는 run.py 메모리에만 있어서, 루프가 활동이 바뀔 때마다
  `data/runtime/status.json`에 쓰게 했다(`harness/presence.py`의 `activity`). idle / chat / dm / post / memory / goal
  (goal이면 목표 id, step 번호, 판단 중인지). 마지막 신호가 5분보다 오래되면 '루프 멈춤'으로 표시.
- **실시간**: 서버가 위 파일들을 1초마다 확인해 바뀌면 전체 상태를 SSE(`/api/stream`)로 보낸다. `?snapshot`을 붙이면
  한 번만 불러온다(스크린샷용).
- **목표 입력**: 폼(목표, 완료 기준 1~5개, id 선택) → 서버가 goals.json에 바로 쓴다. 쓰기 직전에 다시 읽고, 하네스와
  같은 규칙(objective 200자, 기준 1~5개, 진행 중인 최상위 목표가 있으면 거절)으로 검사하고, 임시 파일에 쓴 뒤 교체한다.
  루프가 같은 파일을 저장하는 순간과 겹치는 아주 짧은 틈은 남아 있다(두 프로그램이 한 파일을 쓰는 구조의 한계).
  루프는 2분 안에 새 목표를 알아채고 첫 step을 시작한다.
- **이름**: 운영 모카가 보는 그대로 — 운영 활용을 허락한 멤버만 이름, 나머지는 '한 멤버'. 채우지 못한 자리표시자도 '한 멤버'.
- **화면**: 목표(폼 + 최상위 목표 목록), 목표별 페이지(트리·노드 JSON, 에이전트 상태·최근 step, 작업 목록, 메시지 흐름),
  메시지 흐름(전체), 지식(통계 + created_at 정렬 목록).
