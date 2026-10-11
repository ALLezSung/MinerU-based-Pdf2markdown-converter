# 可复用的 PDF → Markdown 转换工作流

统一入口为 `converter.py`。支持 MinerU 托管精准 API 的 `vlm`、`pipeline` 模式；输入为 PDF，结果在本地按配置清洗、验证和打包。当前通用入口不支持本地 MinerU 或其他 OCR 后端。

## 配置分工

- `config.json`：任务名称、输入目录、识别参数、分片与并发、网络重试、清洗规则、验收策略和交付文件名。省略字段采用 `settings.py` 默认值；未知字段和类型错误会停止执行。
- `<state_dir>/plan.json`：自动生成的逐文件计划。填写小样页、正文页范围、排除文件、块级修正和人工审核结果。每个任务生成独立计划。
- 凭据：环境变量或 `.env.local`，与普通配置分开；配置中只填写环境变量名称和凭据文件路径。

完整模板为 `config.example.json`，结构校验为 `config.schema.json`；`examples/with_images.json` 提供保留图片的配置组合。配置路径相对于配置文件目录解析，运行目录必须互相独立。

`documents.defaults` 与 `documents.rules` 用于生成新文档的计划，规则按顺序覆盖，`match` 匹配相对源路径。已有且源文件未变的计划保留人工编辑；修改生成规则不会覆盖已有计划，应直接修改对应 `plan.json`。源 PDF 内容变化时会清空旧页范围、修正与审核状态。

## 新任务步骤

从转换器目录（包含 converter.py 的目录）执行；输入使用专门的 PDF 目录。

```powershell
$python = '.venv/Scripts/python.exe'
$cli = 'converter.py'
$config = 'tasks/new_task/config.json'
& $python $cli init --config $config --input-root 'D:/PDF资料' --name '新资料转换'
& $python $cli doctor --config $config
& $python $cli inventory --config $config
& $python $cli plan --config $config
```

`init` 不覆盖已有配置。保留图片时加 `--profile with_images`。凭据文件默认位于新配置旁，也可将 `credentials.env_file` 改为 `../../.env.local` 复用项目凭据，或设置 `MINERU_API_KEY` 环境变量。`env_file=""` 禁用文件读取。`doctor` 不输出密钥、不请求 API。

填写 `state/plan.json`：页码为原 PDF 从 1 开始的物理页，支持 `"1-5,8,12-20"`；`convert_pages` 支持 `"all"`，`body_pages` 为空表示保留所有识别页。可先识别再指定正文范围。`pilot_pages` 应覆盖公式、表格、跨页等代表样本。

```powershell
& $python $cli pilot --config $config
& $python $cli pilot --config $config --execute
```

不带 `--execute` 只预览；带该参数才上传和识别。核对 `state/pilot/<id>/body.md` 与源 PDF；通过后将配置 `review.pilot_accepted` 改成 `true`。

```powershell
& $python $cli batch --config $config --execute
& $python $cli validate --config $config
```

结果在 `state/body/<id>/`，包含 Markdown、保留块审计、问题清单和页码映射。`--ids 文件ID ...` 支持选定文件处理。修改正文范围或清洗规则后可离线重放：

```powershell
& $python $cli clean --config $config --execute
& $python $cli validate --config $config
```

`clean` 缺少匹配缓存会停止，绝不会转而请求 API。核对原 PDF 的正文、连续性、公式、表格，以及 `quality.json` 中的问题和空页后，记录审核并交付：

```powershell
& $python $cli review --config $config --ids 文件ID --approve
& $python $cli publish --config $config
& $python $cli package --config $config
```

`review --approve` 表示操作者已完成核验，并确认该文档的空页和告警。审核绑定当前 MD 哈希，内容变化需重新审核。机器检查不会自动批准人工审核；配置可明确关闭不需要的审核门槛。打包数量按实际清单生成，不固定为 24 份。

## 常用配置

| 配置组 | 用途 |
|---|---|
| `task`、`paths` | 任务标识与输入、缓存、输出、交付目录；路径支持 `{task_id}` |
| `input` | 子目录、包含/排除文件模式、递归 |
| `parse` | 模式、语言、OCR/公式/表格、分片、重叠页、并发、缓存目录、大小限制 |
| `transport` | API 地址、curl/requests、超时、轮询、重试、下载源站映射 |
| `cleaning` | 删除块类型与文本模式、图片、图题、重复页边内容、段落衔接、公式与表格归一化 |
| `documents` | 新文档计划默认值与按源路径匹配的规则 |
| `review` | 小样通过状态、人工审核字段、空页和告警确认要求 |
| `validation` | 公式语法、Node/KaTeX 路径、噪声模式、阻断问题类型 |
| `delivery` | ZIP/报告/清单名称、Markdown 子目录、压缩级别、日期和备注 |

复杂表格保留 HTML，其他表格沿用 MinerU 结果。`table_policy=auto` 在多分片场景优先采用逐页原表，避免跨片合并丢失；`per_page` 强制逐页原表，`engine_merged` 使用引擎合并表。公式使用 `$...$` / `$$...$$`。

图片可 `omit` 或 `retain`；保留图片还需从 `drop_block_types` 移除相关类型，建议直接使用 `with_images` 模板。默认段落衔接规则面向中文，其他语言需调整或关闭 `paragraph_join`。

缓存按源哈希、识别参数和页码匹配；配置或清洗代码变化使完成标记失效。`parse.cache_dirs` 可指定已有缓存供只读复用。每个任务有写入锁，不同任务使用不同状态目录。机器检查验证已识别块、页序与语法，不能证明 OCR 逐字和公式语义正确；源文件缺页不会补写。

## 安装与测试

当前环境可以直接复用。新机器安装 Python 依赖；公式语法检查另需 Node 和 KaTeX。

```powershell
python -m venv .venv
& .venv/Scripts/python.exe -m pip install -r requirements-hosted.txt
npm ci --prefix math_check
& .venv/Scripts/python.exe -m unittest discover -s . -p 'test_*.py' -q
```

没有 Node/KaTeX 时可设置 `validation.math_syntax=false`，报告会说明关闭。测试使用本地模拟结果，不调用 MinerU。旧 `workflow.py` 等入口保留为兼容包装，推荐统一使用 `converter.py`。

## 仓库与私有数据

仓库只保存代码、通用配置模板、依赖清单和测试。根目录采用允许列表，新数据文件默认忽略。实际 `config.json`、`tasks/`、`state/`、`legacy/`、输入 PDF、解析缓存、报告、交付结果及凭据均通过 `.gitignore` 排除。新资料通过 `init` 创建独立任务。

不要使用 `git add -f` 强制提交私有资料；忽略规则不能移除已经进入 Git 历史的文件。发布前应检查 `git diff --cached --name-only`。
