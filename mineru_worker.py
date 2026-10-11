"""MinerU 4 adapter. Integration must be checked on pilot PDFs before batch use."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys

DROP_TYPES = {"header", "footer", "page_number", "page_footnote_number", "image", "chart",
              "image_caption", "image_footnote", "chart_caption", "chart_footnote", "aside_text"}
KNOWN_TYPES = DROP_TYPES | {"text", "title", "list", "table", "equation",
                            "table_caption", "table_footnote", "footnote",
                            "code", "algorithm", "reference"}


def kind(block):
    value = block.type
    return getattr(value, "value", value)


def check_pages(expected, actual):
    if len(actual) != len(set(actual)):
        raise ValueError("引擎返回重复页，禁止拼接")
    return sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))


def issues_for_markdown(text):
    found = []
    for code, pattern in [
        ("image_fallback", r"!\[[^\]]*\]\([^\n]*\)|<img\b[^>]*>"),
        ("replacement_character", "\ufffd"),
        ("unconverted_math_delimiter", r"\\\[|\\\]|\\\(|\\\)"),
    ]:
        for match in re.finditer(pattern, text, re.I):
            found.append({"id": f"{code}:{match.start()}", "code": code,
                          "offset": match.start(), "context": text[max(0, match.start()-40):match.end()+40]})
    # A diagnostic, not proof that LaTeX or recognized symbols are correct.
    if len(re.findall(r"(?<!\\)\$", text)) % 2:
        found.append({"id": "unbalanced_dollars", "code": "unbalanced_dollars"})
    return found


def main(request_path):
    from mineru.parser import parse
    from mineru.parser.writer import FileBasedDataWriter
    from mineru.render import render, RenderFormat

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    installed = version("mineru")
    if installed.split(".")[0] != "4":
        raise ValueError(f"适配器只支持MinerU 4.x，当前: {installed}")
    kwargs = {"tier": request["tier"], "page_range": request["page_range"]}
    if request["tier"] not in {"standard", "advanced", "basic"}:
        raise ValueError("禁止flash作为最终公式/表格识别引擎")
    if request["engine"] == "local":
        result = parse(request["source"], **kwargs)
    elif request["engine"] == "self_hosted_v1":
        import os
        from mineru.parser import MinerUApiParser
        if not request["api_url"]:
            raise ValueError("缺少自建V1服务地址")
        client = MinerUApiParser(api_url=request["api_url"],
                                api_key=os.environ.get("MINERU_API_KEY", ""),
                                tier=request["tier"], include_images=True)
        result = client.parse(request["source"], page_range=request["page_range"])
    else:
        raise ValueError("只支持local/self_hosted_v1；托管API需要单独适配，不能直接套用V1")
    out = Path(request["output"])
    result.save(FileBasedDataWriter(str(out / "raw")))
    model = deepcopy(result.middle_json)
    actual = [p.page_idx + 1 for p in model.pages]
    missing, unexpected = check_pages(request["expected_pages"], actual)
    if missing or unexpected:
        raise ValueError(f"页覆盖有误: 缺失{missing}，额外{unexpected}；raw结果已保留")
    audit, issues, empty_pages, boundaries = [], [], [], []
    requested_drops = set(request["drop_blocks"])
    matched_drops = set()
    corrections = request.get("correct_blocks", {})
    matched_corrections = set()
    kept_pages = set(request.get("keep_pages") or request["expected_pages"])
    for page in model.pages:
        retained = []
        for block in page.blocks:
            loc = f"{page.page_idx + 1}:{block.index}"
            block_kind = kind(block)
            original = block.model_dump(mode="json")
            original_hash = hashlib.sha256(json.dumps(original, sort_keys=True,
                                                       ensure_ascii=False).encode()).hexdigest()
            correction = corrections.get(loc)
            if correction:
                matched_corrections.add(loc)
                if correction.get("expected_source_sha256") != original_hash:
                    raise ValueError(f"修正的原始块校验失败: {loc}")
                if not correction.get("reason", "").strip():
                    raise ValueError("修正必须说明原页核对依据")
                replacement = dict(original)
                replacement["content"] = correction["content"]
                block = type(block).model_validate(replacement)
            manual_reason = request["drop_blocks"].get(loc)
            if manual_reason is not None:
                matched_drops.add(loc)
                if not isinstance(manual_reason, str) or not manual_reason.strip():
                    raise ValueError("人工删除必须填写原因")
            reason = ("outside_body" if page.page_idx + 1 not in kept_pages else None)
            reason = reason or manual_reason or (f"layout:{block_kind}" if block_kind in DROP_TYPES else None)
            if reason and correction:
                raise ValueError(f"同一块不能同时修正和删除: {loc}")
            audit.append({"locator": loc, "page": page.page_idx + 1,
                          "type": block_kind, "action": "drop" if reason else ("correct" if correction else "keep"),
                          "reason": reason,
                          "source": original, "source_sha256": original_hash,
                          "correction": correction})
            if not reason:
                retained.append(block)
                if block_kind not in KNOWN_TYPES:
                    issues.append({"id": f"unknown_type:{loc}", "code": "unknown_type",
                                   "locator": loc, "type": block_kind})
        page.blocks = retained
        if not retained and page.page_idx + 1 in kept_pages:
            empty_pages.append(page.page_idx + 1)
        # Preserve original order and continues_prev; native renderer decides joining.
        boundaries.append({"page": page.page_idx + 1,
                           "first_block": retained[0].index if retained else None,
                           "last_block": retained[-1].index if retained else None})
    if requested_drops - matched_drops:
        raise ValueError(f"人工删除定位不存在: {sorted(requested_drops - matched_drops)}")
    if set(corrections) - matched_corrections:
        raise ValueError(f"修正定位不存在: {sorted(set(corrections) - matched_corrections)}")
    text = render(model, RenderFormat.MARKDOWN)
    # Do not strip image fallbacks with regex: a formula/table could be image-only.
    # Such output remains a draft and cannot be published until fixed.
    issues.extend(issues_for_markdown(text))
    quality = {"engine_version": installed, "source_sha256": request["source_sha256"],
               "expected_pages": request["expected_pages"], "actual_pages": actual,
               "missing_pages": missing, "unexpected_pages": unexpected,
               "empty_pages": empty_pages, "issues": issues,
               "page_boundaries": boundaries,
               "status": "needs_human_review",
               "note": "页覆盖通过不等于OCR完整/准确；逐项核对删除块、跨页、公式、表格。"}
    (out / "body.md").write_text(text.strip() + "\n", encoding="utf-8")
    for name, value in [("audit.json", audit), ("quality.json", quality)]:
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成待审核正文；{len(actual)}页，{len(issues)}项诊断，{len(empty_pages)}页无保留块")


if __name__ == "__main__":
    main(sys.argv[1])
