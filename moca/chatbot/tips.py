"""'/tip' in a 1:1: one line from documents/tips.txt, picked at random.

Like /stardust, the harness answers it directly — no model call — so the tip is exactly what the file says.
Edit the file to change the tips; it is read on every request.
"""
import random

from chatbot.store import ROOT

TIPS = ROOT / "documents" / "tips.txt"
HEADER = "💡 모카 팁"


def load():
    lines = TIPS.read_text(encoding="utf-8").splitlines() if TIPS.exists() else []
    return [line.strip().lstrip("-").strip() for line in lines if line.strip().lstrip("-").strip()]


def parse_command(text):
    word = (text or "").strip().lower().split()
    return bool(word) and word[0] in ("/tip", "/팁")


def pick(last=None):
    """A random tip, never the same one twice in a row for the same member."""
    tips = load()
    if not tips:
        return None
    choices = [t for t in tips if t != last] or tips
    return random.choice(choices)


def message(tip):
    return f"{HEADER}\n{tip}"
