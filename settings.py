"""Versioned task settings, defaults and strict validation. No network or mutations."""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
DROP = ["header", "footer", "page_number", "page_footnote_number", "image", "chart",
        "image_caption", "image_footnote", "chart_caption", "chart_footnote", "aside_text"]
DEFAULTS = {
    "schema_version": 1,
    "task": {"id": "new_task", "name": "PDF转换任务"},
    "paths": {"input_root": ".", "state_dir": "state", "output_dir": "output", "delivery_dir": "delivery"},
    "input": {"directories": ["."], "include_patterns": ["*.pdf"], "exclude_patterns": [], "recursive": True},
    "parse": {"engine": "hosted_v4", "model": "vlm", "language": "ch", "is_ocr": True,
              "enable_formula": True, "enable_table": True, "chunk_pages": 180, "overlap_pages": 2,
              "document_concurrency": 2, "chunk_concurrency": 3, "cache_dirs": [],
              "max_file_bytes": 200000000, "max_download_bytes": 1073741824, "max_extract_bytes": 3221225472},
    "credentials": {"token_env_var": "MINERU_API_KEY", "env_file": ".env.local"},
    "transport": {"api_base_url": "https://mineru.net/api/v4", "backend": "auto", "ipv4": True,
                  "connect_timeout": 15, "api_read_timeout": 45, "query_retries": 4,
                  "retry_backoff_base": 2, "upload_timeout": 1800, "upload_connect_timeout": 30,
                  "download_timeout": 600, "download_retries": 1, "download_retry_delay": 1,
                  "poll_interval": 5, "poll_timeout": 1800, "upload_poll_interval": 3,
                  "origin_fallbacks": {"cdn-mineru.openxlab.org.cn": "mineru.oss-cn-shanghai.aliyuncs.com"}},
    "cleaning": {
        "drop_block_types": DROP,
        "drop_text_patterns": [r"^The Ground Truth image", r"^The image contains", r"^The quick brown fox jumps"],
        "image_policy": "omit", "table_policy": "auto",
        "figure_caption": {"enabled": True, "pattern": r"图\s*\d+(?:[.\-－]\d+)*\s+[^。！？；\n]{1,65}",
                           "overlap_ratio": 0.6, "min_gap": -5, "max_gap": 70},
        "repeated_margin": {"enabled": True, "max_chars": 100, "min_pages": 3, "top": 120, "bottom": 925},
        "paragraph_join": {"enabled": True, "min_previous_chars": 35, "max_page_gap": 1, "left_tolerance": 14, "separator": "auto",
                           "block_types": ["text", "page_footnote"],
                           "previous_end_pattern": r"[\u4e00-\u9fff，,]$", "next_start_pattern": r"[\u4e00-\u9fff]",
                           "next_heading_pattern": r"^(?:第[一二三四五六七八九十\d]+[章节]|[一二三四五六七八九十]+[、．.]|注[：:]|说明[：:]|式中[：:]|图\s*\d|表\s*\d)"},
        "normalization": {"formula_tags": True, "table_math_delimiters": True, "table_math_entities": True}
    },
    "documents": {"defaults": {"pilot_pages": "", "pilot_reason": "", "convert_pages": "all", "body_pages": "", "deliver": True,
                                 "drop_blocks": {}, "correct_blocks": {}, "disabled_joins": {}, "source_quality": {}, "review_notes": ""}, "rules": []},
    "review": {"pilot_accepted": False, "require_pilot": True, "require_manual_review": True,
               "required_flags": ["body_ranges_reviewed", "content_verified", "continuity_verified", "formula_table_verified"],
               "require_empty_page_review": True, "require_issue_acknowledgement": True},
    "validation": {"math_syntax": True, "node_executable": "node", "katex_module": "",
                   "allow_empty_markdown": False, "noise_patterns": [r"The quick brown fox", r"The image contains", r"According to Rule", r"\[Empty String\]"],
                   "blocking_issue_codes": ["unrenderable_block", "missing_asset", "replacement_character", "unbalanced_dollars", "unconverted_math_delimiter"]},
    "delivery": {"archive_name": "{task_id}_markdown.zip", "report_name": "转换报告.md", "manifest_name": "交付清单.json",
                 "markdown_folder": "Markdown", "include_report": True, "include_manifest": True,
                 "compression_level": 9, "report_date": "", "utc_offset": "+08:00", "notes": []}
}


