"""The second persona's body: what she looks like, what she's wearing, her room.

The avatar page shows the second persona as a 3D character in a room. This
module makes the model treat that character as itself rather than as a puppet
it controls:

- avatar_instructions() writes the part of her instructions that describes her
  body (the editor's "how she looks" and "her room" text, plus what she is
  wearing and which colours she has right now) and the moods/gestures she can
  show with [tags].
- change_outfit / change_colour are function tools she can use to change her
  own clothes and colours; they edit the saved layout, so the page follows.

Everything is read from the same layout.json the room editor saves.
"""

from __future__ import annotations

import json
import logging
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.avatar import HUB, MEDIA_DIRS, UPLOAD_DIR, avatar_actions, load_layout, save_layout
from app.persona import CONFIG as PERSONA

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

GARMENT = re.compile(r"^Outfit_([A-Za-z]+?)\d*$")
VARIANT = re.compile(r"^(.+?)__(.+?)__(\d+)-(.+)_([0-9a-f]{6})\.(png|jpe?g)$", re.I)
# Clothes she can take off and put back on herself; the rest of the outfit stays on.
CHANGEABLE = {"jacket": "Jacket", "tie": "Tie", "socks": "Sock", "sneakers": "Sneaker", "shirt": "Shirt", "skirt": "Skirt", "panties": "UnderwearBottom", "top": "UnderwearTop"}
GARMENT_WORDS = {"Jacket": "jacket", "Shirt": "shirt", "Skirt": "skirt", "Sneaker": "sneakers", "Sock": "socks",
                 "Tie": "tie", "UnderwearBottom": "stripes", "UnderwearTop": "tankie"}
# Colour variant textures she can switch, by what they colour.
COLOUR_SLOTS = {"hair": "Hair_BaseColor", "eyes": "Body_BaseColor", "jacket": "Outfit2_BaseColor"}
# Rough colour names for the variants' swatch colours.
NAMED_COLOURS = {
    "white": "f2f2f2", "silver": "c6c6c6", "grey": "8a8a8a", "charcoal": "464646", "black": "1e1e1e",
    "cream": "e6d3b8", "beige": "cdb59a", "light brown": "b19680", "brown": "7b4a2e", "dark brown": "4f2e26",
    "red": "b03030", "burgundy": "8f403e", "pink": "e0a0a8", "dusty pink": "bb8993", "rose": "a15d63",
    "orange": "e08040", "amber": "a17847", "gold": "d4a74a", "yellow": "e8d44d", "olive": "93977c",
    "sage green": "9aa873", "pale green": "b9c78c", "green": "4f9a4f", "teal": "3f9a9a", "light blue": "95bad2", "steel blue": "8188a3",
    "blue": "4060b0", "navy": "2a3560", "lavender": "9c92d9", "mauve": "a27091", "purple": "7a4fb0",
}


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)


def colour_name(hex_: str) -> str:
    r, g, b = _rgb(hex_)
    return min(NAMED_COLOURS, key=lambda n: sum((x - y) ** 2 for x, y in zip((r, g, b), _rgb(NAMED_COLOURS[n]))))


def second_model() -> Path | None:
    """The second persona's model file: the one set in the options, else the .vrm whose name contains her name."""
    if PERSONA.second_model_file:
        for directory in MEDIA_DIRS:
            if (directory / PERSONA.second_model_file).is_file():
                return directory / PERSONA.second_model_file
    key = PERSONA.second_name.lower()
    for directory in MEDIA_DIRS:
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() == ".vrm" and key in path.name.lower():
                    return path
    return None


_garment_cache: dict[tuple[str, float], list[str]] = {}


def model_garments(path: Path) -> list[str]:
    """Garment names in a VRM (nodes named Outfit_<Name>), read from its JSON chunk only."""
    key = (str(path), path.stat().st_mtime)
    if key not in _garment_cache:
        with path.open("rb") as f:
            f.read(12)
            length, _ = struct.unpack("<I4s", f.read(8))
            gltf = json.loads(f.read(length))
        names = []
        for node in gltf.get("nodes", []):
            m = GARMENT.match(node.get("name", ""))
            if m and m.group(1) not in names:
                names.append(m.group(1))
        _garment_cache[key] = names
    return _garment_cache[key]


def colour_variants(model: Path) -> dict[str, list[dict]]:
    """Colour options per part ("hair", ...) from the uploaded variant textures."""
    base = model.stem
    files = {p.name for d in MEDIA_DIRS if d.is_dir() for p in d.iterdir()}
    parts: dict[str, list[dict]] = {}
    for part, slot in COLOUR_SLOTS.items():
        options = []
        for name in sorted(files):
            m = VARIANT.match(name)
            if m and m.group(1) == base and m.group(2) == slot:
                original = m.group(4).lower() == "original"
                options.append({"file": None if original else name, "label": m.group(4),
                                "colour": colour_name(m.group(5)), "original": original, "order": int(m.group(3))})
        if len(options) > 1:
            parts[part] = sorted(options, key=lambda o: o["order"])
    return parts


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _option_words(options: list[dict]) -> str:
    return ", ".join(("original " if o["original"] else "") + o["colour"] for o in options)


