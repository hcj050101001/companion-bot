"""从本机 NapCat bundle 抽取 N6 终审动作的 payload/return schema 字段级证据。

引擎与 N5 同一份（`tests/scripts_n5/extract_bundle_schemas.py`，逐字
复制），本副本只改抽取范围与出处标注：N6 终审范围为 L3 族 8 个 +
get_share_link + .get_word_slices + get_unidirectional_friend_list +
bot_exit / set_restart（ops_only 终审）+ get_group_todo（待验复核）。

方法（承 N0 `tests/scripts_n0/extract_bundle_registry.py` 口径）：

1. 动作名 -> 注册表键：解析 bundle 中的动作注册表对象 ``const re = {...}``
   （大括号配对截取、逐行解析，与 N0 快照同一方法），反查 snake 名到 Key。
2. 动作类定位：按 ``actionName = re.<Key>`` 定位类字段，反向找到所属
   ``class X extends Y { ... }`` 类块（大括号配对），在类块内抽
   ``payloadSchema`` / ``returnSchema`` / ``actionSummary`` /
   ``actionDescription`` / ``payloadExample`` / ``returnExample`` 六个字段。
   类自身缺 schema 时沿 ``extends`` 父类链继承（例：``ocr_image`` 的类
   ``Dbe extends IL``，schema 定义在父类 ``IL``），并记录继承来源。
3. schema 变量解析：schema 若为标识符变量，在类位置之前最近的一处
   ``<var> = p.<...>`` 定义（TypeBox 表达式）处取值，再递归解析该表达式：
   ``p.Object`` 逐字段、``p.Optional`` 标记可选、选项对象里的
   ``description`` / ``default`` 原样记录；``p.Array`` 记录元素类型，
   ``p.Union`` 记录成员类型，嵌套 ``p.Object`` 递归展开。
4. ``_handle`` 方法体只作证据摘录：记录其访问的参数字段名
   （``e.<field>`` 顺序去重）；``test_download_stream`` 另存方法体摘录
   与若干机械判定位（是否调用 ``this.core.apis``、是否使用 url 参数等），
   供定性引用，脚本不替人下结论之外的推断。
5. 消息段证据：在 bundle 中按段 schema 的 ``$id``
   （``OB11MessageFlashTransfer`` / ``OB11MessageOnlineFile``）与
   ``p.Literal("<type>")`` 定位段定义，解析其 ``data`` 对象字段；同时记录
   段类型枚举（``ze``）原文、raw->ob11 转换器产出点与 ob11->raw
   转换器实现摘录。任务口径中的 ``online_file`` 在 bundle 中不是段类型
   字面量（实际为 ``onlinefile``，无下划线），脚本如实记录该差异。

只读：不改动 bundle、不调用任何接口、不联网。输出 JSON 不含时间戳，
两次运行结果逐字节一致（确定性）。

用法：

    uv run python tests/scripts_n6/extract_bundle_schemas.py \
        --bundle ~/agent-workspace/projects/QQBot-deploy/napcat-v4/\
root/opt/QQ/resources/app/napcat/napcat.mjs \
        --out tests/fixtures/napcat/bundle_action_schemas_n6.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

#: N6 终审抽取范围（14 个）。
TARGET_ACTIONS: list[str] = [
    "get_cookies",
    "get_csrf_token",
    "get_credentials",
    "get_clientkey",
    "nc_get_rkey",
    "get_rkey",
    "get_rkey_server",
    "send_packet",
    "get_share_link",
    ".get_word_slices",
    "get_unidirectional_friend_list",
    "bot_exit",
    "set_restart",
    "get_group_todo",
]

_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]"}
_CLOSE_CHARS = set(_OPEN_TO_CLOSE.values())


# --------------------------------------------------------------------------
# 基础扫描器：字符串/注释/模板字符串感知的配平与切分。
# 模板字符串整体按字符串处理：其 ${...} 内的花括号本身成对，
# 整体忽略不影响外层配平（本 bundle 的 schema/示例/handler 区域成立）。
# --------------------------------------------------------------------------


def extract_balanced(text: str, start: int) -> int:
    """text[start] 为开括号，返回与之配平的闭括号下标（含）。"""
    assert text[start] in _OPEN_TO_CLOSE, text[start]
    stack = [_OPEN_TO_CLOSE[text[start]]]
    i = start + 1
    n = len(text)
    in_str: str | None = None
    in_line = False
    in_block = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block = True
                i += 1
            elif ch in "\"'`":
                in_str = ch
            elif ch in _OPEN_TO_CLOSE:
                stack.append(_OPEN_TO_CLOSE[ch])
            elif ch in _CLOSE_CHARS:
                if not stack or ch != stack[-1]:
                    raise ValueError(f"unbalanced at {i}: unexpected {ch!r}")
                stack.pop()
                if not stack:
                    return i
        i += 1
    raise ValueError("unbalanced: no closing bracket")


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """在括号深度 0 处按 sep 切分（字符串/注释感知）。"""
    parts: list[str] = []
    depth = 0
    in_str: str | None = None
    in_line = False
    in_block = False
    last = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block = True
                i += 1
            elif ch in "\"'`":
                in_str = ch
            elif ch in _OPEN_TO_CLOSE:
                depth += 1
            elif ch in _CLOSE_CHARS:
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append(text[last:i])
                last = i + 1
        i += 1
    parts.append(text[last:])
    return parts


def strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释（字符串内不处理），以空格替代保持可解析。"""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str: str | None = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        if ch in "\"'`":
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)


