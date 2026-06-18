"""Common helpers."""

from datetime import datetime


def get_current_time():
    return datetime.now().strftime("%Y-%m-%d_%H:%M:%S")


# JS `event.key` -> Fara key name.
# JS already gives most names in the form Fara expects (Enter, Tab, ArrowUp...);
# only a few need fixing.
def normalize_key_name(js_key):
    if js_key == " ":
        return "Space"
    return js_key


def is_typeable_char(js_key):
    """A single printable character (not Enter/Tab/etc.)."""
    return len(js_key) == 1 and js_key.isprintable() and js_key != " " or js_key == " "


def is_typeable_for_buffer(js_key):
    """Chars that go into the type-buffer (visible chars including space)."""
    return len(js_key) == 1 and (js_key.isprintable() or js_key == " ")
