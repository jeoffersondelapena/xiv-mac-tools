"""The watchers' notes to the player: one line per source in Overlay Doctor's attention file, which the plugin
reads at login and repeats in chat while a note stands. Wording: "<source>: <what happened> at <time>; <capture>"."""
import datetime, os

BASE = os.path.expanduser("~/Library/Application Support/XIV on Mac")
ATTENTION = os.path.join(BASE, "pluginConfigs", "OverlayDoctor", "attention.txt")


def attention_lines(existing, source, note):
    """Replace this source's line; drop it when note is None."""
    kept = [l for l in existing.splitlines() if l.strip() and not l.startswith(source + ":")]
    if note:
        kept.append(f"{source}: {note}")
    return "".join(l + "\n" for l in kept)


def set_attention(source, note, path=ATTENTION):
    """Leave a note Overlay Doctor shows the player; None clears this source's note. Never raises."""
    try:
        existing = open(path).read() if os.path.exists(path) else ""
        text = attention_lines(existing, source, note)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if text:
            with open(path, "w") as f:
                f.write(text)
        elif os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


def event_note(what, capture=None, when=None):
    """'<what> at HH:MM; capture <file>' — the shared shape for a watcher's event."""
    stamp = (when or datetime.datetime.now()).strftime("%H:%M")
    return f"{what} at {stamp}" + (f"; capture {os.path.basename(capture)}" if capture else "")
