"""Hosted ZIP -> audited body draft. Supported schemas are explicit, never guessed."""
from __future__ import annotations

import hashlib
import json
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

from hosted_api import acquire_chunk, atomic_json, chunk_plan
from mineru_worker import DROP_TYPES, issues_for_markdown
from settings import DEFAULTS, merge


def is_misclassified_caption(block, neighbors, options=None):
    options=options or DEFAULTS['cleaning']['figure_caption']
    if not options['enabled']:
        return False
    text = block.get("text", "").strip()
    box = block.get("bbox")
    if block["type"] != "text" or not box or not re.fullmatch(options['pattern'], text):
        return False
    for item in neighbors:
        image_box = item.get("bbox")
        if item["type"] in {"image", "chart"} and image_box:
            overlap = min(box[2], image_box[2]) - max(box[0], image_box[0])
            if overlap > options['overlap_ratio'] * (box[2]-box[0]) and options['min_gap'] <= box[1]-image_box[3] <= options['max_gap']:
                return True
    return False


def continuation(previous, current, options=None):
    """Conservative join; retain both original blocks and record every operation."""
    a, b = previous[2].strip(), current[2].strip()
    options=options or DEFAULTS['cleaning']['paragraph_join']
    if not options['enabled'] or previous[3].get('type') not in options['block_types'] or current[3].get('type') not in options['block_types']:
        return False
    if previous[3].get('text_level') or current[3].get('text_level'):
        return False
    if not (len(a) >= options['min_previous_chars'] and a and b and re.search(options['previous_end_pattern'], a)
            and re.match(options['next_start_pattern'], b)):
        return False
    if re.match(options['next_heading_pattern'], b):
        return False
    if not 0 <= current[1]-previous[1] <= options['max_page_gap']:
        return False
    box_a, box_b = previous[3].get('bbox'), current[3].get('bbox')
    if not box_a or not box_b:
        return False
    # Continued Chinese lines begin at the column margin, not a new indent.
    return box_b[0] <= box_a[0] + options['left_tolerance']


def string_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    if value in (None, []):
        return []
    raise ValueError("API列表结构未适配，禁止静默忽略")