def capture_value(text: str, start: int, terminators: str) -> tuple[str, int]:
    """从 start 起捕获一个 JS 值表达式，直到深度 0 处遇到 terminators 之一。

    返回 (表达式文本, 终止符下标)。字符串/注释/括号配平感知。
    """
    depth = 0
    in_str: str | None = None
    in_line = False
    in_block = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block = True
                i += 1
            elif ch in "\"'`":
                in_str = ch
            elif ch in _OPEN_TO_CLOSE:
                depth += 1
            elif ch in _CLOSE_CHARS:
                depth -= 1
            elif depth == 0 and ch in terminators:
                return text[start:i], i
        i += 1
    return text[start:], n


def parse_js_string(token: str) -> str | None:
    """解析 JS 字符串字面量（'...' / \"...\" / `...` 无插值时），失败返回 None。"""
    token = token.strip()
    if len(token) < 2 or token[0] not in "\"'`":
        return None
    quote = token[0]
    out: list[str] = []
    i = 1
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 1 < len(token):
            esc = token[i + 1]
            simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                      "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"',
                      "`": "`", "/": "/", "$": "$"}
            if esc in simple:
                out.append(simple[esc])
                i += 2
                continue
            if esc == "u" and i + 5 < len(token) + 1 and i + 6 <= len(token):
                try:
                    out.append(chr(int(token[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if esc == "x" and i + 4 <= len(token):
                try:
                    out.append(chr(int(token[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.append(esc)
            i += 2
            continue
        if ch == quote:
            return "".join(out)
        out.append(ch)
        i += 1
    return None


def normalize_ws(text: str, limit: int | None = None) -> str:
    """把连续空白压成单空格（摘录用）；可截断。"""
    out = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(out) > limit:
        out = out[:limit] + "…"
    return out


# --------------------------------------------------------------------------
# 注册表（与 N0 同一方法）：const re = {...} -> action 名 -> Key
# --------------------------------------------------------------------------

_ENTRY_RE = re.compile(r'^([A-Za-z0-9_.]+)\s*:\s*["\']([^"\']+)["\']')


def parse_registry(text: str, var: str = "re") -> dict[str, str]:
    m = re.search(rf"const\s+{re.escape(var)}\s*=\s*\{{", text)
    if not m:
        raise SystemExit(f"registry object `const {var} = {{` not found")
    start = m.end() - 1
    end = extract_balanced(text, start)
    mapping: dict[str, str] = {}
    for line in text[start:end + 1].splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        mm = _ENTRY_RE.match(stripped)
        if mm:
            mapping[mm.group(2)] = mm.group(1)
    return mapping


def extract_version(text: str) -> str | None:
    m = re.search(r'typeof\s+\w+\s*<\s*"u"\s*&&\s*"(\d+\.\d+\.\d+)"', text)
    if m:
        return m.group(1)
    m = re.search(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"', text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# TypeBox 表达式解析
# --------------------------------------------------------------------------


def _parse_options(obj_text: str) -> tuple[str | None, bool, str | None]:
    """解析选项对象字面量，返回 (description, has_default, default_raw)。"""
    obj_text = obj_text.strip()
    if not obj_text.startswith("{"):
        return None, False, None
    end = extract_balanced(obj_text, 0)
    inner = obj_text[1:end]
    description: str | None = None
    has_default = False
    default_raw: str | None = None
    for part in split_top_level(inner):
        part = part.strip()
        if not part:
            continue
        mm = re.match(r"^([\w$]+)\s*:\s*(.*)$", part, re.S)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if key == "description":
            description = parse_js_string(val)
        elif key == "default":
            has_default = True
            default_raw = normalize_ws(val)
    return description, has_default, default_raw


def parse_object_fields(obj_text: str) -> list[dict]:
    """解析 p.Object 的第一个参数（对象字面量）为字段表。"""
    obj_text = strip_comments(obj_text).strip()
    if not obj_text.startswith("{"):
        return []
    end = extract_balanced(obj_text, 0)
    inner = obj_text[1:end]
    fields: list[dict] = []
    for part in split_top_level(inner):
        part = part.strip()
        if not part:
            continue
        mm = re.match(r"^([\w$]+|\"[^\"]+\"|'[^']+')\s*:\s*(.*)$", part, re.S)
        if not mm:
            fields.append({
                "name": None, "type": "Unparsed", "optional": False,
                "has_default": False, "default": None, "description": None,
                "raw": normalize_ws(part, 300),
            })
            continue
        raw_name = mm.group(1)
        name = parse_js_string(raw_name) if raw_name[0] in "\"'" else raw_name
        field = parse_typebox(mm.group(2))
        field_ordered = {"name": name}
        field_ordered.update(field)
        fields.append(field_ordered)
    return fields


def parse_typebox(expr: str) -> dict:
    """解析一个 TypeBox 表达式为字段/类型描述 dict（不含 name）。"""
    expr = strip_comments(expr).strip()
    base: dict = {
        "type": "Unknown",
        "optional": False,
        "has_default": False,
        "default": None,
        "description": None,
    }
    # 标识符引用（指向另一处定义的 schema 变量/共享对象）
    if re.fullmatch(r"[\w$]+", expr):
        base["type"] = "Ref"
        base["ref"] = expr
        return base
    m = re.match(r"^p\.(\w+)\s*\(", expr)
    if not m:
        base["raw"] = normalize_ws(expr, 300)
        return base
    kind = m.group(1)
    paren_start = expr.index("(", m.start())
    paren_end = extract_balanced(expr, paren_start)
    inner = expr[paren_start + 1:paren_end]
    args = [a.strip() for a in split_top_level(inner)] if inner.strip() else []

    if kind == "Optional":
        if not args:
            return base
        inner_field = parse_typebox(args[0])
        inner_field["optional"] = True
        if len(args) > 1 and args[1].startswith("{"):
            desc, has_def, def_raw = _parse_options(args[1])
            if desc is not None:
                inner_field["description"] = desc
            if has_def:
                inner_field["has_default"] = True
                inner_field["default"] = def_raw
        return inner_field

    if kind == "Object":
        fields = parse_object_fields(args[0]) if args else []
        desc, has_def, def_raw = None, False, None
        if len(args) > 1:
            desc, has_def, def_raw = _parse_options(args[1])
        base.update({"type": "Object", "description": desc, "fields": fields,
                     "has_default": has_def, "default": def_raw})
        return base

    if kind == "Array":
        item = parse_typebox(args[0]) if args else {"type": "Unknown"}
        desc, has_def, def_raw = None, False, None
        if len(args) > 1:
            desc, has_def, def_raw = _parse_options(args[1])
        base.update({"type": "Array", "description": desc,
                     "has_default": has_def, "default": def_raw,
                     "item_type": item.get("type")})
        if item.get("type") == "Object":
            base["item_fields"] = item.get("fields", [])
        elif item.get("type") == "Ref":
            base["item_ref"] = item.get("ref")
        return base

    if kind == "Union":
        members: list[dict] = []
        member_types: list[str] = []
        if args and args[0].startswith("["):
            arr_end = extract_balanced(args[0], 0)
            for part in split_top_level(args[0][1:arr_end]):
                part = part.strip()
                if not part:
                    continue
                member = parse_typebox(part)
                members.append(member)
                member_types.append(member.get("type"))
        desc, has_def, def_raw = None, False, None
        if len(args) > 1:
            desc, has_def, def_raw = _parse_options(args[1])
        base.update({"type": "Union", "description": desc,
                     "has_default": has_def, "default": def_raw,
                     "member_types": member_types, "members": members})
        return base

    if kind == "Literal":
        lit_raw = args[0] if args else ""
        lit_val = parse_js_string(lit_raw)
        desc = None
        if len(args) > 1:
            desc, _, _ = _parse_options(args[1])
        base.update({"type": "Literal", "description": desc,
                     "literal": lit_val if lit_val is not None else normalize_ws(lit_raw)})
        return base

    # 标量与特殊类型：String/Number/Boolean/Integer/Any/Void/Null/Unknown...
    desc = None
    has_def = False
    def_raw = None
    if args and args[0].startswith("{"):
        desc, has_def, def_raw = _parse_options(args[0])
    base.update({"type": kind, "description": desc,
                 "has_default": has_def, "default": def_raw})
    return base


# --------------------------------------------------------------------------
# 变量定义与类块解析
# --------------------------------------------------------------------------


def find_var_definition(text: str, var: str, before_pos: int) -> tuple[str, int] | None:
    """找 <var> = p.<...> 定义表达式；优先类位置之前最近的一处。

    返回 (表达式文本, 定义下标)；找不到返回 None。
    """
    pattern = re.compile(r"(?<![\w$])" + re.escape(var) + r"\s*=\s*(?=p\.)")
    candidates = [mm.start() for mm in pattern.finditer(text)]
    if not candidates:
        return None
    prior = [c for c in candidates if c < before_pos]
    chosen = prior[-1] if prior else candidates[0]
    eq = text.index("=", chosen)
    expr_start = eq + 1
    while expr_start < len(text) and text[expr_start] in " \t\r\n":
        expr_start += 1
    expr, _ = capture_value(text, expr_start, ",;")
    return expr.strip(), chosen


_CLASS_RE = re.compile(r"class\s+([\w$]+)(?:\s+extends\s+([\w$]+))?\s*\{")


def find_class_block(text: str, pos_in_block: int) -> dict | None:
    """找包含 pos_in_block 的类定义（取其之前最近的 class 头并校验配平范围）。"""
    last = None
    for mm in _CLASS_RE.finditer(text, 0, pos_in_block + 1):
        last = mm
    if last is None:
        return None
    brace = last.end() - 1
    try:
        end = extract_balanced(text, brace)
    except ValueError:
        return None
    if not (brace <= pos_in_block <= end):
        return None
    return {
        "name": last.group(1),
        "extends": last.group(2),
        "start": last.start(),
        "brace": brace,
        "end": end,
        "text": text[brace:end + 1],
    }


def find_class_by_name(text: str, name: str) -> dict | None:
    for mm in _CLASS_RE.finditer(text):
        if mm.group(1) == name:
            brace = mm.end() - 1
            try:
                end = extract_balanced(text, brace)
            except ValueError:
                return None
            return {
                "name": mm.group(1),
                "extends": mm.group(2),
                "start": mm.start(),
                "brace": brace,
                "end": end,
                "text": text[brace:end + 1],
            }
    return None


_CLASS_FIELDS = (
    "payloadSchema", "returnSchema", "actionSummary",
    "actionDescription", "payloadExample", "returnExample",
)


def extract_class_assignments(block_text: str) -> dict[str, str]:
    """在类块文本内抽各字段的赋值表达式原文（到深度 0 的 ';' 为止）。"""
    out: dict[str, str] = {}
    for field in _CLASS_FIELDS:
        mm = re.search(r"(?<![\w$])" + field + r"\s*=\s*", block_text)
        if mm:
            value, _ = capture_value(block_text, mm.end(), ";")
            value = value.strip()
            # 基类 ue 以 `payloadSchema = void 0` 等占位声明字段，不算赋值。
            if value in ("void 0", "undefined"):
                continue
            out[field] = value
    return out


def extract_method_body(block_text: str, method: str) -> tuple[str, str] | None:
    """抽方法体，返回 (第一形参名, 方法体文本)；找不到返回 None。"""
    mm = re.search(r"(?:async\s+)?" + re.escape(method) + r"\s*\(", block_text)
    if not mm:
        return None
    paren = block_text.index("(", mm.start())
    try:
        paren_end = extract_balanced(block_text, paren)
    except ValueError:
        return None
    params_text = block_text[paren + 1:paren_end]
    first_param = ""
    if params_text.strip():
        first_param = split_top_level(params_text)[0].strip()
        first_param = re.split(r"[=:\s]", first_param)[0]
    brace_search = block_text.find("{", paren_end)
    if brace_search == -1:
        return None
    try:
        body_end = extract_balanced(block_text, brace_search)
    except ValueError:
        return None
    return first_param, block_text[brace_search:body_end + 1]


def field_accesses(param: str, body: str) -> list[str]:
    if not param:
        return []
    seen: list[str] = []
    for mm in re.finditer(r"\b" + re.escape(param) + r"\.([\w$]+)", body):
        if mm.group(1) not in seen:
            seen.append(mm.group(1))
    return seen


# --------------------------------------------------------------------------
# schema 表达式 -> 结构化结果
# --------------------------------------------------------------------------


def resolve_schema(text: str, expr: str, class_pos: int,
                   notes: list[str]) -> dict:
    """把 payloadSchema/returnSchema 的赋值表达式解析为结构化 dict。"""
    expr = expr.strip()
    result: dict = {"source_expr": normalize_ws(expr, 200)}
    if re.fullmatch(r"[\w$]+", expr):
        result["schema_var"] = expr
        found_def = find_var_definition(text, expr, class_pos)
        if found_def is None:
            result["type"] = "Unresolved"
            notes.append(f"schema 变量 {expr} 未找到 p.* 定义")
            return result
        def_expr, def_pos = found_def
        result["schema_var_defined_at"] = def_pos
        parsed = parse_typebox(def_expr)
    else:
        result["schema_var"] = None
        parsed = parse_typebox(expr)
    result["type"] = parsed.get("type")
    result["description"] = parsed.get("description")
    if parsed.get("type") == "Object":
        result["fields"] = parsed.get("fields", [])
    elif parsed.get("type") == "Array":
        result["item_type"] = parsed.get("item_type")
        if "item_fields" in parsed:
            result["item_fields"] = parsed["item_fields"]
        if "item_ref" in parsed:
            result["item_ref"] = parsed["item_ref"]
    elif parsed.get("type") == "Union":
        result["member_types"] = parsed.get("member_types", [])
    elif parsed.get("type") == "Ref":
        result["ref"] = parsed.get("ref")
    return result


def summarize_example(raw: str | None) -> dict:
    """归纳 payloadExample/returnExample 赋值：字面量记原文与顶层键，引用记 ref。"""
    if raw is None:
        return {"present": False}
    raw = raw.strip()
    if raw == "null":
        return {"present": True, "kind": "null", "value": None}
    if raw.startswith("{") or raw.startswith("["):
        keys: list[str] = []
        if raw.startswith("{"):
            try:
                end = extract_balanced(raw, 0)
                for part in split_top_level(strip_comments(raw[1:end])):
                    part = part.strip()
                    if not part:
                        continue
                    mm = re.match(r"^([\w$]+|\"[^\"]+\"|'[^']+')\s*:", part)
                    if mm:
                        key = mm.group(1)
                        keys.append(parse_js_string(key) if key[0] in "\"'" else key)
            except ValueError:
                keys = []
        return {"present": True, "kind": "literal",
                "raw": normalize_ws(raw, 1000), "keys": keys}
    if re.fullmatch(r"[\w$]+(\.[\w$]+)+", raw):
        return {"present": True, "kind": "reference", "ref": raw}
    return {"present": True, "kind": "expression", "raw": normalize_ws(raw, 300)}


# --------------------------------------------------------------------------
# 单个动作抽取
# --------------------------------------------------------------------------


def extract_action(text: str, registry: dict[str, str], action: str) -> dict:
    entry: dict = {"action": action, "found": False}
    key = registry.get(action)
    entry["registry_key"] = key
    if key is None:
        entry["reason"] = "动作名不在 bundle 注册表 const re 中"
        return entry
    matches = list(re.finditer(
        r"actionName\s*=\s*re\." + re.escape(key) + r"\b", text))
    if not matches:
        entry["reason"] = f"注册表有键 {key} 但未找到 actionName = re.{key} 的动作类"
        return entry
    entry["action_name_occurrences"] = len(matches)
    match_pos = matches[0].start()
    block = find_class_block(text, match_pos)
    if block is None:
        entry["reason"] = "actionName 所在类块定位失败（class 头/配平异常）"
        return entry

    notes: list[str] = []
    assigns = extract_class_assignments(block["text"])
    inherited_from: list[str] = []
    handle_block_text = block["text"]
    handle_from: str | None = None
    # 只在 payload/return schema 缺失或本类无 _handle 时沿 extends 链上溯
    # （例：ocr_image 的类 Dbe extends IL，schema 与 _handle 都在 IL）。
    # 基类 ue 的字段是 void 0 占位，已在抽取时剔除，不会误记为继承来源。
    parent_name = block["extends"]
    depth = 0
    while parent_name and depth < 5 and (
            "payloadSchema" not in assigns or "returnSchema" not in assigns
            or handle_from is None and extract_method_body(
                handle_block_text, "_handle") is None):
        parent_block = find_class_by_name(text, parent_name)
        if parent_block is None:
            notes.append(f"父类 {parent_name} 未找到，继承解析中断")
            break
        parent_assigns = extract_class_assignments(parent_block["text"])
        contributed = False
        for f in _CLASS_FIELDS:
            if f not in assigns and f in parent_assigns:
                assigns[f] = parent_assigns[f]
                if f in ("payloadSchema", "returnSchema"):
                    contributed = True
        if extract_method_body(handle_block_text, "_handle") is None and \
                extract_method_body(parent_block["text"], "_handle"):
            handle_block_text = parent_block["text"]
            handle_from = parent_name
            contributed = True
        if contributed:
            inherited_from.append(parent_name)
        parent_name = parent_block["extends"]
        depth += 1
    if inherited_from:
        entry["inherited_from"] = inherited_from
        notes.append(
            "schema 或 _handle 继承自父类：" + " <- ".join(inherited_from))

    entry["found"] = True
    entry["class_name"] = block["name"]
    entry["class_extends"] = block["extends"]
    entry["class_offset"] = block["start"]
    if "actionSummary" in assigns:
        entry["action_summary"] = parse_js_string(assigns["actionSummary"])
    else:
        entry["action_summary"] = None
    if "actionDescription" in assigns:
        entry["action_description"] = parse_js_string(assigns["actionDescription"])
    else:
        entry["action_description"] = None

    if "payloadSchema" in assigns:
        payload = resolve_schema(text, assigns["payloadSchema"],
                                 block["start"], notes)
    else:
        payload = {"type": "Missing", "schema_var": None}
        notes.append("类及父类链均无 payloadSchema 赋值")
    entry["payload_schema"] = payload
    entry["payload_fields"] = payload.get("fields", []) \
        if payload.get("type") == "Object" else []

    if "returnSchema" in assigns:
        ret = resolve_schema(text, assigns["returnSchema"],
                             block["start"], notes)
    else:
        ret = {"type": "Missing", "schema_var": None}
        notes.append("类及父类链均无 returnSchema 赋值")
    entry["return_schema"] = ret
    entry["return_fields"] = ret.get("fields", []) \
        if ret.get("type") == "Object" else []
    entry["return_description"] = (
        ret.get("description")
        or (f"Array<{ret.get('item_type')}>" if ret.get("type") == "Array" else None))

    entry["payload_example"] = summarize_example(assigns.get("payloadExample"))
    entry["return_example"] = summarize_example(assigns.get("returnExample"))

    handle = extract_method_body(handle_block_text, "_handle")
    if handle:
        param, body = handle
        entry["handler_field_accesses"] = field_accesses(param, body)
        entry["handler_body"] = body
        if handle_from:
            entry["handler_inherited_from"] = handle_from
    else:
        entry["handler_field_accesses"] = []
        entry["handler_body"] = None
        notes.append("未找到 _handle 方法")
    check = extract_method_body(block["text"], "check")
    if check:
        entry["check_excerpt"] = normalize_ws(check[1], 500)

    # 示例与 schema 的机械对账：字面量示例出现 schema 外字段时记录。
    ex_keys = entry["payload_example"].get("keys") or []
    field_names = [f.get("name") for f in entry["payload_fields"]]
    extra = [k for k in ex_keys if field_names and k not in field_names]
    if extra:
        notes.append(f"payloadExample 含 payload schema 外字段：{extra}")
    missing_in_example = [n for n in field_names if ex_keys and n not in ex_keys]
    if missing_in_example and entry["payload_example"].get("kind") == "literal":
        notes.append(f"payloadExample 未覆盖字段：{missing_in_example}")
    entry["notes"] = notes
    return entry


# --------------------------------------------------------------------------
# 消息段证据
# --------------------------------------------------------------------------


def extract_segment(text: str, seg_type: str, schema_id: str) -> dict:
    result: dict = {"segment_type": seg_type, "schema_id": schema_id,
                    "found": False}
    m = re.search(r"p\.Literal\(\s*\"" + re.escape(seg_type) + r"\"", text)
    if not m:
        result["reason"] = f"未找到 p.Literal(\"{seg_type}\") 段定义"
        return result
    window = text[m.start():m.start() + 2500]
    idm = re.search(r"\$id:\s*\"([^\"]+)\"", window)
    result["schema_id_in_bundle"] = idm.group(1) if idm else None
    dm = re.search(r"data:\s*p\.Object\(", window)
    if not dm:
        result["reason"] = "段定义附近未找到 data: p.Object(...)"
        return result
    paren = window.index("(", dm.start())
    try:
        end = extract_balanced(window, paren)
    except ValueError:
        result["reason"] = "data 对象配平失败"
        return result
    obj_text = window[paren:end + 1]
    # 对 data 的 p.Object(...) 整体解析：
    call_start = window.index("p.Object", dm.start())
    call_paren = window.index("(", call_start)
    call_end = extract_balanced(window, call_paren)
    parsed = parse_typebox(window[call_start:call_end + 1])
    result["found"] = True
    result["data_fields"] = parsed.get("fields", []) \
        if parsed.get("type") == "Object" else []
    # obj_text 变量未用，保留配平成功这一事实
    _ = obj_text
    return result


def extract_segments(text: str) -> dict:
    segments: dict = {"note": (
        "段类型以 bundle 的 ze 枚举与段 schema $id 为准；任务口径的 "
        "online_file 在 bundle 中无此字面量段类型，实际段类型为 "
        "onlinefile（无下划线），online_file 仅出现在动作名/事件名中。")}
    # ze 枚举原文
    enum_hits = {}
    for name in ("flashtransfer", "onlinefile"):
        mm = re.search(r"t\." + name + r"\s*=\s*\"" + name + r"\"", text)
        enum_hits[name] = normalize_ws(text[mm.start():mm.end()]) if mm else None
    segments["ze_enum"] = enum_hits
    segments["online_file_literal_found"] = bool(
        re.search(r"p\.Literal\(\s*\"online_file\"", text)
        or re.search(r"t\.online_file\s*=", text))

    flash = extract_segment(text, "flashtransfer", "OB11MessageFlashTransfer")
    online = extract_segment(text, "onlinefile", "OB11MessageOnlineFile")
    segments["flashtransfer"] = flash
    segments["online_file"] = {
        "requested_name": "online_file",
        "found_as_literal_segment_type": segments["online_file_literal_found"],
        "actual_segment_type": "onlinefile",
        "evidence": online,
    }
    segments["onlinefile"] = online

    # raw -> ob11 产出点摘录
    prod: dict = {}
    mm = re.search(r"type:\s*ze\.flashtransfer", text)
    if mm:
        prod["flashtransfer_raw_to_ob11"] = normalize_ws(
            text[max(0, mm.start() - 220):mm.start() + 220], 500)
    mm = re.search(r"type:\s*ze\.onlinefile", text)
    if mm:
        prod["onlinefile_raw_to_ob11"] = normalize_ws(
            text[max(0, mm.start() - 220):mm.start() + 320], 600)
    segments["raw_to_ob11_converters"] = prod
    # ob11 -> raw 转换器摘录（两者均为空实现，上方有「不需要支持发送」注释）
    conv: dict = {}
    for name in ("onlinefile", "flashtransfer"):
        mm = re.search(r"\[ze\." + name + r"\]:\s*async\s*\(\)\s*=>\s*\{\s*\}", text)
        if mm:
            conv[name] = normalize_ws(
                text[max(0, mm.start() - 60):mm.end()], 300)
    segments["ob11_to_raw_converters"] = conv
    return segments


# --------------------------------------------------------------------------
# test_download_stream 定性证据
# --------------------------------------------------------------------------


def analyze_test_download_stream(entry: dict) -> dict:
    body = entry.get("handler_body") or ""
    facts = {
        "handler_field_accesses": entry.get("handler_field_accesses", []),
        "uses_url_param": bool(re.search(r"\b\w+\.url\b", body)),
        "calls_core_apis": "this.core.apis" in body,
        "calls_network_or_file_io": bool(re.search(
            r"fetch\(|axios|readFile|createReadStream|download", body, re.I)),
        "sends_synthetic_index_chunks": "Index->" in body,
        "stream_send_calls": body.count(".send("),
        "throws_test_error_literal": "This is a test error" in body,
        "loop_iterations_literal_10": bool(re.search(r"o\s*<\s*10", body)),
    }
    return {
        "action": "test_download_stream",
        "payload_fields": entry.get("payload_fields", []),
        "payload_example": entry.get("payload_example"),
        "handler_excerpt": normalize_ws(body, 1500) if body else None,
        "mechanical_facts": facts,
        "qualitative_conclusion": (
            "测试专用（合成流）：_handle 不调用 this.core.apis、不读 url 参数、"
            "不做任何网络/文件下载，只是循环 10 次每 100ms 经流通道发送 "
            "合成文本 `Index-> n`，且当入参 error 为真时抛字面量 "
            "`This is a test error`；payload schema 仅有一个可选字段 error，"
            "payloadExample 中的 url 字段不在 schema 内。定性依据均为上方 "
            "handler 摘录与机械判定位，不依赖动作名望文生义。"
            if (facts["sends_synthetic_index_chunks"]
                and not facts["calls_core_apis"]
                and not facts["uses_url_param"])
            else "机械判定位未全部命中，定性需人工复核 handler 摘录。"),
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--snapshot", default=None,
                    help="N0 快照 JSON 路径，给出时对账 bundle_md5")
    ap.add_argument("--actions", default=None,
                    help="逗号分隔动作名；缺省用内置 N6 终审范围")
    args = ap.parse_args()

    raw = Path(args.bundle).read_bytes()
    text = raw.decode("utf-8")
    md5 = hashlib.md5(raw).hexdigest()
    registry = parse_registry(text)
    actions = ([a.strip() for a in args.actions.split(",") if a.strip()]
               if args.actions else list(TARGET_ACTIONS))

    entries: dict[str, dict] = {}
    for action in actions:
        entries[action] = extract_action(text, registry, action)

    snapshot_md5 = None
    md5_matches = None
    if args.snapshot and Path(args.snapshot).exists():
        snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        snapshot_md5 = snap.get("source", {}).get("bundle_md5") \
            or snap.get("bundle_md5")
        md5_matches = (snapshot_md5 == md5)

    out: dict = {
        "source": {
            "napcat_version": extract_version(text),
            "bundle_file": "napcat.mjs",
            "bundle_md5": md5,
            "bundle_size_bytes": len(raw),
            "registry_variable": "re",
            "n0_snapshot_bundle_md5": snapshot_md5,
            "md5_matches_n0_snapshot": md5_matches,
            "method": ("tests/scripts_n6/extract_bundle_schemas.py（引擎同 N5）：注册表反查键名、"
                       "actionName 定位动作类、类内/父类链 schema 赋值、"
                       "TypeBox 表达式递归解析、_handle 字段访问摘录"),
            "note": ("字段级证据来自 bundle 内 TypeBox payloadSchema/returnSchema "
                     "定义原文；抽不到的动作 found=false 并注明原因，不补造。"),
        },
        "actions": {},
        "segment_evidence": extract_segments(text),
        "summary": {},
    }
    for action in actions:
        entry = dict(entries[action])
        entry.pop("handler_body", None)  # 方法体全文不进夹具，仅摘录另存
        out["actions"][action] = entry
    if "test_download_stream" in entries:
        out["test_download_stream_analysis"] = \
            analyze_test_download_stream(entries["test_download_stream"])
    found = sum(1 for a in actions if entries[a].get("found"))
    out["summary"] = {
        "requested": len(actions),
        "found": found,
        "not_found": [a for a in actions if not entries[a].get("found")],
        "with_payload_fields": sum(
            1 for a in actions if entries[a].get("payload_fields")),
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print(f"requested={len(actions)} found={found} md5={md5} "
          f"md5_matches_n0={md5_matches}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
