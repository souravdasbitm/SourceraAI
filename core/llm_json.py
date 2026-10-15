"""Parse JSON returned by an LLM. Models often emit CSS such as \\s, which is not a JSON escape."""
import json

_VALID_ESCAPES = set('"\\/bfnrt')


def loads_llm_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_invalid_escapes(text))


def _repair_invalid_escapes(text: str) -> str:
    out = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if ch == '"' and not _escaped(text, i):
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if in_string and ch == "\\":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt in _VALID_ESCAPES:
                out.append(text[i : i + 2])
                i += 2
                continue
            if nxt == "u" and _is_hex4(text, i + 2):
                out.append(text[i : i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escaped(text: str, index: int) -> bool:
    slashes = 0
    j = index - 1
    while j >= 0 and text[j] == "\\":
        slashes += 1
        j -= 1
    return slashes % 2 == 1


def _is_hex4(text: str, index: int) -> bool:
    chunk = text[index : index + 4]
    return len(chunk) == 4 and all(c in "0123456789abcdefABCDEF" for c in chunk)
