"""Strictly translate a complete DSML call envelope into literal Python calls."""
import ast
import json
import keyword
import math
import re
import xml.etree.ElementTree as ET


DSML_TAG_RE = re.compile(r"<\s*/?\s*(?:[|｜]\s*)+DSML\s*(?:[|｜]\s*)+(?:calls|invoke|parameter)\b", re.I)


def contains_dsml_action(text):
    return isinstance(text, str) and bool(DSML_TAG_RE.search(text))


def _literal_value(value):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError, RecursionError) as exc:
            raise ValueError("DSML argument must be a literal") from exc
    def valid(item, depth=0):
        if depth > 30:
            return False
        if type(item) is float:
            return math.isfinite(item)
        if item is None or type(item) in (str, bool, int):
            return True
        if isinstance(item, list):
            return all(valid(x, depth + 1) for x in item)
        if isinstance(item, dict):
            return all(isinstance(k, str) and valid(v, depth + 1) for k, v in item.items())
        return False
    if not valid(parsed):
        raise ValueError("Unsupported DSML literal type")
    return parsed


def parse_dsml_calls(text):
    if not contains_dsml_action(text):
        return None
    if len(text) > 100_000 or "<!" in text:
        raise ValueError("Invalid DSML envelope")
    normalized = re.sub(r"<\s*(/?)\s*(?:[|｜]\s*)+DSML\s*(?:[|｜]\s*)+", r"<\1", text).strip()
    try:
        root = ET.fromstring(normalized)
    except ET.ParseError as exc:
        raise ValueError("Incomplete or malformed DSML calls") from exc
    if root.tag != "calls" or root.attrib or (root.text or "").strip() or not 1 <= len(root) <= 8:
        raise ValueError("Expected a complete DSML calls envelope")
    lines = []
    for index, call in enumerate(root):
        name = call.get("name", "")
        if (call.tag != "invoke" or set(call.attrib) != {"name"}
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)
                or keyword.iskeyword(name)
                or (call.text or "").strip() or (call.tail or "").strip() or len(call) > 32):
            raise ValueError("Invalid DSML invocation")
        arguments = {}
        for param in call:
            key = param.get("name", "")
            string = param.get("string", "true")
            if (param.tag != "parameter" or len(param) or (param.tail or "").strip()
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
                    or keyword.iskeyword(key)
                    or key in arguments or string not in ("true", "false")
                    or set(param.attrib) - {"name", "string"}):
                raise ValueError("Invalid DSML parameter")
            value = param.text or ""
            arguments[key] = value if string == "true" else _literal_value(value.strip())
        # repr keeps text inert; the normal agent executor still checks tool availability.
        arguments_code = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        lines.append(f"dsml_result_{index} = {name}({arguments_code})")
        lines.append(f"print(dsml_result_{index})")
    return "\n".join(lines)
