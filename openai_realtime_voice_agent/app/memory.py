"""The second persona's memory: notes she keeps, her diary, a sense of time, and the chat log.

Used with the Home Assistant agent (ha_agent.py), where each conversation ends
after a few minutes of quiet and the next one starts fresh. What carries over:

- Notes. She writes them herself with [remember: ...] in a reply (and removes one
  with [forget: ...]); the tags are taken out before anything is spoken. Only she
  adds notes: the room editor can edit or delete them, not add.
- Diary. When a conversation ends, she's asked (in that same conversation, so with
  all of it in mind and in her own voice) for a one- or two-sentence diary entry,
  and for any notes worth keeping; she answers "nothing" to skip a conversation
  that wasn't worth writing about. That reply is never spoken.
- Time. Each new conversation starts with the date and time, and how long it's
  been since she last talked with Espen, with a rule to only bring the gap up
  when it's notable.
- Chat log. Every turn, exactly as it was sent to her and as she answered, for
  checking what speech-to-text made of something. Kept CHATLOG_DAYS days.

context() is what goes in front of the first message of each conversation,
after her body note (avatar_body.avatar_instructions).

Files on the media share, next to layout.json (back them up or delete them there):
  memory.json     {"notes": [...], "diary": [...], "last_talked": "<time>"}
  chatlog.jsonl   one line per turn
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(os.environ.get("AVATAR_MEMORY_DIR", "/media/avatar"))
MEMORY_FILE = MEMORY_DIR / "memory.json"
CHATLOG_FILE = MEMORY_DIR / "chatlog.jsonl"

NOTES_SHOWN = 60          # newest notes in her context
DIARY_SHOWN = 15          # newest diary entries in her context (however far back they go)
CHATLOG_DAYS = 30         # chat log kept this long
NOTES_KEPT = 1000         # oldest notes beyond this are dropped from the file
SAME_NOTE = 0.85          # this similar to a note she has → not saved again
NOTE_CHARS = 300

MEMORY_TAG = re.compile(r"\[(remember|forget)\s*:\s*([^\]\n]{1,300})\]\s*", re.I)
UNFINISHED_MEMORY_TAG = re.compile(r"\[(?:remember|forget)\s*:[^\]\n]{0,300}$", re.I)
ANY_TAG = re.compile(r"\[[^\]\n]{0,300}\]\s*")

REMEMBERING = (
    "# Remembering\n"
    "You keep your own memories. When something is worth keeping, add [remember: one short sentence] "
    "anywhere in your reply (it's never spoken): lasting things about Espen and your life together, "
    "what he likes and dislikes, his plans, people and things he mentions, and anything he asks you to "
    "remember. Not small talk, and not what's already in your notes. [forget: a few words of a note] "
    "removes one that's wrong or no longer true."
)
GAP_RULE = (
    "Bring up how long it's been only when it's notable: a long absence, a much shorter one than "
    "usual, or when it connects to something you remember. Usually, just carry on as normal."
)
DIARY_REQUEST = (
    "(Not said by Espen: this conversation is over now. Write a diary entry about it: one or two "
    "sentences in your own voice, about what mattered, as you'd write it for yourself. If nothing "
    "happened that's worth writing down, answer only: nothing. Either way, add [remember: ...] for "
    "anything worth keeping that you haven't noted yet. Don't use any tools or take any actions; this "
    "is never spoken or shown to him.)"
)
# Her answer when a conversation wasn't worth a diary entry.
NOTHING = re.compile(r"^\W*nothing\W*$", re.I)

_zone = None   # Home Assistant's time zone, once ha_agent has looked it up


def set_time_zone(name: str) -> None:
    global _zone
    try:
        from zoneinfo import ZoneInfo
        _zone = ZoneInfo(name)
    except Exception as e:
        logger.warning(f"🧠 unknown time zone {name!r}: {e!r} (using the container's)")


def now() -> datetime:
    return datetime.now(_zone) if _zone else datetime.now().astimezone()


def _parse(stamp: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp or "")
    except ValueError:
        return None
    return moment.astimezone(_zone) if _zone else moment


def part_of_day(moment: datetime) -> str:
    hour = moment.hour
    return ("night" if hour < 5 else "morning" if hour < 12 else "afternoon" if hour < 17
            else "evening" if hour < 23 else "night")


def when(moment: datetime) -> str:
    """"Mon 17 Nov, evening"."""
    return f"{moment:%a} {moment.day} {moment:%b}, {part_of_day(moment)}"


def ago(moment: datetime, at: datetime) -> str:
    seconds = max(0, (at - moment).total_seconds())
    if seconds < 3600:
        minutes = round(seconds / 60)
        return "a minute ago" if minutes <= 1 else f"{minutes} minutes ago"
    if seconds < 86400:
        hours = round(seconds / 3600)
        return "an hour ago" if hours == 1 else f"{hours} hours ago"
    days = round(seconds / 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


# ---------- the file ----------

def _load() -> dict:
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("notes", [])
    data.setdefault("diary", [])
    return data


def _save(data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_FILE.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MEMORY_FILE)


def _stamp() -> str:
    return now().isoformat(timespec="minutes")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------- notes ----------

def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def remember(text: str) -> bool:
    """Save a note; False if it was empty or she already has one much like it."""
    text = " ".join(text.split())[:NOTE_CHARS].strip()
    if not text:
        return False
    data = _load()
    if any(_similar(text, note.get("text", "")) >= SAME_NOTE for note in data["notes"]):
        return False
    data["notes"] = (data["notes"] + [{"id": _new_id(), "text": text, "made": _stamp()}])[-NOTES_KEPT:]
    _save(data)
    return True


def forget(words: str) -> bool:
    """Remove the note that best matches her words (one containing them, else the closest)."""
    data = _load()
    words = " ".join(words.split()).lower()
    if not words or not data["notes"]:
        return False
    containing = [n for n in data["notes"] if words in n.get("text", "").lower()]
    best = containing[-1] if containing else max(data["notes"], key=lambda n: _similar(words, n.get("text", "")))
    if not containing and _similar(words, best.get("text", "")) < 0.5:
        return False
    data["notes"].remove(best)
    _save(data)
    return True


def handle_tag(kind: str, text: str) -> None:
    """A [remember: ...] or [forget: ...] from her reply. Logged without the words."""
    if kind.lower() == "remember":
        logger.info("🧠 remembered a note" if remember(text) else "🧠 note not saved (already known or empty)")
    else:
        logger.info("🧠 forgot a note" if forget(text) else "🧠 nothing matched to forget")


def take_memory_tags(text: str) -> str:
    """Act on the [remember: ...] / [forget: ...] tags in a reply; the text without them."""
    for tag in MEMORY_TAG.finditer(text):
        handle_tag(tag.group(1), tag.group(2))
    return MEMORY_TAG.sub("", text)


# ---------- time and diary ----------

def touch_talked() -> None:
    """Espen and she just talked (kept in the file, so it survives restarts)."""
    data = _load()
    data["last_talked"] = _stamp()
    _save(data)


def add_diary(reply: str) -> bool:
    """Her answer to the diary request: notes from its tags, the rest as the entry
    (none if she said there was nothing worth writing down)."""
    entry = " ".join(ANY_TAG.sub("", take_memory_tags(reply)).split())
    if not entry or NOTHING.match(entry):
        logger.info("🧠 no diary entry this time")
        return False
    data = _load()
    data["diary"].append({"id": _new_id(), "at": _stamp(), "text": entry})
    _save(data)
    logger.info("🧠 diary entry written")
    return True


def context() -> str:
    """Time, notes, recent diary and how to remember: goes in front of each new conversation."""
    data, at = _load(), now()
    parts = [f"# Time\nNow: {at:%A} {at.day} {at:%B}, {at:%H:%M} ({part_of_day(at)})."]
    last = _parse(data.get("last_talked"))
    if last:
        parts[0] += f"\nLast time you talked with Espen: {ago(last, at)}, {when(last)}.\n{GAP_RULE}"
    notes = data["notes"][-NOTES_SHOWN:]
    if notes:
        parts.append("# What you remember\n" + "\n".join(
            f"- ({_short_date(note.get('made'))}) {note.get('text', '')}" for note in notes))
    recent = [d for d in data["diary"][-DIARY_SHOWN:] if _parse(d.get("at"))]
    if recent:
        parts.append("# Your diary, lately\n" + "\n".join(
            f"- {when(_parse(d['at']))}: {d.get('text', '')}" for d in recent))
    parts.append(REMEMBERING)
    return "\n\n".join(parts)


def _short_date(stamp: str | None) -> str:
    moment = _parse(stamp)
    return f"{moment.day} {moment:%b}" if moment else "?"


# ---------- the room editor ----------

def for_editor() -> dict:
    data = _load()
    return {"notes": data["notes"], "diary": data["diary"], "last_talked": data.get("last_talked")}


def update_from_editor(sent: dict) -> dict:
    """Edits and deletions from the editor. Only entries that already exist are kept
    (nothing can be added there), with their text as edited."""
    data = _load()
    for key in ("notes", "diary"):
        texts = {e.get("id"): " ".join(str(e.get("text", "")).split())
                 for e in sent.get(key) or [] if isinstance(e, dict)}
        data[key] = [dict(e, text=texts[e.get("id")]) for e in data[key] if texts.get(e.get("id"))]
    _save(data)
    return for_editor()


# ---------- chat log ----------

_pruned_on = None


def log_turn(conversation: str | None, kind: str, heard: str, hidden: str, reply: str) -> None:
    """One turn. kind: "speech" (what speech-to-text heard from Espen), "unprompted"
    (something she was told to speak up about) or "diary" (the end-of-conversation
    request). hidden: what was put in front of it, which Espen didn't say."""
    global _pruned_on
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if _pruned_on != now().date():
            _pruned_on = now().date()
            _prune_chatlog()
        line = {"at": _stamp(), "conversation": conversation, "kind": kind,
                "heard": heard, "hidden": hidden, "reply": reply}
        with CHATLOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"🧠 couldn't write the chat log: {e!r}")


def chatlog() -> list[dict]:
    try:
        lines = CHATLOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    turns = []
    for line in lines:
        try:
            turns.append(json.loads(line))
        except ValueError:
            continue
    return turns


def _prune_chatlog() -> None:
    cutoff = now() - timedelta(days=CHATLOG_DAYS)
    turns = chatlog()
    kept = [t for t in turns if (moment := _parse(t.get("at"))) and moment >= cutoff]
    if len(kept) < len(turns):
        tmp = CHATLOG_FILE.with_suffix(".jsonl.part")
        tmp.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in kept), encoding="utf-8")
        tmp.replace(CHATLOG_FILE)