def merge(base, override, path=""):
    result = deepcopy(base)
    for key, value in override.items():
        field = f"{path}.{key}".lstrip(".")
        if key not in base:
            raise ValueError(f"未知配置项: {field}")
        original = base[key]
        if isinstance(original, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{field}必须是对象")
            flexible={'transport.origin_fallbacks','documents.defaults.drop_blocks','documents.defaults.correct_blocks','documents.defaults.disabled_joins','documents.defaults.source_quality'}
            result[key] = deepcopy(value) if field in flexible else merge(original, value, field)
        else:
            if type(value) is not type(original) and not (type(original) is float and type(value) is int):
                raise ValueError(f"{field}类型错误，应为{type(original).__name__}")
            result[key] = deepcopy(value)
    return result


def validate(config):
    if config["schema_version"] != 1:
        raise ValueError("不支持的schema_version")
    if not re.fullmatch(r"[\w-]+", config["task"]["id"]):
        raise ValueError("task.id只允许字母、中文、数字、下划线和短横线")
    parse = config["parse"]
    lists=[config['input']['directories'],config['input']['include_patterns'],config['input']['exclude_patterns'],
           parse['cache_dirs'],config['cleaning']['drop_block_types'],config['cleaning']['drop_text_patterns'],
           config['cleaning']['paragraph_join']['block_types'],config['review']['required_flags'],
           config['validation']['noise_patterns'],config['validation']['blocking_issue_codes'],config['delivery']['notes']]
    if any(not all(isinstance(v,str) for v in values) for values in lists):
        raise ValueError('目录、模式、类型、审核字段和备注列表必须只含字符串')
    if parse["engine"] != "hosted_v4" or parse["model"] not in {"vlm", "pipeline"}:
        raise ValueError("通用入口支持MinerU托管精准API：engine=hosted_v4，model=vlm/pipeline")
    if not 1 <= parse["chunk_pages"] <= 200 or not 0 <= parse["overlap_pages"] <= 99 or parse["chunk_pages"] + 2*parse["overlap_pages"] > 200:
        raise ValueError("分片和重叠页合计不能超过200页")
    for key in ["document_concurrency", "chunk_concurrency"]:
        if not 1 <= parse[key] <= 16:
            raise ValueError(f"parse.{key}必须为1-16")
    for key in ["max_file_bytes", "max_download_bytes", "max_extract_bytes"]:
        if parse[key] <= 0:
            raise ValueError(f"parse.{key}必须大于0")
    if parse["max_file_bytes"] > 200000000:
        raise ValueError("max_file_bytes不能超过当前API的200MB上限")
    if config["cleaning"]["image_policy"] not in {"omit", "retain"}:
        raise ValueError("image_policy必须为omit/retain")
    if config["cleaning"]["table_policy"] not in {"auto", "per_page", "engine_merged"}:
        raise ValueError("table_policy必须为auto/per_page/engine_merged")
    if config["transport"]["backend"] not in {"auto", "curl", "requests"}:
        raise ValueError("transport.backend必须为auto/curl/requests")
    from urllib.parse import urlparse
    endpoint=urlparse(config["transport"]["api_base_url"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("API必须使用HTTPS")
    for host in [*config['transport']['origin_fallbacks'],*config['transport']['origin_fallbacks'].values()]:
        if not isinstance(host,str) or not re.fullmatch(r'[A-Za-z0-9.-]+(?::\d+)?',host):
            raise ValueError('origin_fallbacks只接受主机名及可选端口，不接受URL/用户信息')
    for key, value in config["transport"].items():
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            raise ValueError(f"transport.{key}必须大于0")
    for pattern in config["cleaning"]["drop_text_patterns"] + config["validation"]["noise_patterns"]:
        re.compile(pattern)
    for pattern in [config["cleaning"]["figure_caption"]["pattern"], *[config["cleaning"]["paragraph_join"][k] for k in ["previous_end_pattern", "next_start_pattern", "next_heading_pattern"]]]:
        re.compile(pattern)
    if not 0 <= config["delivery"]["compression_level"] <= 9:
        raise ValueError("compression_level必须为0-9")
    if config['cleaning']['paragraph_join']['separator'] not in {'auto','',' '}:
        raise ValueError('paragraph_join.separator必须为auto、空字符串或空格')
    margin=config['cleaning']['repeated_margin'];caption=config['cleaning']['figure_caption'];joins=config['cleaning']['paragraph_join']
    if not (0<=margin['top']<margin['bottom']<=1000 and margin['min_pages']>=2 and margin['max_chars']>0):
        raise ValueError('repeated_margin阈值无效；bbox按0-1000归一化')
    if not (0<=caption['overlap_ratio']<=1 and caption['min_gap']<=caption['max_gap'] and joins['max_page_gap']>=0 and joins['min_previous_chars']>=0):
        raise ValueError('图题/段落连接阈值无效')
    allowed_doc = {"pilot_pages", "pilot_reason", "convert_pages", "body_pages", "deliver", "chunk_pages", "drop_blocks", "correct_blocks", "disabled_joins", "source_quality", "review_notes"}
    for rule in config["documents"]["rules"]:
        if not isinstance(rule, dict) or set(rule) != {"match", "set"} or not isinstance(rule["match"], str) or not isinstance(rule["set"], dict):
            raise ValueError("documents.rules项必须为{match:字符串,set:对象}")
        if set(rule["set"]) - allowed_doc:
            raise ValueError("documents.rules中存在未知文档选项")
    for key in ["archive_name", "report_name", "manifest_name", "markdown_folder"]:
        value = config["delivery"][key].format(task_id=config["task"]["id"])
        if not value or Path(value).name != value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError(f"delivery.{key}必须为单个文件/目录名称")


@dataclass(frozen=True)
class Task:
    config_path: Path
    config: dict
    input_root: Path
    state: Path
    output: Path
    delivery: Path

    def resolve(self, value):
        return (self.config_path.parent / value).resolve()


def load_task(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    config = merge(DEFAULTS, value)
    validate(config)
    resolved = {key: (path.parent / name.format(task_id=config["task"]["id"])).resolve() for key, name in config["paths"].items()}
    destinations = [resolved[k] for k in ["state_dir", "output_dir", "delivery_dir"]]
    for index, a in enumerate(destinations):
        for b in destinations[index+1:]:
            if a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError("state/output/delivery必须是互不嵌套的独立目录")
    if path.is_relative_to(resolved["output_dir"]) or path.is_relative_to(resolved["delivery_dir"]):
        raise ValueError("配置文件不能放在生成输出目录内")
    if resolved['input_root'] in destinations:
        raise ValueError('运行目录不能等于输入根目录')
    return Task(path, config, resolved["input_root"], *destinations)


def example(profile="text_only"):
    config = deepcopy(DEFAULTS)
    if profile == "with_images":
        config["cleaning"]["image_policy"] = "retain"
        config["cleaning"]["drop_block_types"] = [v for v in DROP if v not in {"image", "chart", "image_caption", "image_footnote", "chart_caption", "chart_footnote"}]
        config["cleaning"]["figure_caption"]["enabled"] = False
    elif profile != "text_only":
        raise ValueError("profile必须为text_only/with_images")
    return config
