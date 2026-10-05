# 활동 신청·체크인 사이트

활동마다 Dots가 만든 사이트(`<활동 id>/website/`)와 그 행사 설정(`<활동 id>/event.json`)을 둔다.
서버에서는 `server/systemd/moca-activity-site.service`가 하나를 돌린다. 신청 답변은
`/opt/moca/activity-data/<활동 id>/`에 쌓이고, 그 폴더는 깃에도 웹에도 나가지 않는다.

- a_20261005_6bfb — Dots로 레스토랑 경영하기 (10/10). 포트 8080.
