"""Official MinerU v4 acquisition; no API request at import time."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import threading
import queue
import shutil
import subprocess
from urllib.parse import urlparse
import zipfile

BASE = "https://mineru.net/api/v4"


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_token(env_var="MINERU_API_KEY", env_file=None):
    token = os.environ.get(env_var, "").strip()
    if not token:
        local = Path(__file__).parent / ".env.local" if env_file is None else (Path(env_file) if env_file else None)
        if local and local.exists():
            for line in local.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith(env_var+"="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if token.startswith("Bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("缺少API凭据；配置任务指定的环境变量或凭据文件。不输出凭据。")
    return token


def chunk_plan(selected, core_size=180, overlap=2):
    if not selected or selected != sorted(set(selected)):
        raise ValueError("选择页必须非空、排序且唯一")
    if core_size < 1 or overlap < 0 or core_size + 2 * overlap > 200:
        raise ValueError("分片含重叠后不能超过200页")
    # Never concatenate unrelated pilot windows into a fake continuous document.
    runs = []
    for page in selected:
        if not runs or page != runs[-1][-1] + 1:
            runs.append([page])
        else:
            runs[-1].append(page)
    result = []
    for run in runs:
        for start in range(0, len(run), core_size):
            owned = run[start:start + core_size]
            upload = run[max(0, start-overlap):min(len(run), start+core_size+overlap)]
            result.append({"owned_pages": owned, "upload_pages": upload})
    return result


def safe_extract(archive, target, max_bytes=3*1024**3):
    target = Path(target).resolve()
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if sum(m.file_size for m in members) > max_bytes:
            raise ValueError("结果ZIP解压体积异常")
        for member in members:
            name = member.filename.replace("\\", "/")
            destination = (target / name).resolve()
            if not destination.is_relative_to(target) or ":" in name:
                raise ValueError("结果ZIP路径越界")
        package.extractall(target)


class HostedClient:
    def __init__(self, token, settings=None, max_download_bytes=1024**3):
        import requests
        from settings import DEFAULTS, merge
        self.requests = requests
        self.headers = {"Authorization": f"Bearer {token}"}
        self.settings = merge(DEFAULTS['transport'], settings or {})
        self.max_download_bytes = max_download_bytes

    def curl(self):
        backend=self.settings['backend']
        executable=shutil.which('curl.exe') or shutil.which('curl')
        if backend=='curl' and not executable:
            raise ValueError('配置选择curl，但未找到可执行文件')
        return executable if backend!='requests' else None

    def api(self, method, endpoint, payload=None):
        # Avoid printing response bodies/exception URLs containing credentials.
        attempts = self.settings['query_retries'] if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.requests.request(method, self.settings['api_base_url'].rstrip('/') + endpoint,
                                                 headers=self.headers, json=payload,
                                                 timeout=(self.settings['connect_timeout'],self.settings['api_read_timeout']), allow_redirects=False)
                if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(self.settings['retry_backoff_base']**attempt)
                    continue
                break
            except self.requests.RequestException:
                if attempt + 1 < attempts:
                    time.sleep(self.settings['retry_backoff_base']**attempt)
                    continue
                raise RuntimeError("MinerU API网络异常；已保存的batch_id可恢复，未自动重复提交") from None
        if response.status_code != 200:
            raise RuntimeError(f"MinerU API HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("API没有返回有效JSON") from None
        if body.get("code") != 0:
            raise RuntimeError(f"MinerU业务错误码: {body.get('code')}；检查账号、额度及参数")
        return body["data"]

    def put(self, signed_url, path):
        if urlparse(signed_url).scheme != "https":
            raise ValueError("上传地址必须使用HTTPS")
        started = time.monotonic()
        print(f"上传主机 {urlparse(signed_url).hostname}；{Path(path).stat().st_size} bytes", flush=True)
        curl = self.curl()
        if curl:
            command=[curl,'--silent','--show-error',*(['--ipv4'] if self.settings['ipv4'] else []),'--connect-timeout',str(self.settings['upload_connect_timeout']),'--max-time',str(self.settings['upload_timeout']),
                     '--output',os.devnull,'--upload-file',str(path),
                     '--write-out','%{http_code} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total} %{size_upload} %{speed_upload}',signed_url]
            outcome=subprocess.run(command,capture_output=True,text=True)
            stats=outcome.stdout.strip().split()
            if outcome.returncode or not stats or stats[0] not in {'200','201','204'}:
                raise RuntimeError(f'文件上传失败(curl exit={outcome.returncode})；不输出签名地址')
            print(f'上传完成(curl)；TCP/TLS/首响应/总秒数/字节/速率: {" ".join(stats[1:])}',flush=True)
            return
        try:
            with Path(path).open("rb") as file:
                response = self.requests.put(signed_url, data=file, timeout=(self.settings['upload_connect_timeout'],self.settings['upload_timeout']),
                                             allow_redirects=False)
        except self.requests.RequestException as exc:
            raise RuntimeError(f"文件上传网络异常({type(exc).__name__})；未记录签名地址或凭据") from None
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(f"上传失败: HTTP {response.status_code}")
        print(f"上传完成；{round(time.monotonic()-started,1)}秒", flush=True)

    def download(self, url, path):
        if urlparse(url).scheme != "https":
            raise ValueError("下载地址必须使用HTTPS")
        candidates=[url]
        parsed=urlparse(url)
        origin=self.settings['origin_fallbacks'].get(parsed.hostname)
        if origin:candidates.append(parsed._replace(netloc=origin).geturl())
        curl=self.curl()
        if curl:
            temp=Path(path).with_suffix('.part')
            for candidate in candidates:
                command=[curl,'--silent','--show-error',*(['--ipv4'] if self.settings['ipv4'] else []),'--fail','--location','--proto-redir','=https',
                         '--connect-timeout',str(self.settings['connect_timeout']),'--max-time',str(self.settings['download_timeout']),'--max-filesize',str(self.max_download_bytes),
                         '--retry',str(self.settings['download_retries']),'--retry-delay',str(self.settings['download_retry_delay']),
                         '--output',str(temp),'--write-out','%{http_code}',candidate]
                result=subprocess.run(command,capture_output=True,text=True)
                if result.returncode==0 and result.stdout.strip()=='200':
                    temp.replace(path)
                    if candidate!=url:
                        print('已从MinerU同一存储源站下载结果，绕过CDN握手故障。',flush=True)
                    return
            raise RuntimeError(f'结果下载失败(curl exit={result.returncode})，任务保留，不输出下载地址')
        for candidate in candidates:
            for attempt in range(self.settings['download_retries']+1):
                try:
                    with self.requests.get(candidate,stream=True,timeout=(self.settings['connect_timeout'],self.settings['download_timeout'])) as response:
                        if response.status_code!=200:
                            continue
                        if urlparse(response.url).scheme!='https':raise ValueError('禁止降级到HTTP下载')
                        temp=Path(path).with_suffix('.part');size=0
                        with temp.open('wb') as handle:
                            for piece in response.iter_content(1024*1024):
                                size+=len(piece)
                                if size>self.max_download_bytes:raise ValueError('结果ZIP超过配置大小上限')
                                handle.write(piece)
                        temp.replace(path);return
                except self.requests.RequestException:
                    if attempt<self.settings['download_retries']:time.sleep(self.settings['download_retry_delay'])
        raise RuntimeError('结果下载网络异常，可从已有batch_id继续') from None

    def put_and_confirm(self, signed_url, path, batch_id):
        """OSS can accept bytes while its PUT response stalls. Confirm with API."""
        result = queue.Queue()
        def upload():
            try:
                self.put(signed_url, path)
                result.put(None)
            except Exception as exc:
                result.put(exc)
        threading.Thread(target=upload,daemon=True).start()
        deadline=time.monotonic()+self.settings['upload_timeout']
        while time.monotonic()<deadline:
            try:
                outcome=result.get_nowait()
            except queue.Empty:
                outcome='pending'
            if outcome is None:
                return
            states=self.api('GET',f'/extract-results/batch/{batch_id}').get('extract_result',[])
            if len(states)==1 and states[0].get('state') in {'pending','running','converting','done'}:
                print('服务端已接收文件并进入解析，继续查询。',flush=True)
                return
            if outcome!='pending':
                raise outcome
            time.sleep(self.settings['upload_poll_interval'])
        raise RuntimeError('上传确认超时；保留batch_id，下次先查询状态')


def acquire_chunk(source, chunk, folder, model="vlm", offline=False, options=None):
    """Create a local slice only on execution; preserve mapping to original pages."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    options=options or {}
    recognition=options.get('recognition',{})
    identity_fields={"source_sha256": chunk["source_sha256"],
                                          "upload_pages": chunk["upload_pages"],
                                          "model": model}
    if recognition and recognition!={'language':'ch','is_ocr':True,'enable_formula':True,'enable_table':True}:
        identity_fields['recognition']=recognition
    identity = hashlib.sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()
    stamp = folder / "downloaded.json"
    archive = folder / "result.zip"
    extract_limit=options.get('max_extract_bytes',3*1024**3)
    # Cache roots are explicit task configuration. Never alter the source cache.
    if not stamp.exists():
        for root in options.get('cache_dirs',[]):
            candidate=Path(root)/folder.name
            other_stamp=candidate/'downloaded.json'
            other_zip=candidate/'result.zip'
            if other_stamp.is_file() and other_zip.is_file():
                recorded=json.loads(other_stamp.read_text(encoding='utf-8'))
                if recorded.get('identity')==identity and recorded.get('zip_sha256')==hashlib.sha256(other_zip.read_bytes()).hexdigest():
                    recorded['archive_reference']=str(other_zip.resolve())
                    atomic_json(stamp,recorded)
                    break
    if stamp.exists():
        stored=json.loads(stamp.read_text(encoding='utf-8'))
        if stored.get('identity')==identity and stored.get('archive_reference'):
            reference=Path(stored['archive_reference']).resolve()
            roots=[Path(root).resolve() for root in options.get('cache_dirs',[])]
            if not any(reference.is_relative_to(root) for root in roots):
                raise ValueError('外部缓存引用不在已配置缓存目录内')
            archive=reference
    if stamp.exists() and archive.exists():
        recorded = json.loads(stamp.read_text(encoding="utf-8"))
        if recorded.get("identity") == identity and recorded.get("zip_sha256") == hashlib.sha256(archive.read_bytes()).hexdigest():
            safe_extract(archive, folder / "raw",extract_limit)
            return
    if offline:
        raise ValueError("缺少已校验的API结果缓存；clean不会联网获取")
    from pypdf import PdfReader, PdfWriter
    slice_path = folder / "input.pdf"
    reader, writer = PdfReader(str(source)), PdfWriter()
    for number in chunk["upload_pages"]:
        writer.add_page(reader.pages[number-1])
    writer.write(str(slice_path))
    if slice_path.stat().st_size > options.get('max_file_bytes',200000000):
        raise ValueError("分片超过200MB；减小hosted_chunk_pages后重新预览")
    atomic_json(folder / "page_map.json", chunk)
    credentials=options.get('credentials',{})
    client = HostedClient(load_token(credentials.get('token_env_var','MINERU_API_KEY'),credentials.get('env_file')),
                          options.get('transport'),options.get('max_download_bytes',1024**3))
    job_path = folder / "job.json"
    if job_path.exists():
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if job["identity"] != identity:
            raise ValueError("已有任务与当前输入不匹配；不覆盖远程任务记录")
        if not job.get("uploaded"):
            existing = client.api("GET", f"/extract-results/batch/{job['batch_id']}").get('extract_result', [])
            if len(existing) == 1 and existing[0].get('state') in {'pending','running','converting','done'}:
                job['uploaded'] = True
                atomic_json(job_path,job)
            elif len(existing) == 1 and existing[0].get('state') == 'waiting-file':
                # No OCR task was launched for this upload. Preserve history and
                # acquire a fresh signed URL instead of resubmitting a parsed file.
                atomic_json(folder / f"interrupted-{job['batch_id']}.json",job)
                job = None
            else:
                raise ValueError("上传状态不明确；保留原任务记录，禁止重复提交")
    else:
        job = None
    if job is None:
        data = client.api("POST", "/file-urls/batch", {
            "files": [{"name": "input.pdf", "data_id": identity[:32], "is_ocr": recognition.get('is_ocr',True)}],
            "model_version": model, "language": recognition.get('language','ch'),
            "enable_formula": recognition.get('enable_formula',True), "enable_table": recognition.get('enable_table',True)})
        if len(data["file_urls"]) != 1:
            raise ValueError("API上传链接数量异常")
        job = {"identity": identity, "batch_id": data["batch_id"], "uploaded": False}
        atomic_json(job_path, job)
        client.put_and_confirm(data["file_urls"][0], slice_path, job['batch_id'])
        job["uploaded"] = True
        atomic_json(job_path, job)
    deadline = time.monotonic() + client.settings['poll_timeout']
    while time.monotonic() < deadline:
        data = client.api("GET", f"/extract-results/batch/{job['batch_id']}")
        items = data.get("extract_result", [])
        if len(items) > 1:
            raise ValueError("返回多个结果，无法对应本分片")
        if items:
            item = items[0]
            if item["state"] == "failed":
                raise RuntimeError("MinerU解析失败；保留batch_id供查询，不自动重复付费提交")
            if item["state"] == "done":
                client.download(item["full_zip_url"], archive)
                safe_extract(archive, folder / "raw",extract_limit)
                atomic_json(stamp, {"identity": identity,
                                    "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
                return
        time.sleep(client.settings['poll_interval'])
    raise RuntimeError("本轮等待超时；任务仍可继续，下次执行通过已有batch_id恢复")
