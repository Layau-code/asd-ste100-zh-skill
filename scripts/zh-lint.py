#!/usr/bin/env python3
"""Review candidates in Chinese technical prose; do not infer author intent.

Usage:
    zh-lint.py [--mode strict|clear] [--json] [--baseline N] [FILE ...]
    zh-lint.py --disable rule1,rule2 FILE
    zh-lint.py --selftest

Read UTF-8 files or standard input. Exit 1 only if hard findings exceed the
baseline. Advisory findings are review prompts, not proof of a writing error.
The linter deliberately never flags modality words such as 可能 or 通常 alone.
"""

import argparse
import json
import re
import sys
from pathlib import Path


HAN = re.compile(r"[\u3400-\u9fff]")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"`[^`]*`")
LIST_START = re.compile(r"^(?P<indent> {0,3})(?P<marker>[-*+]|[0-9]+[.)])(?P<gap> +)(?P<body>.*)$")
LIST_END = re.compile(r"(?:和|与|及|或|以及|并且|或者)\s*$")
TABLE_SEPARATOR = re.compile(r":?-{3,}:?")
SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]?")

# Only the first two rules have unambiguous syntax that merits a hard finding.
# Everything else needs a human or an agent to review context and meaning.
RULES = (
    ("and-or", "hard",
     re.compile(r"(?:和|及|与)\s*[/／]\s*或|或\s*[/／]\s*(?:和|及|与)"),
     "明确是否表示至少一项成立，且允许两项同时成立。"),
    ("vague-deadline", "advisory",
     re.compile(r"尽快|及时|尽早|适时"),
     "若期限影响执行，请让作者给出可核对的时间；不要编造期限。"),
    ("vague-criterion", "advisory",
     re.compile(r"适当|合理|相关|必要时|视情况|稳定可靠"),
     "检查是否需要说明判断标准、对象或触发条件。"),
    ("referent", "advisory",
     re.compile(r"其(?!他|中|余)|前者|后者|上述|该项|这些|分别"),
     "检查这个指代是否只有一个可能的对象。"),
    ("negation-scope", "advisory",
     re.compile(r"(?:不|未|无|禁止|不得)[^。！？；;，,\n]{0,18}(?:和|与|及|或)"),
     "检查否定词作用于各并列项，还是作用于整体。"),
    ("condition-stack", "advisory",
     re.compile(r"(?:如果|若|当|除非)[^。！？；;\n]{0,45}(?:并且|或者|否则|除非|如果|若)"),
     "检查每个条件对应的动作和例外是否明确。"),
    ("nominalization", "advisory",
     re.compile(r"(?:进行|开展|实施)[^，。；;\n]{0,10}(?:检查|分析|处理|验证|评估|审核|操作)"),
     "检查能否直接使用动作动词；保留正式名称和必要限定。"),
    ("marketing-claim", "advisory",
     re.compile(r"高效|无缝|强大|业界领先|极致|一流|全方位"),
     "若这是技术说明，请删除空泛评价或给出可核对依据。"),
    ("soft-jargon", "advisory",
     re.compile(r"赋能|打通|拉通|沉淀|闭环|抓手"),
     "检查是否需要写出实际动作和对象。"),
    ("passive-actor", "advisory",
     re.compile(r"被(?:删除|修改|移除|替换|拒绝|覆盖|发送|更新|处理|清空|撤销)"),
     "检查执行者是否对读者重要；若原文没有执行者，不要猜测。"),
    ("sequence-in-sentence", "advisory",
     re.compile(r"先[^。！？\n]{0,45}然后[^。！？\n]{0,45}(?:最后|再)"),
     "检查是否应改成编号步骤，同时保留原有顺序。"),
)

SYNONYM_GROUPS = (
    ("检查", "核查", "校验", "验证"),
    ("删除", "移除", "清除"),
    ("启动", "开启", "开始"),
    ("停止", "终止", "结束"),
)
OBJECT_END = r"(?=是否|后|时|并|且|再|然后|，|,|。|；|;|！|!|？|\?|\s|$)"


def make_finding(path, line, column, rule, severity, matched, message):
    return {"path": path, "line": line, "column": column,
            "rule": rule, "severity": severity, "matched": matched,
            "message": message}


def table_row(line):
    """Return (cell, original zero-based column) pairs for a simple Markdown row."""
    left = len(line) - len(line.lstrip())
    value = line.strip()
    if "|" not in value:
        return None
    if value.startswith("|"):
        value = value[1:]
        left += 1
    if value.endswith("|"):
        value = value[:-1]
    pieces = re.split(r"(?<!\\)\|", value)
    if len(pieces) < 2:
        return None
    result = []
    column = left
    for piece in pieces:
        pad = len(piece) - len(piece.lstrip())
        result.append((piece.strip(), column + pad))
        column += len(piece) + 1
    return result