def describe_state(layout: dict, model: Path) -> str:
    """What she is wearing and which colours she has right now, as a sentence or two."""
    worn = layout.get("wardrobe", {}).get(model.name, {})
    garments = [g for g in model_garments(model) if g in GARMENT_WORDS]
    on = [GARMENT_WORDS[g] for g in garments if worn.get(g) is not False]
    off = [GARMENT_WORDS[g] for g in garments if worn.get(g) is False]
    lines = []
    if on:
        lines.append("You're wearing your " + _and(on) + ".")
    if off:
        lines.append("You've taken off your " + _and(off) + ".")
    chosen = layout.get("appearance", {}).get(model.name, {})
    for part, options in colour_variants(model).items():
        current = next((o for o in options if o["file"] and o["file"] == chosen.get(COLOUR_SLOTS[part])), None)
        if current:
            original = next((o for o in options if o["original"]), None)
            normally = f" (normally {original['colour']})" if original else ""
            lines.append(f"Your {part} is {current['colour']} right now{normally}.")
    return " ".join(lines)


# Clothes and colours she can also change with tags, which works where she has
# no tools (when a Home Assistant agent answers for her): [take off jacket],
# [put on jacket], [hair pink], [jacket burgundy], [eyes amber].
TAG_CLOTHES = ("jacket", "tie", "socks", "sneakers", "panties", "top", "shirt", "skirt")
OUTFIT_TAG = re.compile(r"^(take off|remove|put on|wear)\s+(?:your\s+|my\s+)?(.+)$")


def body_tag_action(value: str) -> Callable[[], Awaitable[str]] | None:
    """If a [tag] changes her outfit or a colour, the change to make; None for moods and gestures."""
    m = OUTFIT_TAG.match(value)
    if m and m.group(2) in TAG_CLOTHES:
        item, wear = m.group(2), m.group(1) in ("put on", "wear")
        return lambda: change_outfit(item, wear)
    words = value.split(None, 1)
    if len(words) == 2 and words[0] in COLOUR_SLOTS:
        part, colour = words
        return lambda: change_colour(part, colour)
    return None


def avatar_instructions(for_agent: bool = False) -> str:
    """The part of the second persona's instructions about her body, room, moods and gestures.

    for_agent: written for a Home Assistant agent, which has none of the add-on's
    tools, so outfit and colour changes are offered as tags instead.
    """
    layout = load_layout()
    character = layout.get("character") or {}
    model = second_model()
    # The page only ever uses the first entry with a given tag, so list only that one.
    actions, seen = [], set()
    for action in avatar_actions():
        key = " ".join(str(action.get("tag", "")).lower().replace("_", " ").replace("-", " ").split())
        if key not in seen:
            seen.add(key)
            actions.append(action)
    animations = {p.stem for d in MEDIA_DIRS if d.is_dir() for p in d.iterdir()
                  if p.suffix.lower() in (".fbx", ".vrma")}
    moods = [a for a in actions if a.get("kind") == "mood" and a.get("tag")]
    gestures = [a for a in actions if a.get("kind") == "gesture" and a.get("tag") and a.get("animation") in animations]

    parts = ["\n\n# Your body\n"
             "You are not just a voice: you have a body, and you live in your own little room, shown on a screen "
             "in the user's home. That body is you. When someone asks how you look, about your clothes, your hair "
             "or your room, they mean this body and this room, so answer as yourself from what's written here. "
             "Never call yourself an avatar, a model or a character, and never talk about animations or tags."]
    if character.get("appearance"):
        parts.append("\n\nHow you look: " + character["appearance"].strip())
    if model:
        state = describe_state(layout, model)
        if state:
            parts.append("\n\nRight now: " + state)
        colours = colour_variants(model)
        tag_clothes = [w for w in TAG_CLOTHES if CHANGEABLE[w] in model_garments(model)]
        clothes = [w for w, g in CHANGEABLE.items() if g in model_garments(model)]
        if for_agent and (tag_clothes or colours):
            offers = ([f"[take off {tag_clothes[0]}] and [put on {tag_clothes[0]}]"
                       + (f" (also your {_and(tag_clothes[1:])})" if len(tag_clothes) > 1 else "")] if tag_clothes else [])
            offers += [f"[{part} <colour>] ({_option_words(options)})" for part, options in colours.items()]
            parts.append("\n\nYou can change some of your clothes and colours yourself, when you're asked to or when "
                         "you feel like it, with a tag where you do it: " + "; ".join(offers)
                         + ". Use \"original\" for your usual colour. The rest of your outfit stays as it is.")
        elif clothes or colours:
            parts.append("\n\nYou can change these yourself with change_outfit and change_colour, when you're "
                         "asked to or when you feel like it: "
                         + "; ".join(([f"take off or put on your {_and(clothes)}"] if clothes else [])
                                     + [f"your {part} ({_option_words(options)})" for part, options in colours.items()])
                         + ". The rest of your outfit stays as it is.")
    if character.get("room"):
        parts.append("\n\nYour room: " + character["room"].strip())

    line = lambda a: f"[{a['tag']}] {a.get('note', '')}".rstrip()
    if moods or gestures:
        parts.append("\n\n# Your face and movements\n"
                     "You move your face and body with tags in square brackets inside your replies. They are never "
                     "spoken, and you only use the ones listed here.")
    if moods:
        parts.append("\n\nMoods (your facial expression; it stays until you change it). Start every reply with "
                     "one, and put a new one mid-reply when your mood shifts:\n" + "\n".join(map(line, moods)))
    if gestures:
        parts.append("\n\nGestures (a short movement, done once where you put the tag). Use one only when it "
                     "really fits, at most one or two per reply:\n" + "\n".join(map(line, gestures)))
    return "".join(parts)


