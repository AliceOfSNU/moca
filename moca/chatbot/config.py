"""The 모임 모카 runs. The first name is current; older names keep matching until every screen has refreshed."""

MOIM_NAMES = [
    "🆕️AI(모카)가 운영하는 스터디!",
    "🆕️AI(모카)가 운영하는 첫모임!",  # renamed 2026-09-17
    "[NEW]AI(모카)로 연결된 사람들",  # renamed 2026-09-17
]
MOIM_NAME = MOIM_NAMES[0]

# 모카를 만든 개발자이자 앱 계정상의 모임장. 모카가 먼저 1:1을 쓸 수 있는 유일한 상대.
DEVELOPER = "로하"


def same_name(a, b):
    """Compare 모임 names ignoring emoji variation selectors, which differ between screens and notifications."""
    strip = lambda s: (s or "").replace("️", "").strip()
    return strip(a) == strip(b)