def render_v1(block, options=None):
    """Reuse recognized text/LaTeX/HTML. No table-cell or formula reinterpretation."""
    category = block["type"]
    options=options or DEFAULTS['cleaning']
    if category in {"text", "title", "footnote", "page_footnote", "table_caption", "table_footnote", "ref_text", "aside_text",
                    "header", "footer", "page_number", "page_footnote_number", "image_caption", "image_footnote", "chart_caption", "chart_footnote"}:
        value = block.get("text", block.get("content", ""))
        if not isinstance(value, str):
            raise ValueError("文字块content不是字符串；需要适配实际API包")
        level = block.get("text_level", block.get("level", 0))
        return ("#" * min(int(level), 6) + " " if level else "") + value
    if category == "equation":
        value = block.get("text", block.get("content", ""))
        if not isinstance(value, str) or not value.strip():
            raise ValueError("公式没有文字LaTeX，不能当图片删除")
        value = value.strip()
        # Preserve the recognized label, remove print-only leader dots/parentheses.
        def tag_label(match):
            label = re.search(r'([A-Za-z]?\s*\.?\s*\d+(?:\s*\.\s*\d+)*)', match.group(1))
            return r'\tag{' + re.sub(r'\s+', '', label.group(1)).lstrip('.') + '}' if label else ''
        if options['normalization']['formula_tags']:
            value = re.sub(r'\\tag\{([^{}]*)\}', tag_label, value)
        if value.startswith("$$") and value.endswith("$$"):
            return value
        if value.startswith("$") and value.endswith("$"):
            value = value[1:-1]
        return "$$\n" + value + "\n$$"
    if category == "table":
        body = block.get("table_body", block.get("content", ""))
        if not isinstance(body, str) or not body.strip():
            raise ValueError("表格没有结构化文字，不能用图像回退代替")
        def cell_delimiters(match):
            value=match.group(1)
            while r'\(' in value or r'\)' in value:
                token=re.search(r'\\[()]',value)
                inside=len(re.findall(r'(?<!\\)\$',value[:token.start()]))%2==1
                replacement=('$' if (token.group()==r'\(' and not inside)
                               or (token.group()==r'\)' and inside) else '')
                value=value[:token.start()]+replacement+value[token.end():]
            return '<td>'+value+'</td>'
        # Mixed \(...$ exports: normalize delimiters, preserve equation content.
        if options['normalization']['table_math_delimiters']:
            body=re.sub(r'<td>(.*?)</td>',cell_delimiters,body,flags=re.S)
        def literal_math(match):
            value=html.unescape(match.group(1)).replace('<',r'\lt ').replace('>',r'\gt ')
            return '$'+value+'$'
        if options['normalization']['table_math_entities']:
            body=re.sub(r'(?<!\\)\$([^$<>]*?)(?<!\\)\$',literal_math,body,flags=re.S)
        return "\n\n".join(string_list(block.get("table_caption")) + [body]
                             + string_list(block.get("table_footnote")))
    if category == "list":
        items = string_list(block.get("list_items"))
        if not items:
            raise ValueError("列表为空或结构未适配")
        return "\n".join(items)  # Preserve numbering recognized by MinerU.
    if category == "code":
        value = block.get("code_body", block.get("text", ""))
        if not isinstance(value, str) or not value:
            raise ValueError("代码块无可识别正文")
        return "```\n" + value + "\n```"
    if category in {'image','chart'} and options['image_policy']=='retain':
        source=block.get('img_path')
        if not source:
            raise ValueError('图像块没有本地资产路径')
        prefix='image' if category=='image' else 'chart'
        return '\n\n'.join(string_list(block.get(prefix+'_caption'))+[f'![]({source})']+string_list(block.get(prefix+'_footnote')))
    raise ValueError(f"未适配块类型: {category}")


def chunk_key(request,chunk):
    value={'upload':chunk['upload_pages'],'source':request['source_sha256'],'model':request['hosted_model']}
    recognition=request.get('api_options',{}).get('recognition',{})
    if recognition and recognition!={'language':'ch','is_ocr':True,'enable_formula':True,'enable_table':True}:
        value['recognition']=recognition
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:16]


def materialize_images(text,raw,output,key,assets):
    """Copy local assets from a verified result; never fetch remote images."""
    import shutil
    raw=Path(raw).resolve()
    def copy_reference(reference):
        if re.match(r'^[A-Za-z]+:',reference):
            raise ValueError('只支持结果包内本地图像，不能自动获取远程图像')
        candidate=(raw/reference).resolve()
        if not candidate.is_relative_to(raw):
            raise ValueError('图像路径越界')
        if not candidate.is_file():
            candidate=(raw/'images'/Path(reference).name).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(raw):
            raise ValueError('结果包图像缺失')
        asset_hash=hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
        relative=Path('assets')/key/(asset_hash+'_'+candidate.name)
        destination=Path(output)/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(candidate,destination)
        assets.add(relative.as_posix())
        return relative.as_posix()
    text=re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])',lambda m:m.group(1)+copy_reference(m.group(2))+m.group(3),text,flags=re.I)
    return re.sub(r'(!\[[^\]]*\]\()([^\n)]+)(\))',lambda m:m.group(1)+copy_reference(m.group(2))+m.group(3),text)