def table_cells(lines):
    """Map table rows to cells; do not merge columns into one sentence."""
    result = {}
    index = 1
    while index < len(lines):
        header = table_row(lines[index - 1])
        separator = table_row(lines[index])
        if not (header and separator and len(header) == len(separator)
                and all(TABLE_SEPARATOR.fullmatch(cell) for cell, _ in separator)):
            index += 1
            continue
        result[index - 1] = header
        result[index] = []
        index += 1
        while index < len(lines):
            row = table_row(lines[index])
            if not row or len(row) != len(separator):
                break
            result[index] = row
            index += 1
    return result


def masked_inline(text):
    return INLINE_CODE.sub(lambda match: " " * len(match.group()), text)


def fence_transition(line, current):
    """Return (new opening marker, is delimiter); mismatched fences stay code."""
    match = FENCE.match(line)
    if not match:
        return current, False
    marker = match.group(1)
    if current is None:
        return marker, True
    if marker[0] == current[0] and len(marker) >= len(current):
        return None, True
    return current, False


def dangling_list_findings(lines, path):
    """Flag a list item whose last meaningful line ends in 和/或/etc."""
    findings = []
    in_fence = None
    index = 0
    while index < len(lines):
        line = lines[index]
        in_fence, delimiter = fence_transition(line, in_fence)
        if delimiter:
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        start = LIST_START.match(line)
        if not start:
            index += 1
            continue
        content_indent = len(start.group("indent")) + len(start.group("marker")) + len(start.group("gap"))
        parts = [(index, start.group("body"), start.start("body"))]
        next_index = index + 1
        while next_index < len(lines):
            candidate = lines[next_index]
            if LIST_START.match(candidate):
                break
            if FENCE.match(candidate):
                break
            if candidate.strip() and len(candidate) - len(candidate.lstrip(" ")) < content_indent:
                break
            parts.append((next_index, candidate, 0))
            next_index += 1
        meaningful = [(n, masked_inline(body).rstrip(), col)
                      for n, body, col in parts if masked_inline(body).strip()]
        if meaningful:
            end_index, body, col = meaningful[-1]
            match = LIST_END.search(body)
            if match:
                findings.append(make_finding(
                    path, end_index + 1, col + match.start() + 1,
                    "dangling-conjunction", "hard", match.group(),
                    "列表项以连接词结尾；补全该项或与下一项合并。"))
        index = next_index
    return findings


def lint(content, path="<stdin>", mode="strict"):
    lines = content.splitlines()
    cells = table_cells(lines)
    results = []
    seen_actions = {}
    in_fence = None
    han_count = 0
    for line_number, raw_line in enumerate(lines, 1):
        in_fence, delimiter = fence_transition(raw_line, in_fence)
        if delimiter:
            continue
        if in_fence or raw_line.lstrip().startswith("#"):
            continue
        segments = cells.get(line_number - 1, [(raw_line, 0)])
        for raw_segment, source_column in segments:
            segment = masked_inline(raw_segment)
            han_count += len(HAN.findall(segment))
            for rule, severity, pattern, message in RULES:
                for match in pattern.finditer(segment):
                    results.append(make_finding(
                        path, line_number, source_column + match.start() + 1,
                        rule, severity, match.group(), message))
            if mode == "strict":
                for match in re.finditer(r"[；;]", segment):
                    results.append(make_finding(
                        path, line_number, source_column + match.start() + 1,
                        "semicolon", "advisory", match.group(),
                        "检查分号是否掩盖了独立步骤或条件。"))
            limit = 90 if mode == "strict" else 120
            for sentence in SENTENCE.finditer(segment):
                count = len(HAN.findall(sentence.group()))
                if count > limit:
                    results.append(make_finding(
                        path, line_number, source_column + sentence.start() + 1,
                        "long-sentence", "advisory", f"{count} 个汉字",
                        "检查本句是否包含多个动作、条件或话题；字数不是合规阈值。"))
            # Compare only verbs applied to the same explicit object. Similar
            # verbs can still mean different things, so this stays advisory.
            for group_index, group in enumerate(SYNONYM_GROUPS):
                verbs = "|".join(re.escape(word) for word in group)
                pattern = re.compile(rf"(?P<verb>{verbs})(?P<object>[\u3400-\u9fff]{{2,12}}?){OBJECT_END}")
                for match in pattern.finditer(segment):
                    verb, obj = match.group("verb"), match.group("object")
                    key = (group_index, obj)
                    current = (verb, line_number, source_column + match.start("verb") + 1)
                    if key not in seen_actions:
                        seen_actions[key] = current
                    elif seen_actions[key][0] != verb:
                        first = seen_actions[key][0]
                        results.append(make_finding(
                            path, line_number, current[2], "synonym-rotation",
                            "advisory", match.group(),
                            f"“{verb}”与前文“{first}”作用于同一对象“{obj}”；确认是否为同一动作。"))
                        seen_actions[key] = current
    results.extend(dangling_list_findings(lines, path))
    results.sort(key=lambda item: (item["line"], item["column"], item["rule"]))
    return results, han_count


