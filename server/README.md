# 모카가 사는 서버

모카는 안드로이드 에뮬레이터 위의 소모임 앱을 직접 조작한다. 그래서 서버는 KVM(중첩 가상화)이
되는 장비여야 한다 — 보통의 클라우드 VM에서는 에뮬레이터가 뜨지 않는다. 지금 쓰는 것은 카페24
베어메탈 임대 장비(Ubuntu 22.04)다.

이 디렉터리에는 장비를 다시 세울 때 필요한 것만 있다. **`moca/data/` 는 여기에 없다** — 멤버들의
1:1 대화, 동의 기록, 멤버 노트는 깃에 들어가지 않는다(`.gitignore`). 새 장비를 세운 뒤에는 쓰던
장비에서 `moca/data/` 를 따로 옮겨야 한다. 옮겼으면 메시지 수와 지식 레코드 수를 세어 맞는지 본다.

## 바깥에 열린 포트는 22 하나뿐이다

대시보드(8765)와 VNC(5900)에는 **인증이 없다**. 둘 다 `127.0.0.1` 에만 묶여 있고, SSH 터널로만 본다:

    ssh -N -L 8765:127.0.0.1:8765 -L 5900:127.0.0.1:5900 root@<서버>

방화벽에서 이 포트를 열면 모임 멤버들의 1:1 대화가 그대로 공개된다. 열지 않는다.
SSH는 키로만 들어온다(`PasswordAuthentication no`).

## 세우는 순서

1. 패키지

       apt install -y xvfb x11vnc python3-venv unzip openjdk-17-jdk

2. 안드로이드 SDK를 `/opt/android` 에 풀고 에뮬레이터와 시스템 이미지를 받는다.

   **API 34(안드로이드 14)를 쓴다.** API 36(안드로이드 16) 이미지는 이 장비에서 surfaceflinger가
   죽는 고리에 빠져(`ReadColorBufferDma` assertion, uiautomator가 빈 트리를 돌려줌) 쓸 수 없었다.
   Xvfb·Mesa·에뮬레이터 버전·`-gpu` 설정을 모두 바꿔 봤지만 이미지 쪽 문제였다.

3. AVD를 만든다. 이름은 `moca34` (`server/systemd/moca-emulator.service` 가 이 이름을 쓴다).

4. 서비스를 올려 에뮬레이터를 띄우고, VNC 터널로 화면을 보면서 **소모임 앱에 모카 계정으로
   직접 로그인한다.** 이 단계는 사람이 해야 한다.

5. 한글 입력기. `tools/ADBKeyboard.apk` 는 깃에 없으니 따로 받아서 설치한다.

       adb install ADBKeyboard.apk
       adb shell ime enable com.android.adbkeyboard/.AdbIME

   IME 목록이 갱신되기 전에는 `enable` 이 한 번 실패할 수 있다. 안 되면 다시 한다.
   기본 입력기로 두지는 않는다 — 코드가 입력할 때만 바꿔 쓰고 되돌린다(`cua/android.py`).

6. 코드. 읽기 전용 deploy key를 깃허브 저장소에 등록하고,

       git clone git@github.com:AliceOfSNU/moca.git /opt/moca

7. 파이썬

       python3 -m venv /opt/moca/.venv
       /opt/moca/.venv/bin/pip install -r /opt/moca/requirements.txt

8. OpenAI 키는 `/opt/moca/moca/.env` 에 넣고 `chmod 600`. 깃이나 채팅으로 옮기지 않는다.

9. 서비스

       cp server/systemd/*.service /etc/systemd/system/
       cp server/wait-for-boot.sh server/deploy.sh /opt/moca/
       chmod +x /opt/moca/wait-for-boot.sh /opt/moca/deploy.sh
       systemctl daemon-reload
       systemctl enable --now moca-xvfb moca-emulator moca-vnc moca-dashboard moca-loop

   `moca-loop` 은 `wait-for-boot.sh` 로 안드로이드 부팅이 끝날 때까지 기다린다(최대 5분).

## 배포

    /opt/moca/deploy.sh

fetch 후 fast-forward 하고 `moca-loop` 을 재시작한다. 서버에서 직접 고친 파일이 있으면 멈추고
보여 준다. 서비스 파일이나 `deploy.sh` 자신이 바뀌었으면 복사해 넣으라고 알려 준다 —
그 복사는 사람이 한다.

## 기기를 옮기면 대화창은 비어 있다

소모임은 1:1과 단체 채팅 기록을 **보낸 기기에** 보관한다. 새 장비의 에뮬레이터에서는 대화창이
비어 보인다. 모카의 기억(`moca/data/`)은 그대로이므로 아는 것을 잃지는 않지만, 지난번 읽은
위치를 화면에서 찾지 못한다. 그래서:

- 단체 채팅은 위치를 못 찾은 회차에는 답하지 않고 위치만 다시 잡는다(`run.py`).
- 1:1 동의 안내는 화면이 아니라 하네스가 적어 둔 발송 기록을 믿는다. 그러지 않으면 회차마다
  안내문을 다시 보내고, 멤버가 이미 쓴 '네'를 영원히 보지 못한다(`chatbot/dm.py`).
