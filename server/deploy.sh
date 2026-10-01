#!/bin/bash
# 서버 코드를 최신으로 올리고 루프를 다시 시작한다.
#
# 이 파일은 /opt/moca/deploy.sh 의 원본이다. 돌고 있는 쪽은 복사본이다 — bash는 스크립트를
# 실행하면서 조금씩 읽어 나가므로, 깃이 돌고 있는 스크립트를 덮어쓰면 그 다음 줄부터 엉뚱하게
# 읽는다. 그래서 pull이 닿지 않는 자리에 복사해 두고 쓴다. 이 파일이 바뀌면 아래에서 알려준다.
#
# moca/data/ 는 .gitignore에 있으므로 건드리지 않는다 (멤버들의 1:1 기록과 동의 기록이 거기 있다).
set -euo pipefail
cd /opt/moca

# 서버에서 직접 고친 파일이 있으면 멈춘다. 조용히 덮어쓰면 그 수정은 흔적 없이 사라진다.
if ! git diff --quiet HEAD --; then
  echo "서버 작업 트리에 수정된 파일이 있습니다 — 배포를 멈춥니다:"
  git status --short
  exit 1
fi

git fetch --quiet origin
before=$(git rev-parse --short HEAD)
git merge --ff-only origin/main
after=$(git rev-parse --short HEAD)

if [ "$before" = "$after" ]; then
  echo "이미 최신입니다 ($after)"
  exit 0
fi
echo "$before → $after"
git --no-pager log --oneline "$before..$after"

systemctl restart moca-loop
sleep 4
systemctl is-active moca-loop

# 서비스 파일이나 이 스크립트 자신이 바뀌었으면, 복사해 넣는 것은 사람이 해야 한다.
if ! cmp -s server/deploy.sh /opt/moca/deploy.sh; then
  echo "알림: server/deploy.sh 가 바뀌었습니다 → cp server/deploy.sh /opt/moca/deploy.sh"
fi
for u in server/systemd/*.service; do
  n=$(basename "$u")
  if ! cmp -s "$u" "/etc/systemd/system/$n"; then
    echo "알림: $n 이 바뀌었습니다 → cp $u /etc/systemd/system/ && systemctl daemon-reload"
  fi
done