def selftest():
    findings, _ = lint("可能失败。通常可以重试。")
    assert findings == [], findings
    findings, _ = lint("请求可能大概会失败。")
    assert findings == [], findings  # uncertainty is content, not a lint target
    findings, _ = lint("```text\nA 和/或 B；尽快。\n```\n`和/或` 保留。")
    assert findings == [], findings
    findings, _ = lint("```text\n~~~\nA 和/或 B。\n```")
    assert findings == [], findings  # a tilde fence does not close backticks
    findings, _ = lint("A 和/或 B。\n- 检查配置和\n- 通知用户。")
    assert {item["rule"] for item in findings} >= {"and-or", "dangling-conjunction"}
    findings, _ = lint("检查配置文件。\n核查配置文件。")
    assert any(item["rule"] == "synonym-rotation" for item in findings), findings
    findings, _ = lint("检查配置文件。\n核查日志内容。")
    assert not any(item["rule"] == "synonym-rotation" for item in findings)
    findings, _ = lint("不支持管理员和访客修改。如果配置错误或者缓存失效，停止任务。")
    assert {item["rule"] for item in findings} >= {
        "negation-scope", "condition-stack"}, findings
    findings, _ = lint("| 项目 | 说明 |\n| --- | --- |\n| A | 尽快处理 |")
    assert any(item["rule"] == "vague-deadline" and item["line"] == 3
               for item in findings), findings
    findings, _ = lint("- 检查配置和\n  缓存。")
    assert not any(item["rule"] == "dangling-conjunction" for item in findings)
    findings, _ = lint("文件被删除。系统进行检查；结果高效且稳定可靠。")
    assert {item["rule"] for item in findings} >= {
        "passive-actor", "nominalization", "semicolon", "marketing-claim",
        "vague-criterion"}, findings
    findings, _ = lint("检查配置文件。\n核查配置文件。", mode="clear")
    assert any(item["rule"] == "synonym-rotation" for item in findings)
    findings, _ = lint("检查配置文件。")
    assert not any(item["rule"] == "synonym-rotation" for item in findings)
    findings, _ = lint("| 第一列 | 第二列 |\n| --- | --- |\n| A | 和/或 |")
    and_or = [item for item in findings if item["rule"] == "and-or"]
    assert len(and_or) == 1 and and_or[0]["line"] == 3, findings
    assert and_or[0]["column"] == 7, findings
    findings, _ = lint("甲" * 91 + "。")
    assert any(item["rule"] == "long-sentence" for item in findings)
    findings, _ = lint("甲" * 91 + "。", mode="clear")
    assert not any(item["rule"] == "long-sentence" for item in findings)
    print("selftest OK")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="UTF-8 files; otherwise stdin")
    parser.add_argument("--mode", choices=("strict", "clear"), default="strict")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--baseline", type=int, default=0,
                        help="allowed number of hard findings")
    parser.add_argument("--disable", default="", help="comma-separated rule names")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        selftest()
        return 0
    if args.baseline < 0:
        parser.error("--baseline must be zero or greater")
    known = {item[0] for item in RULES} | {
        "semicolon", "long-sentence", "synonym-rotation", "dangling-conjunction"}
    disabled = {item.strip() for item in args.disable.split(",") if item.strip()}
    unknown = disabled - known
    if unknown:
        parser.error(f"unknown rule(s): {', '.join(sorted(unknown))}")

    results, han_count = [], 0
    if args.files:
        for filename in args.files:
            try:
                content = Path(filename).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                parser.error(f"cannot read {filename}: {error}")
            findings, count = lint(content, filename, args.mode)
            results.extend(findings)
            han_count += count
    else:
        results, han_count = lint(sys.stdin.read(), "<stdin>", args.mode)
    results = [item for item in results if item["rule"] not in disabled]
    hard_count = sum(item["severity"] == "hard" for item in results)
    density = round(len(results) * 100 / han_count, 1) if han_count else 0.0
    report = {"findings": results, "count": len(results),
              "hard_count": hard_count, "baseline": args.baseline,
              "han_characters": han_count, "per_100_han": density}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f'{item["path"]}:{item["line"]}:{item["column"]}: '
                  f'{item["severity"]} {item["rule"]}: {item["message"]} '
                  f'[{item["matched"]}]')
        print(f'{len(results)} findings ({hard_count} hard, baseline {args.baseline}); '
              f'{han_count} 汉字；每百汉字 {density} 项')
        print("“可能”“通常”等限定词本身不判错；确定性属于原文内容。")
    return int(hard_count > args.baseline)


if __name__ == "__main__":
    sys.exit(main())
