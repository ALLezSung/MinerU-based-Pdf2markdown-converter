"""Reusable, configuration-driven MinerU PDF-to-Markdown CLI."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from common import digest, pages, read, write
from settings import DEFAULTS, HERE, example, load_task, validate


def binding(task):
    return {'task_id':task.config['task']['id'],'input_root':str(task.input_root)}


@contextmanager
def locked(task):
    task.state.mkdir(parents=True,exist_ok=True)
    marker=task.state/'task.json'
    if (task.state/'manifest.json').exists() and not marker.exists():
        raise ValueError('state已有未绑定的历史清单；使用独立目录并通过migrate迁移，不直接覆盖')
    if marker.exists() and read(marker)!=binding(task):
        raise ValueError('这个state目录属于另一个任务/输入根目录；请指定独立state_dir')
    lock=task.state/'.task.lock'
    try:
        handle=lock.open('x',encoding='utf-8')
    except FileExistsError:
        raise ValueError('任务正在写入或存在中断锁；检查对应进程后再删除state/.task.lock') from None
    try:
        handle.write(json.dumps({'pid':os.getpid(),'config':str(task.config_path)}));handle.close()
        write(marker,binding(task))
        yield
    finally:
        handle.close()
        lock.unlink(missing_ok=True)


def inspect_sources(task):
    from pypdf import PdfReader
    options=task.config['input']
    found={}
    for name in options['directories']:
        folder=(task.input_root/name).resolve()
        if not folder.is_relative_to(task.input_root) or not folder.is_dir():
            raise ValueError(f'输入目录不存在或越界: {name}')
        for file in folder.rglob('*') if options['recursive'] else folder.iterdir():
            if not file.is_file() or file.suffix.lower()!='.pdf':
                continue
            file=file.resolve()
            if not file.is_relative_to(task.input_root):
                raise ValueError('PDF符号链接指向输入根目录以外')
            if any(file.is_relative_to(root) for root in [task.state,task.output,task.delivery]):
                continue
            relative=file.relative_to(task.input_root).as_posix()
            if not any(fnmatchcase(relative.casefold(),p.casefold()) for p in options['include_patterns']):
                continue
            if any(fnmatchcase(relative.casefold(),p.casefold()) for p in options['exclude_patterns']):
                continue
            found[relative]=file
    if not found:
        raise ValueError('没有匹配的PDF；检查input_root/directories/include_patterns')
    docs=[]
    for relative,file in sorted(found.items()):
        row={'id':hashlib.sha256(relative.encode()).hexdigest()[:16],'source':relative,
             'sha256':digest(file),'bytes':file.stat().st_size,'page_count':None,'inspection_error':None}
        try:
            reader=PdfReader(str(file));row.update(page_count=len(reader.pages),encrypted=reader.is_encrypted)
        except Exception as exc:
            row['inspection_error']=str(exc)
        docs.append(row)
    write(task.state/'manifest.json',docs)
    print(f'清单：{len(docs)}份，页数已读取{sum(d["page_count"] is not None for d in docs)}份。无OCR。')


def initial_document(task,doc):
    row={'id':doc['id'],'source':doc['source'],'sha256':doc['sha256'],
         'pilot_pages':'','pilot_reason':'','convert_pages':'all','body_pages':'','deliver':True,
         'drop_blocks':{},'correct_blocks':{},'disabled_joins':{},'reviewed_empty_pages':[],
         'body_ranges_reviewed':False,'content_verified':False,'continuity_verified':False,
         'formula_table_verified':False,'acknowledged_issue_ids':[],'review_notes':'','source_quality':{}}
    row.update(deepcopy(task.config['documents']['defaults']))
    for rule in task.config['documents']['rules']:
        if fnmatchcase(doc['source'],rule['match']):
            row.update(deepcopy(rule['set']))
    return row


def make_plan(task):
    file=task.state/'plan.json'
    previous=read(file)['documents'] if file.exists() else []
    previous={r['id']:r for r in previous}
    rows=[]
    for doc in read(task.state/'manifest.json'):
        old=previous.get(doc['id'])
        if old and old.get('sha256')==doc['sha256']:
            row=old
        else:
            row=initial_document(task,doc)
            if old:
                row['review_notes']='源PDF已变化；已清空旧页范围、修正、删除规则和审核状态。'
        row.update(source=doc['source'],sha256=doc['sha256'])
        rows.append(row)
    write(file,{'schema_version':1,'task_id':task.config['task']['id'],'documents':rows})
    print(f'处理计划：{len(rows)}份。新PDF不会继承旧文件的修正及审核状态。')


def plan_rows(task):
    payload=read(task.state/'plan.json')
    if payload.get('task_id')!=task.config['task']['id']:
        raise ValueError('plan.json不属于当前task.id')
    rows=payload['documents']
    if not isinstance(rows,list) or len({r['id'] for r in rows})!=len(rows):raise ValueError('plan.documents必须为ID唯一的数组')
    docs={d['id']:d for d in read(task.state/'manifest.json')}
    for row in rows:
        if row['id'] not in docs or row['source']!=docs[row['id']]['source']:
            raise ValueError('plan的id/source与清单不一致，请勿手改生成字段')
        if row.get('sha256')!=docs[row['id']]['sha256']:
            raise ValueError('源PDF已变化；先运行plan以重置旧修正规则')
        for key in ['deliver','body_ranges_reviewed','content_verified','continuity_verified','formula_table_verified']:
            if type(row.get(key)) is not bool:raise ValueError(f'plan.{key}必须为JSON布尔值')
        for key in ['pilot_pages','convert_pages','body_pages']:
            if not isinstance(row.get(key),str):raise ValueError(f'plan.{key}必须为字符串')
        for key in ['drop_blocks','correct_blocks','disabled_joins']:
            if not isinstance(row.get(key,{}),dict):raise ValueError(f'plan.{key}必须为对象')
        for value in row.get('drop_blocks',{}).values():
            if not isinstance(value,str) or not value.strip():raise ValueError('删除规则必须填写文字原因')
        if 'chunk_pages' in row and type(row['chunk_pages']) is not int:raise ValueError('plan.chunk_pages必须为整数')
    return rows


def signature(task,doc,item,stage):
    settings={'source_sha256':doc['sha256'],'parse':task.config['parse'],'cleaning':task.config['cleaning'],
              'api_base_url':task.config['transport']['api_base_url'],
              'stage':stage,'selection':item.get('pilot_pages','') if stage=='pilot' else item.get('convert_pages','all'),
              'document':{k:item.get(k) for k in ['body_pages','chunk_pages','drop_blocks','correct_blocks','disabled_joins']},
              'renderer':digest(HERE/'hosted_worker.py'),'transport':digest(HERE/'hosted_api.py')}
    # Cache locations/concurrency are operational, not content.
    settings['parse']={k:v for k,v in settings['parse'].items() if k not in {'cache_dirs','document_concurrency','chunk_concurrency'}}
    return hashlib.sha256(json.dumps(settings,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def artifacts_valid(folder,stamp):
    return all((folder/name).is_file() and digest(folder/name)==sha for name,sha in stamp.get('artifacts',{}).items()) and bool(stamp.get('artifacts'))


def run(task,stage,ids=None,execute=False,offline=False):
    from hosted_api import chunk_plan
    if stage=='body' and task.config['review']['require_pilot'] and not task.config['review']['pilot_accepted']:
        raise ValueError('小样尚未验收；review.pilot_accepted=true后才允许批量')
    if stage=='pilot' and not ids:
        raise ValueError('pilot必须用--ids选择少量文档')
    manifest={r['id']:r for r in read(task.state/'manifest.json')}
    all_rows=plan_rows(task)
    selected=[r for r in all_rows if r.get('deliver',True) and (not ids or r['id'] in ids)]
    if not selected or (ids and set(ids)!={r['id'] for r in selected}):
        raise ValueError('无文档、ID未知或文档已排除')
    prepared=[]
    options=task.config['parse']
    for item in selected:
        doc=manifest[item['id']]
        source=(task.input_root/doc['source']).resolve()
        if not source.is_relative_to(task.input_root) or digest(source)!=doc['sha256'] or item['sha256']!=doc['sha256']:
            raise ValueError('源文件改变/路径越界，请重新inventory和plan')
        selection=item['pilot_pages'] if stage=='pilot' else item.get('convert_pages','all')
        target=pages(selection,doc['page_count'])
        retained=pages(item['body_pages'],doc['page_count']) if item.get('body_pages') else target
        if stage=='body' and not set(retained).issubset(target):
            raise ValueError('body_pages超出convert_pages')
        retained=sorted(set(retained)&set(target))
        if not retained:
            raise ValueError('样本范围与正文范围没有交集')
        chunks=chunk_plan(target,item.get('chunk_pages',options['chunk_pages']),options['overlap_pages'])
        prepared.append((doc,item,target,retained,source,selection))
        print(f'{stage}: {doc["id"]} {doc["source"]}，{len(target)}页/{len(chunks)}片')
    if not execute:
        print('仅预览；--execute才运行。clean即使执行也不会联网。');return
    def one(prepared_doc):
        doc,item,target,retained,source,selection=prepared_doc
        folder=task.state/stage/doc['id'];folder.mkdir(parents=True,exist_ok=True)
        stamp_path=folder/'completed.json';fingerprint=signature(task,doc,item,stage)
        if stamp_path.exists() and read(stamp_path).get('signature')==fingerprint and artifacts_valid(folder,read(stamp_path)):
            return doc['id'],True,'有效缓存'
        stamp_path.unlink(missing_ok=True)
        recognition={k:options[k] for k in ['language','is_ocr','enable_formula','enable_table']}
        if task.config['transport']['api_base_url']!=DEFAULTS['transport']['api_base_url']:
            recognition['service']=task.config['transport']['api_base_url']
        credential=deepcopy(task.config['credentials'])
        credential['env_file']=str(task.resolve(credential['env_file'])) if credential['env_file'] else ''
        request={'source':str(source),'source_sha256':doc['sha256'],'output':str(folder),
                 'expected_pages':target,'keep_pages':retained,'page_range':selection,'offline':offline,
                 'drop_blocks':item.get('drop_blocks',{}),'correct_blocks':item.get('correct_blocks',{}),
                 'disabled_joins':item.get('disabled_joins',{}),'cleaning':task.config['cleaning'],
                 'hosted_model':options['model'],'hosted_chunk_pages':item.get('chunk_pages',options['chunk_pages']),
                 'hosted_overlap_pages':options['overlap_pages'],'chunk_concurrency':options['chunk_concurrency'],
                 'api_options':{'recognition':recognition,'credentials':credential,'transport':task.config['transport'],
                     'cache_dirs':[str(task.resolve(root)/doc['id']/'chunks') for root in options['cache_dirs']],
                     **{k:options[k] for k in ['max_file_bytes','max_download_bytes','max_extract_bytes']}}}
        write(folder/'request.json',request)
        env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
        with (folder/'worker.log').open('w',encoding='utf-8') as log:
            process=subprocess.run([sys.executable,str(HERE/'hosted_worker.py'),str(folder/'request.json')],
                                   stdout=log,stderr=subprocess.STDOUT,env=env,check=False)
        if process.returncode:
            return doc['id'],False,str(folder/'worker.log')
        names=['body.md','audit.json','quality.json','raw/index.json']
        names += [p.relative_to(folder).as_posix() for p in (folder/'assets').rglob('*') if p.is_file()] if (folder/'assets').exists() else []
        write(stamp_path,{'signature':fingerprint,'artifacts':{name:digest(folder/name) for name in names}})
        new_sha=digest(folder/'body.md')
        if item.get('reviewed_markdown_sha256')!=new_sha and any(item.get(flag,False) for flag in task.config['review']['required_flags']):
            for flag in task.config['review']['required_flags']:item[flag]=False
            item['acknowledged_issue_ids']=[];item['reviewed_empty_pages']=[]
            item['reviewed_markdown_sha256']=''
            item['review_notes']=item.get('review_notes','')+' 输出内容变化或未绑定当前产物，旧审核状态已撤销。'
        return doc['id'],True,'完成'
    results=[]
    with ThreadPoolExecutor(max_workers=options['document_concurrency']) as pool:
        futures={pool.submit(one,r):r[0]['id'] for r in prepared}
        for future in as_completed(futures):
            try:
                result=future.result()
            except Exception as exc:
                result=(futures[future],False,type(exc).__name__+': '+str(exc))
            results.append(result);print(result[0],result[2])
    write(task.state/'run_results.json',[{'id':a,'success':b,'message':c} for a,b,c in results])
    write(task.state/'plan.json',{'schema_version':1,'task_id':task.config['task']['id'],'documents':all_rows})
    if not all(success for _,success,_ in results):
        raise ValueError('部分文档失败；成功结果已保留，查看run_results.json和worker.log后重试')


def validate_results(task):
    from hosted_worker import render_v1
    documents=[];expressions=[];errors=[]
    for item in plan_rows(task):
        if not item.get('deliver',True):continue
        folder=task.state/'body'/item['id']
        if not (folder/'completed.json').exists():
            errors.append({'id':item['id'],'code':'not_converted'});continue
        doc=next(r for r in read(task.state/'manifest.json') if r['id']==item['id'])
        if read(folder/'completed.json')['signature']!=signature(task,doc,item,'body') or not artifacts_valid(folder,read(folder/'completed.json')):
            errors.append({'id':item['id'],'code':'stale_artifacts'});continue
        if digest(task.input_root/doc['source'])!=doc['sha256']:
            errors.append({'id':item['id'],'code':'source_changed'});continue
        quality=read(folder/'quality.json');audit=read(folder/'audit.json');index=read(folder/'raw/index.json')
        owned=[page for chunk in index['chunks'] for page in chunk['owned_pages']]
        if owned!=pages(item.get('convert_pages','all'),doc['page_count']) or quality['missing_pages'] or quality['unexpected_pages']:
            errors.append({'id':item['id'],'code':'page_coverage'})
        text=(folder/'body.md').read_text(encoding='utf-8');expected=[]
        for block in audit:
            if block['action']=='drop':
                if not block['reason']:errors.append({'id':item['id'],'code':'unexplained_drop'})
                continue
            try:
                value=block['correction']['markdown'] if block['action']=='correct' else render_v1(block['source'],task.config['cleaning'])
            except ValueError:
                errors.append({'id':item['id'],'code':'unrenderable_block'});continue
            expected.append(value)
            if block['type']=='code':continue
            if len(re.findall(r'(?<!\\)\$',value))%2:
                errors.append({'id':item['id'],'code':'unbalanced_dollars','locator':block['locator']})
            for match in re.finditer(r'(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|(?<!\\)\$([^$]*?)(?<!\\)\$',value):
                expressions.append({'doc_id':item['id'],'locator':block['locator'],'page':block['page'],
                                    'display':match.group(1) is not None,'tex':match.group(1) if match.group(1) is not None else match.group(2)})
        # Asset references are rewritten deliberately; compare content after removing references.
        canonical=lambda v:re.sub(r'\s+','',re.sub(r'<img\b[^>]*>|!\[[^\]]*\]\([^\n)]*\)','',v,flags=re.I))
        if canonical(''.join(expected))!=canonical(text):errors.append({'id':item['id'],'code':'retained_text_mismatch'})
        if not text.strip() and not task.config['validation']['allow_empty_markdown']:errors.append({'id':item['id'],'code':'empty_markdown'})
        if task.config['cleaning']['image_policy']=='omit' and re.search(r'<img\b|!\[',text,re.I):errors.append({'id':item['id'],'code':'image_in_text_only'})
        for asset in quality.get('assets',[]):
            file=(folder/asset).resolve()
            if not file.is_relative_to(folder) or not file.is_file():errors.append({'id':item['id'],'code':'missing_asset'})
        for pattern in task.config['validation']['noise_patterns']:
            if re.search(pattern,text):errors.append({'id':item['id'],'code':'ocr_noise'})
        for issue in quality['issues']:
            if issue['code'] in task.config['validation']['blocking_issue_codes']:
                errors.append({'id':item['id'],'code':issue['code'],'issue_id':issue['id']})
        counts=Counter(b['type'] for b in audit if b['action']!='drop')
        documents.append({'id':item['id'],'source':doc['source'],'pdf_pages':doc['page_count'],
            'retained_physical_pages':len(pages(item['body_pages'],doc['page_count'])) if item.get('body_pages') else len(owned),
            'characters':len(text),'tables':counts['table'],'display_equations':counts['equation'],
            'corrections':sum(b['action']=='correct' for b in audit),'source_quality':item.get('source_quality',{}),
            'review_notes':item.get('review_notes',''),'empty_pages':quality['empty_pages'],
            'source_sha256':doc['sha256'],'markdown_sha256':digest(folder/'body.md')})
    write(task.state/'math_expressions.json',expressions)
    math={'checked':0,'errors':[],'enabled':task.config['validation']['math_syntax']}
    if math['enabled']:
        module=task.resolve(task.config['validation']['katex_module']) if task.config['validation']['katex_module'] else HERE/'math_check/node_modules/katex'
        (task.state/'math_validation.json').unlink(missing_ok=True)
        with (task.state/'math_validator.log').open('w',encoding='utf-8') as log:
            result=subprocess.run([task.config['validation']['node_executable'],str(HERE/'math_validator.cjs'),
                str(task.state/'math_expressions.json'),str(task.state/'math_validation.json'),str(module)],stdout=log,stderr=subprocess.STDOUT)
        if not (task.state/'math_validation.json').exists():
            raise ValueError('KaTeX未安装或node不可用；查看math_validator.log，或明确关闭validation.math_syntax')
        math.update(read(task.state/'math_validation.json'))
        if math['errors']:errors.append({'code':'math_syntax','count':len(math['errors'])})
    summary={'task':task.config['task'],'documents':documents,'count':len(documents),
             'processed_physical_pages':sum(r['pdf_pages'] for r in documents),'math':math,
             'errors':errors,'machine_checks_passed':not errors,
             'note':'程序验证不修改人工审核字段；原页准确性须依据任务审核记录判断。'}
    write(task.state/'validation.json',summary)
    print(f'验证：{len(documents)}份，{len(errors)}项机器检查错误。人工审核状态保持原值。')
    if errors:raise ValueError('验证未通过；查看validation.json')
    return summary


def release_paths(task):
    return [(item,task.output/Path(item['source']).with_suffix('.md')) for item in plan_rows(task) if item.get('deliver',True)]


def checked_release(task):
    report=read(task.state/'validation.json')
    if not report['machine_checks_passed']:raise ValueError('机器验证未通过')
    review=task.config['review'];ready=[]
    if review['require_pilot'] and not review['pilot_accepted']:raise ValueError('小样尚未验收')
    if task.output.exists() and any(task.output.iterdir()) and not (task.state/'published.json').exists():
        raise ValueError('output已有不属于当前任务的内容；请指定独立输出目录')
    for item,target in release_paths(task):
        folder=task.state/'body'/item['id'];quality=read(folder/'quality.json')
        doc=next(r for r in read(task.state/'manifest.json') if r['id']==item['id'])
        if not artifacts_valid(folder,read(folder/'completed.json')) or read(folder/'completed.json')['signature']!=signature(task,doc,item,'body') or digest(task.input_root/doc['source'])!=doc['sha256']:
            raise ValueError('来源、配置或产物变化，请重新clean和validate')
        checked=next((d for d in report['documents'] if d['id']==item['id']),None)
        if checked is None or digest(folder/'body.md')!=checked['markdown_sha256']:raise ValueError('验证结果过期')
        if review['require_manual_review'] and not all(item.get(flag,False) for flag in review['required_flags']):
            raise ValueError(f'人工审核未完成: {item["source"]}')
        if review['require_manual_review'] and item.get('reviewed_markdown_sha256')!=checked['markdown_sha256']:
            raise ValueError('人工审核未绑定当前MD；核对后填写reviewed_markdown_sha256或运行review --approve')
        if review['require_empty_page_review'] and not set(quality['empty_pages']).issubset(item.get('reviewed_empty_pages',[])):
            raise ValueError('空正文页尚未核验')
        if review['require_issue_acknowledgement'] and any(i['id'] not in item.get('acknowledged_issue_ids',[]) for i in quality['issues']):
            raise ValueError('仍有待确认诊断；修正或按原页确认后记录issue ID')
        if not target.resolve().is_relative_to(task.output):raise ValueError('输出路径越界')
        ready.append((folder,target))
    if not ready:raise ValueError('没有可发布文件')
    return ready


def publish(task):
    ready=checked_release(task)
    for folder,target in ready:
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(folder/'body.md',target)
        for relative in read(folder/'quality.json').get('assets',[]):
            asset=(folder/relative).resolve()
            if not asset.is_relative_to(folder):raise ValueError('资产路径越界')
            destination=target.parent/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(asset,destination)
    write(task.state/'published.json',{'task_id':task.config['task']['id'],'files':[
        {'path':str(target.relative_to(task.output)),'sha256':digest(target)} for _,target in ready]})
    print(f'发布{len(ready)}份MD：{task.output}')


def package(task):
    ready=checked_release(task)
    summary=read(task.state/'validation.json');published=read(task.state/'published.json')
    if not summary['machine_checks_passed']:raise ValueError('验证未通过')
    selected={str(target.relative_to(task.output)):item for item,target in release_paths(task)}
    if set(selected)!={r['path'] for r in published['files']}:raise ValueError('发布清单与当前plan不一致，请重新publish')
    for row in published['files']:
        if digest(task.output/row['path'])!=row['sha256']:raise ValueError('发布文件被修改')
    for folder,target in ready:
        for relative in read(folder/'quality.json').get('assets',[]):
            if digest(target.parent/relative)!=digest(folder/relative):
                raise ValueError('发布资产被修改；重新publish')
    manifest={r['id']:r for r in read(task.state/'manifest.json')}
    for item in plan_rows(task):
        if item.get('deliver',True):
            folder=task.state/'body'/item['id'];stamp=read(folder/'completed.json')
            if stamp['signature']!=signature(task,manifest[item['id']],item,'body') or not artifacts_valid(folder,stamp):
                raise ValueError('打包前配置/缓存改变；重新clean、validate和publish')
    options=task.config['delivery'];task.delivery.mkdir(parents=True,exist_ok=True)
    offset=options['utc_offset'];match=re.fullmatch(r'([+-])(\d{2}):(\d{2})',offset)
    if not match:raise ValueError('utc_offset格式为+08:00等')
    seconds=(int(match[2])*60+int(match[3]))*(1 if match[1]=='+' else -1)
    date=options['report_date'] or datetime.now(timezone(timedelta(minutes=seconds))).date().isoformat()
    lines=[f'# {task.config["task"]["name"]}转换报告','',f'日期：{date}。任务：{task.config["task"]["id"]}。',
           f'交付{len(selected)}份Markdown，处理源PDF物理页{summary["processed_physical_pages"]}页。',
           f'模式：{task.config["parse"]["model"]}；图像：{task.config["cleaning"]["image_policy"]}；表格：{task.config["cleaning"]["table_policy"]}。',
           f'机器检查通过；公式语法检查：{"开启" if summary["math"]["enabled"] else "关闭"}，检查{summary["math"]["checked"]}处。',
           '','机器检查覆盖解析结果的页序、保留块、资产及选定语法检查；不替代原PDF逐字人工校对。',
           '','## 源文件问题及排除文件','']
    for item in plan_rows(task):
        if not item.get('deliver',True):lines.append(f'- 不交付：{item["source"]}。{item.get("review_notes","")}')
        if item.get('source_quality'):lines.append(f'- {item["source"]}：{json.dumps(item["source_quality"],ensure_ascii=False)}')
    lines+=['','## 文件清单','','| 文件 | PDF页数 | 保留页数 | 表格块 | 独立公式块 | 修正块 |','|---|---:|---:|---:|---:|---:|']
    for row in summary['documents']:
        lines.append(f'| {row["source"]} | {row["pdf_pages"]} | {row["retained_physical_pages"]} | {row["tables"]} | {row["display_equations"]} | {row["corrections"]} |')
    lines+=['','## 任务备注','']+options['notes']
    report=task.delivery/options['report_name'].format(task_id=task.config['task']['id']);report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    metadata=task.delivery/options['manifest_name'].format(task_id=task.config['task']['id']);write(metadata,summary)
    archive=task.delivery/options['archive_name'].format(task_id=task.config['task']['id'])
    members={}
    for item,target in release_paths(task):
        relative=target.relative_to(task.output).as_posix()
        members[options['markdown_folder'].format(task_id=task.config['task']['id'])+'/'+relative]=target
        folder=task.state/'body'/item['id']
        for asset in read(folder/'quality.json').get('assets',[]):
            asset_path=target.parent/asset
            members[options['markdown_folder'].format(task_id=task.config['task']['id'])+'/'+asset_path.relative_to(task.output).as_posix()]=asset_path
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=options['compression_level']) as zip_file:
        for name,file in members.items():zip_file.write(file,name)
        if options['include_report']:zip_file.write(report,report.name)
        if options['include_manifest']:zip_file.write(metadata,metadata.name)
    with zipfile.ZipFile(archive) as zip_file:
        if zip_file.testzip() is not None:raise ValueError('交付ZIP校验失败')
    print(f'打包{len(selected)}份：{archive}')


def doctor(task):
    from hosted_api import load_token
    credentials=task.config['credentials']
    try:
        present=bool(load_token(credentials['token_env_var'],task.resolve(credentials['env_file']) if credentials['env_file'] else ''))
    except ValueError:present=False
    result={'task':task.config['task'],'input_root_exists':task.input_root.is_dir(),
            'dependencies':{name:bool(importlib.util.find_spec(name)) for name in ['pypdf','requests']},
            'curl_available':bool(shutil.which('curl.exe') or shutil.which('curl')),'credential_present':present,
            'katex_available':(task.resolve(task.config['validation']['katex_module']) if task.config['validation']['katex_module'] else HERE/'math_check/node_modules/katex').exists()}
    print(json.dumps(result,ensure_ascii=False,indent=2))


def init_task(path,input_root,name,profile):
    path=Path(path).resolve()
    if path.exists():raise ValueError('配置已存在，拒绝覆盖')
    task_id=re.sub(r'[^\w-]+','_',path.parent.name).strip('_') or 'new_task'
    settings=example(profile);settings['task']={'id':task_id,'name':name or path.parent.name}
    settings['paths']['input_root']=str(Path(input_root).resolve())
    validate(settings);write(path,settings);print(f'已创建任务配置：{path}')


def migrate(task,legacy):
    legacy=Path(legacy).resolve()
    docs=read(legacy/'state/manifest.json');rows=read(legacy/'state/plan.json')
    for doc in docs:
        if digest(task.input_root/doc['source'])!=doc['sha256']:raise ValueError('历史清单与当前输入文件不一致')
    for row in rows:
        body=legacy/'state/body'/row['id']/'body.md'
        if body.exists():row['reviewed_markdown_sha256']=digest(body)
    write(task.state/'manifest.json',docs)
    write(task.state/'plan.json',{'schema_version':1,'task_id':task.config['task']['id'],'documents':rows})
    print('迁移清单及文档规则；源缓存及既有交付文件未改写。')


def review(task,ids,approve):
    if not approve or not ids:raise ValueError('review需要--ids和--approve；仅在实际原页核验完成后执行')
    report=read(task.state/'validation.json')
    if not report['machine_checks_passed']:raise ValueError('先修正并通过机器验证')
    rows=plan_rows(task)
    if set(ids)-{r['id'] for r in rows}:raise ValueError('未知文档ID')
    for item in rows:
        if item['id'] not in ids:continue
        folder=task.state/'body'/item['id'];quality=read(folder/'quality.json')
        verified=next(r for r in report['documents'] if r['id']==item['id'])
        if digest(folder/'body.md')!=verified['markdown_sha256']:raise ValueError('机器验证已过期')
        for flag in task.config['review']['required_flags']:item[flag]=True
        item['reviewed_markdown_sha256']=verified['markdown_sha256']
        item['reviewed_empty_pages']=quality['empty_pages']
        item['acknowledged_issue_ids']=[i['id'] for i in quality['issues']]
    write(task.state/'plan.json',{'schema_version':1,'task_id':task.config['task']['id'],'documents':rows})
    print('已按显式--approve记录审核；没有修改PDF或MD。')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','doctor','inventory','plan','pilot','batch','clean','validate','review','publish','package','status','migrate'])
    parser.add_argument('--config',default=str(HERE/'config.json'))
    parser.add_argument('--ids',nargs='*')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approve',action='store_true')
    parser.add_argument('--input-root',default='.')
    parser.add_argument('--name',default='')
    parser.add_argument('--profile',choices=['text_only','with_images'],default='text_only')
    parser.add_argument('--legacy-dir',default=str(HERE))
    args=parser.parse_args(argv)
    try:
        if args.command=='init':init_task(args.config,args.input_root,args.name,args.profile);return
        task=load_task(args.config)
        if args.command=='doctor':doctor(task);return
        if args.command=='status':
            rows=plan_rows(task) if (task.state/'plan.json').exists() else []
            print(json.dumps({'task':task.config['task'],'documents':len(rows),'deliver':sum(r.get('deliver',True) for r in rows),
                              'completed':len(list((task.state/'body').glob('*/completed.json'))),
                              'state_dir':str(task.state),'output_dir':str(task.output)},ensure_ascii=False,indent=2));return
        if args.command in {'pilot','batch','clean'} and not args.execute:
            run(task,'pilot' if args.command=='pilot' else 'body',args.ids,False,args.command=='clean');return
        with locked(task):
            if args.command=='inventory':inspect_sources(task)
            elif args.command=='plan':make_plan(task)
            elif args.command in {'pilot','batch','clean'}:run(task,'pilot' if args.command=='pilot' else 'body',args.ids,True,args.command=='clean')
            elif args.command=='validate':validate_results(task)
            elif args.command=='publish':publish(task)
            elif args.command=='package':package(task)
            elif args.command=='review':review(task,args.ids,args.approve)
            elif args.command=='migrate':migrate(task,args.legacy_dir)
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as exc:
        parser.exit(1,f'停止：{exc}\n')


if __name__=='__main__':main()
