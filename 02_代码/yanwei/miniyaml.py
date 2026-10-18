"""极简 YAML 子集解析器（零依赖）。

只支持本项目配置用到的语法，刻意不支持全量 YAML：
  - 注释(#)、空行
  - `key: value` 标量（int / float / bool / null / str，支持引号）
  - 按 2 空格缩进的嵌套字典
  - `- item` 列表，以及 `- key: value` 形式的列表字典
  - 行内列表 `[a, b, c]`

不支持：多行块标量、锚点、复杂键、流式映射。配置里不会出现。
"""

from __future__ import annotations


def _scalar(s: str):
    s = s.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith("[") and s.endswith("]"):
        body = s[1:-1].strip()
        if not body:
            return []
        return [_scalar(p) for p in body.split(",")]
    if s.startswith("{") and s.endswith("}"):
        body = s[1:-1].strip()
        if not body:
            return {}
        out = {}
        for part in body.split(","):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            out[k.strip()] = _scalar(v)
        return out
    return s


def loads(text: str):
    rows = []
    for raw in text.splitlines():
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        if not raw.strip():
            continue
        rows.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    if not rows:
        return {}

    pos = [0]

    def parse_block(base_indent):
        _, first = rows[pos[0]]
        is_list = first.startswith("- ")
        node = [] if is_list else {}
        while pos[0] < len(rows):
            indent, content = rows[pos[0]]
            if indent < base_indent:
                break
            if indent > base_indent:
                break
            if is_list:
                if not content.startswith("- "):
                    break
                item = content[2:].strip()
                pos[0] += 1
                if ":" in item:
                    k, _, v = item.partition(":")
                    d = {k.strip(): _scalar(v)}
                    while pos[0] < len(rows):
                        ind2, c2 = rows[pos[0]]
                        if ind2 > base_indent and ":" in c2:
                            k2, _, v2 = c2.partition(":")
                            d[k2.strip()] = _scalar(v2)
                            pos[0] += 1
                        else:
                            break
                    node.append(d)
                else:
                    node.append(_scalar(item))
            else:
                if content.startswith("- "):
                    break
                k, _, v = content.partition(":")
                k, v = k.strip(), v.strip()
                pos[0] += 1
                if v == "":
                    if pos[0] < len(rows) and rows[pos[0]][0] > base_indent:
                        node[k] = parse_block(rows[pos[0]][0])
                    else:
                        node[k] = {}
                else:
                    node[k] = _scalar(v)
        return node

    return parse_block(rows[0][0])


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())