# ---------- tools ----------
def avatar_tool_definitions() -> list[dict[str, Any]]:
    """change_outfit / change_colour for whatever this model and its uploaded variants allow."""
    model = second_model()
    if not model:
        return []
    tools = []
    clothes = [w for w, g in CHANGEABLE.items() if g in model_garments(model)]
    if clothes:
        tools.append({
            "type": "function",
            "name": "change_outfit",
            "description": "Take off or put on one of your own clothes (your body on the screen changes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "enum": clothes},
                    "wear": {"type": "boolean", "description": "true to put it on, false to take it off"},
                },
                "required": ["item", "wear"],
            },
        })
    colours = colour_variants(model)
    if colours:
        tools.append({
            "type": "function",
            "name": "change_colour",
            "description": "Change the colour of your own hair, eyes or jacket (your body on the screen changes). "
                           "Options: " + "; ".join(f"{part}: {_option_words(o)}" for part, o in colours.items()),
            "parameters": {
                "type": "object",
                "properties": {
                    "part": {"type": "string", "enum": list(colours)},
                    "colour": {"type": "string", "description": "one of the options, or 'original'"},
                },
                "required": ["part", "colour"],
            },
        })
    return tools


async def _apply(layout: dict, what: str) -> str:
    save_layout(layout)
    HUB.send({"type": "layout"})
    if HUB.on_layout_saved:
        await HUB.on_layout_saved()
    logger.info(f"🧍 {what}")
    return what


async def change_outfit(item: str, wear: bool) -> str:
    model = second_model()
    garment = CHANGEABLE.get(str(item).lower().strip())
    if not model or garment not in model_garments(model):
        return f"You can only change your {', '.join(CHANGEABLE)}."
    layout = load_layout()
    worn = layout.setdefault("wardrobe", {}).setdefault(model.name, {})
    if wear:
        worn.pop(garment, None)
    else:
        worn[garment] = False
    return await _apply(layout, f"You {'put on' if wear else 'took off'} your {item}.")


async def change_colour(part: str, colour: str) -> str:
    model = second_model()
    part = str(part).lower().strip()
    options = colour_variants(model).get(part, []) if model else []
    if not options:
        return "That part can't change colour."
    wanted = str(colour).lower().strip()
    pick = None
    if wanted in ("original", "normal", "default", "usual"):
        pick = next((o for o in options if o["original"]), None)
    pick = pick or next((o for o in options if wanted in (o["colour"], o["label"].lower())), None)
    pick = pick or next((o for o in options if wanted in o["colour"] or o["colour"] in wanted), None)
    if not pick and wanted in NAMED_COLOURS:
        pick = min(options, key=lambda o: sum((x - y) ** 2 for x, y in
                                              zip(_rgb(NAMED_COLOURS[o["colour"]]), _rgb(NAMED_COLOURS[wanted]))))
    if not pick:
        return f"No {colour} for your {part}. Options: {_option_words(options)}."
    layout = load_layout()
    chosen = layout.setdefault("appearance", {}).setdefault(model.name, {})
    if pick["original"]:
        chosen.pop(COLOUR_SLOTS[part], None)
    else:
        chosen[COLOUR_SLOTS[part]] = pick["file"]
    name = ("back to your original " + pick["colour"]) if pick["original"] else pick["colour"]
    return await _apply(layout, f"Your {part} is now {name}.")


def create_avatar_tool_handlers() -> dict[str, Callable[["FunctionCallParams"], Awaitable[None]]]:
    async def outfit(params: "FunctionCallParams") -> None:
        args = params.arguments or {}
        await params.result_callback(await change_outfit(args.get("item", ""), bool(args.get("wear", True))))

    async def colour(params: "FunctionCallParams") -> None:
        args = params.arguments or {}
        await params.result_callback(await change_colour(args.get("part", ""), args.get("colour", "")))

    return {"change_outfit": outfit, "change_colour": colour}
