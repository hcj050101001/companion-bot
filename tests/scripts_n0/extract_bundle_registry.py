"""从本机 NapCat bundle 抽取动作注册表对象，生成 N0 快照夹具。

方法（N0 固化，见 docs/napcat-verification-n0.md）：定位 bundle 中的动作
注册表对象字面量 ``const re = { ... };``（NapCat 4.18.41；变量名可用
``--var`` 覆盖），大括号配对（跳过字符串与注释）截取对象文本，逐行解析
``Key: "action_name"`` 条目；对象内以 ``//`` 开头的条目行记为 commented，
其余记为 active；纯分节注释行（无 Key: "value" 形态）不计。

只读：不改动 bundle、不调用任何接口。NapCat 升级后重跑本脚本并与
tests/fixtures/napcat/bundle_registry_snapshot.json 对账，即 §7.3 第 1 级
复核。用法：

    python3 tests/scripts_n0/extract_bundle_registry.py \
        --bundle /path/to/napcat.mjs --out /tmp/snapshot.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

ENTRY_RE = re.compile(r'^([A-Za-z0-9_.]+)\s*:\s*["\']([^"\']+)["\']')


def find_object_span(text: str, var: str) -> tuple[int, int]:
    """定位 ``const <var> = {`` 并大括号配对到闭合 ``}``，返回字符区间。"""
    m = re.search(rf"const\s+{re.escape(var)}\s*=\s*\{{", text)
    if not m:
        raise SystemExit(f"registry object `const {var} = {{` not found")
    start = m.end() - 1  # 指向 '{'
    depth = 0
    i = start
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch in "\"'`":
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i
        i += 1
    raise SystemExit("unbalanced braces in registry object")


def parse_entries(span_text: str) -> tuple[list[dict], list[dict]]:
    active: list[dict] = []
    commented: list[dict] = []
    for line in span_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            m = ENTRY_RE.match(stripped[2:].strip())
            if m:
                commented.append({"key": m.group(1), "action": m.group(2)})
        else:
            m = ENTRY_RE.match(stripped)
            if m:
                active.append({"key": m.group(1), "action": m.group(2)})
    return active, commented


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--var", default="re")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    raw = open(args.bundle, "rb").read()
    text = raw.decode("utf-8")
    start, end = find_object_span(text, args.var)
    active, commented = parse_entries(text[start : end + 1])
    snapshot = {
        "bundle_md5": hashlib.md5(raw).hexdigest(),
        "bundle_size_bytes": len(raw),
        "registry_variable": args.var,
        "active": active,
        "commented": commented,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=1)
    print(f"active={len(active)} commented={len(commented)} md5={snapshot['bundle_md5']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
