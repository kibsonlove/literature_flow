# literature_flow · 项目指路

以 Zotero 为唯一数据源的本地文献流水线：订阅搜集 → AI 精读笔记回写 Zotero → 全文向量化知识库检索。

## 快速开始（下载后 3 步跑起来）

1. **装依赖**（需 Python 3.10+）：
   ```
   cd webapp
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```
2. **填配置**：进入 `webapp/config/`，把三个 `*.example.json` 各复制一份、去掉 `.example` 后缀，填入你自己的信息：
   - `settings.json` ← 你的 LLM API key（DeepSeek 或任意 openai 兼容服务）
   - `paths.json` ← 你电脑上 Zotero 的数据目录和安装路径
   - `embedding.json` ← 你的 Embedding API key（知识库功能用，硅基流动免费；不用知识库可跳过）
3. **启动**：双击 `webapp\start.bat`（或 `webapp` 目录下运行 `uvicorn app:app --port 8000`），浏览器自动打开 `http://127.0.0.1:8000`。

> 可选：MinerU 结构化解析（长文精读/知识库分块用）——在 `webapp/core/mineru_key.txt` 里放入你的 MinerU 平台 key，不放则相关功能自动降级。
> 可选：网页端功能（WebChat 精读/长文/领域包自动生成，零 API 费）——playwright 已含在 requirements.txt，只需在虚拟环境里执行 `playwright install chromium` 下载浏览器内核；首次使用会弹出浏览器登录一次。
> 详细上手、常见错误排查见 **`docs/项目架构与上手指南.md`**。

## 目录速览

| 目录 | 内容 |
|---|---|
| `webapp/` | 主应用（FastAPI + 前端），日常只用动这里的 `config/` |
| `scripts/subscribe/` | 文献订阅脚本（按期刊名单/关键词搜新文献） |
| `notes/` `reports/` | 运行后自动生成的本地数据（笔记素材包、HTML 报告），不入库 |
| `docs/` | 架构与上手指南 |

## 注意

- 首次使用前需在 `webapp/config/` 填入你自己的 API key 与 Zotero 路径（见上方第 2 步）；这些配置文件已被 gitignore，永远不会进入仓库。
- 本仓库只包含通用代码与文档；`notes/`、`reports/`、`archive/`、订阅名单等**个人研究数据由各使用者在本地自行生成**，不随仓库分发。