def load_bundle(raw, upload_pages, unmerge_tables=False):
    """Require page coverage from layout, not just pages with non-empty text."""
    raw = Path(raw)
    layout_paths = list(raw.rglob("layout.json")) + list(raw.rglob("middle_json.json"))
    if len(layout_paths) != 1:
        raise ValueError("结果包缺少唯一layout/middle JSON；先验证首个API包格式")
    layout = json.loads(layout_paths[0].read_text(encoding="utf-8"))
    layout_pages = layout.get("pdf_info", layout.get("pages"))
    if not isinstance(layout_pages, list):
        raise ValueError("无法识别页级布局结构")
    indices = [p.get("page_idx") for p in layout_pages]
    if indices != list(range(len(upload_pages))):
        raise ValueError("API返回页序与上传分片不一致；禁止猜测页映射")
    content_paths = list(raw.rglob("*_content_list.json"))
    if not content_paths:
        content_paths = list(raw.rglob("content_list.json"))
    if len(content_paths) == 1:
        items = json.loads(content_paths[0].read_text(encoding="utf-8"))
        if not isinstance(items, list) or not all(isinstance(b, dict) and "type" in b and "page_idx" in b for b in items):
            raise ValueError("只支持带page_idx的Content List V1；其他协议需首包验证后适配")
    else:
        paths = list(raw.rglob("structured_content.json"))
        if len(paths) != 1:
            raise ValueError("结果包没有唯一content_list或structured_content")
        structured = json.loads(paths[0].read_text(encoding="utf-8"))
        items = []
        for page in structured["pages"]:
            for block in page["blocks"]:
                items.append(dict(block, page_idx=page["page_idx"]))
    for block in items:
        if not isinstance(block["page_idx"], int) or not 0 <= block["page_idx"] < len(upload_pages):
            raise ValueError("内容块页索引越界")
        if block['type']=='table' and unmerge_tables:
            page=layout_pages[block['page_idx']]
            native=[b for b in page.get('preproc_blocks',[]) if b.get('type')=='table']
            size=page.get('page_size')
            box=block.get('bbox')
            if native and size and box:
                def difference(candidate):
                    raw_box=candidate['bbox']
                    normalized=[raw_box[0]*1000/size[0],raw_box[1]*1000/size[1],
                                raw_box[2]*1000/size[0],raw_box[3]*1000/size[1]]
                    return sum(abs(a-b) for a,b in zip(normalized,box))
                match=min(native,key=difference)
                if difference(match)<30:
                    body=[];captions=[];notes=[]
                    for child in match.get('blocks',[]):
                        parts=[]
                        for line in child.get('lines',[]):
                            for span in line.get('spans',[]):
                                if span.get('type')=='table' and span.get('html'):
                                    body.append(span['html'])
                                elif span.get('type')=='inline_equation':
                                    parts.append('$'+span.get('content','')+'$')
                                else:
                                    parts.append(span.get('content',''))
                        if child.get('type')=='table_caption':
                            captions.append(''.join(parts))
                        elif child.get('type')=='table_footnote':
                            notes.append(''.join(parts))
                    if body:
                        def math_span(m):
                            value=m.group(1)
                            if r'\begin' not in value:
                                value=value.replace('\\\\','\\')
                            return '$'+value+'$'
                        block['_engine_merged_body']=block.get('table_body','')
                        block['table_body']=re.sub(r'<eq>(.*?)</eq>',math_span,'\n'.join(body),flags=re.S)
                        block['table_caption']=captions
                        block['table_footnote']=notes
                        block['_table_policy']='page_original_preproc:avoid_overlap_loss'
        if block['type'] == 'table' and not block.get('table_body') and not block.get('img_path'):
            native = layout_pages[block['page_idx']].get('para_blocks', [])
            if any(b.get('type') == 'table'
                   and any(c.get('lines_deleted') for c in b.get('blocks', [])) for b in native):
                block['_merged_table_placeholder'] = True
    discarded = [p for p in layout_pages if p.get("discarded_blocks")]
    return items, discarded


def main(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    output = Path(request["output"])
    cleaning=merge(DEFAULTS['cleaning'],request.get('cleaning',{}))
    api_options=request.get('api_options',{})
    assets=set()
    chunk_index = []
    ordered, audit, issues = [], [], []
    retained_pages = set(request.get("keep_pages") or request["expected_pages"])
    kept_per_page = {p: 0 for p in retained_pages}
    matched_drops, matched_corrections = set(), set()
    chunks = chunk_plan(request["expected_pages"], request["hosted_chunk_pages"],
                        request["hosted_overlap_pages"])
    # Independent chunk uploads can share waiting time; file identity is fixed.
    acquisition=[]
    for chunk in chunks:
        chunk['source_sha256']=request['source_sha256']
        key=chunk_key(request,chunk)
        acquisition.append((chunk,output/'chunks'/key))
    if len(acquisition)>1:
        with ThreadPoolExecutor(max_workers=request.get('chunk_concurrency',3)) as pool:
            futures={pool.submit(acquire_chunk,request['source'],chunk,folder,
                                 request['hosted_model'],request.get('offline',False),api_options):folder.name
                     for chunk,folder in acquisition}
            failures=[]
            for future in as_completed(futures):
                try:
                    future.result()
                    print(f'分片缓存就绪 {futures[future]}',flush=True)
                except Exception as exc:
                    failures.append(f'{futures[future]}: {type(exc).__name__}: {exc}')
            if failures:
                raise RuntimeError('部分分片尚未成功；保留成功缓存。'+'; '.join(failures))
    for index, chunk in enumerate(chunks):
        chunk["source_sha256"] = request["source_sha256"]
        key = chunk_key(request,chunk)
        folder = output / "chunks" / key
        acquire_chunk(request["source"], chunk, folder, request["hosted_model"],
                      offline=request.get("offline", False),options=api_options)
        unmerge=cleaning['table_policy']=='per_page' or (cleaning['table_policy']=='auto' and len(chunks)>1)
        items, discarded = load_bundle(folder / "raw", chunk["upload_pages"],unmerge_tables=unmerge)
        owned = set(chunk["owned_pages"])
        chunk_index.append(dict(chunk, folder=key))
        if index and chunk["owned_pages"][0] == chunks[index-1]["owned_pages"][-1] + 1:
            issues.append({"id": f"chunk_seam:{chunk['owned_pages'][0]}", "code": "chunk_seam",
                           "note": "跨片可能有合并段落/表格导致重复或遗漏；对照两侧重叠页审核"})
        if discarded:
            issues.append({"id": f"backend_discarded:{key}", "code": "backend_discarded",
                           "note": "核查layout内discarded_blocks，确认仅为页眉页脚等非正文"})
        per_page = {}
        neighbors = {}
        margin_labels={}
        for item in items:
            neighbors.setdefault(item['page_idx'], []).append(item)
            value=re.sub(r'\s+','',item.get('text',''))
            box=item.get('bbox')
            margins=cleaning['repeated_margin']
            if value and len(value)<margins['max_chars'] and box and (box[1]<margins['top'] or box[3]>margins['bottom']):
                margin_labels.setdefault(value,set()).add(item['page_idx'])
        for block in items:
            page = chunk["upload_pages"][block["page_idx"]]
            ordinal = per_page.get(page, 0)
            per_page[page] = ordinal + 1
            loc = f"{key}:{page}:{ordinal}"
            original_hash = hashlib.sha256(json.dumps(block, sort_keys=True,
                                                       ensure_ascii=False).encode()).hexdigest()
            manual_drop = request["drop_blocks"].get(loc)
            correction = request.get("correct_blocks", {}).get(loc)
            reason = None
            if page not in owned:
                reason = "overlap_context"
            elif page not in retained_pages:
                reason = "outside_body"
            elif manual_drop is not None:
                if not isinstance(manual_drop, str) or not manual_drop.strip():
                    raise ValueError("删除必须有原页审核原因")
                matched_drops.add(loc)
                reason = manual_drop
            elif block["type"] in cleaning['drop_block_types']:
                reason = "layout:" + block["type"]
            elif any(re.search(pattern,block.get('text','')) for pattern in cleaning['drop_text_patterns']):
                reason='text_pattern:configured'
            elif block.get('_merged_table_placeholder'):
                reason = 'table_continuation:merged_by_backend'
                kept_per_page[page] += 1
            elif is_misclassified_caption(block, neighbors[block['page_idx']],cleaning['figure_caption']):
                reason = "image_caption:geometry_and_label"
            elif margins['enabled'] and block['type']=='text' and len(margin_labels.get(re.sub(r'\s+','',block.get('text','')),set()))>=margins['min_pages']:
                box=block.get('bbox')
                if box and (box[1]<margins['top'] or box[3]>margins['bottom']) and not block.get('text_level'):
                    reason='repeated_margin:header_footer'
            entry = {"locator": loc, "page": page, "type": block["type"],
                     "source": block, "source_sha256": original_hash,
                     "action": "drop" if reason else "keep", "reason": reason}
            if not reason:
                try:
                    if correction:
                        if correction.get("expected_source_sha256") != original_hash or not correction.get("reason", "").strip():
                            raise ValueError("修正缺少依据或源块不匹配")
                        text = correction["markdown"]
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError("修正必须是非空Markdown；删除另用drop_blocks")
                        matched_corrections.add(loc)
                        entry.update(action="correct", correction=correction)
                    else:
                        text = render_v1(block,cleaning)
                    if cleaning['image_policy']=='retain':
                        text=materialize_images(text,folder/'raw',output,key,assets)
                except ValueError as exc:
                    # Keep the original block in audit and block publication.
                    issues.append({"id": f"unrenderable:{loc}", "code": "unrenderable_block",
                                   "locator": loc, "note": str(exc)})
                    text = ""
                if text.strip():
                    ordered.append((loc, page, text, block))
                    kept_per_page[page] += 1
            audit.append(entry)
    if set(request["drop_blocks"]) - matched_drops:
        raise ValueError("存在无法定位或不在正文中的删除规则；清理旧规则后重试")
    if set(request.get("correct_blocks", {})) - matched_corrections:
        raise ValueError("存在无法定位/应用的修正规则")
    pieces, joins = [], []
    previous = None
    for entry in ordered:
        pair=previous[0]+'|'+entry[0] if previous is not None else ''
        if previous is not None and pair not in request.get('disabled_joins',{}) and continuation(previous, entry,cleaning['paragraph_join']):
            separator=cleaning['paragraph_join']['separator']
            if separator=='auto':
                separator=' ' if re.search(r'[A-Za-z0-9]$',pieces[-1]) and re.match(r'[A-Za-z0-9]',entry[2].strip()) else ''
            pieces[-1] += separator+entry[2].strip()
            joins.append({'before': previous[0], 'after': entry[0],
                          'tail': previous[2][-90:], 'head':entry[2][:90],
                          'reason':'Chinese continuation, no terminal punctuation, column margin'})
        else:
            pieces.append(entry[2])
        previous = entry
    text = "\n\n".join(pieces).strip() + "\n"
    issues.extend(i for i in issues_for_markdown(text) if cleaning['image_policy']=='omit' or i['code']!='image_fallback')
    boundaries = []
    for previous, current in zip(ordered, ordered[1:]):
        if previous[1] != current[1]:
            boundaries.append({"before": previous[0], "after": current[0],
                               "tail": previous[2][-120:], "head": current[2][:120]})
    atomic_json(output / "raw" / "index.json", {"chunks": chunk_index,
                                                "source_sha256": request["source_sha256"]})
    atomic_json(output / "audit.json", audit)
    atomic_json(output / "quality.json", {
        "engine_version": "hosted_v4:" + request["hosted_model"],
        "expected_pages": request["expected_pages"], "actual_pages": request["expected_pages"],
        "missing_pages": [], "unexpected_pages": [],
        "empty_pages": [p for p, count in kept_per_page.items() if not count],
        "issues": issues, "page_boundaries": boundaries, "continuation_joins": joins,
        "assets": sorted(assets), "cleaning": cleaning,
        "status": "needs_human_review",
        "note": "保留MinerU块及表格结构；页间/片间连续性需要原页审核和显式修正。"})
    (output / "body.md").write_text(text, encoding="utf-8")
    print(f"API结果已缓存，正文草稿生成；{len(chunks)}片，{len(issues)}项诊断。")


if __name__ == "__main__":
    main(sys.argv[1])
