# Asteria

给论文留一张安静的书桌。

Asteria 是一款本地优先的 AI 文献阅读工作台，将论文发现、文献整理、中文阅读和批注笔记放在同一个桌面应用中。不需要注册 Asteria 账号，文献库和笔记保存在自己的电脑上。

[下载桌面版](https://github.com/LexianLix/asteria/releases/latest) · [反馈问题](https://github.com/LexianLix/asteria/issues) · [MIT 许可证](LICENSE)

目前提供 Windows x64 桌面版。普通 PDF 阅读、批注和文献整理不依赖 AI 服务。

## 功能

### 发现与整理

- 从 arXiv、OpenAlex 获取论文元数据，按研究方向、分类和关键词筛选。
- 在应用运行期间定时采集，生成每日论文简报并导出 Markdown。
- 导入本地 PDF，按需下载开放获取的论文全文。
- 使用文件夹、标签、收藏和阅读状态整理文献；按 DOI 归组并切换来源版本。
- 搜索标题、摘要、已索引正文、译文与笔记，定位到相关页面。
- 可选 AI 摘要、标签和相关度辅助整理。

### 阅读与翻译

- 原文 PDF 连续阅读、缩放、章节导航与阅读位置记忆。
- 文字阅读、中文阅读和原文对照，支持字号调整与专注阅读。
- 选段、本页和全文翻译，支持暂停续跑、失败页重试与缺项补译。
- 术语表、手动修订，以及数字、引用等差异提示。
- 逐页检查当前译文缓存缺项，区分待翻译文字与可本地补齐的数字、保护原文；本地补齐不调用 AI。
- 本地正文检查提示无文字、英文逐字拆分与无法解码的字形，可定位原页；翻译前按所选范围提醒异常页面。
- 译文检查汇总数字、引用与上下标差异，支持定位原文，并按当前手动修订后的译文重新核对。
- 公式、图表和表格原页区域查看；可选本地增强解析与 OCR。

### 批注与知识整理

- 在原文上手写圈画、区域高亮，在文字模式中选择文字高亮。
- 页级笔记、草稿保存、笔记 Markdown 导出。
- 选段解释与论文问答，查看发送内容并通过引用返回原文。
- 整理论文研究卡片，分别保存 AI 提取内容和个人想法。
- 本地备份、导入验证与恢复，统一查看后台任务。

## 下载与使用

从 [Releases](https://github.com/LexianLix/asteria/releases/latest) 下载 Windows x64 安装包或便携包。

- 安装包：下载文件名包含 `Setup` 的安装程序并运行。
- 便携包：完整解压 Windows x64 ZIP，运行 `Asteria/Asteria.exe`，不要单独移动 EXE。

普通阅读不需要 Python。桌面窗口需要 Microsoft WebView2 Runtime。安装包目前未做代码签名，请核对下载来源和发布页提供的 SHA256，不要关闭系统安全防护。

首次使用可直接导入 PDF，或在设置中配置研究方向并采集论文。每日采集需要开启相应设置并保持应用运行；退出后不会继续执行，也不会自动创建开机启动或系统计划任务。

### 开始阅读

1. 导入本地 PDF，或设置研究方向后点击“获取新论文”。
2. 打开文献，在原文、文字和中文阅读模式间切换。
3. 需要翻译或问答时，先配置模型服务，再确认要发送的内容。
4. 使用“译文检查”查看缺项；数字与保护原文可在本地补齐，其他文字仍需翻译。
5. 圈画、高亮并记录笔记，按需导出 Markdown 或创建文献库备份。

### AI 服务

可在设置中选择 ChatGPT 账号授权、OpenAI 兼容接口或 Ollama 本地模型。各服务的可用模型、授权资格、费用和用量限制以服务提供方为准。

手动翻译、问答等操作会在发送前显示确认；自动摘要与分类需单独启用并授权。使用远程服务时，确认范围内的论文内容会发送到所选服务。个人想法不作为研究卡片的 AI 输入。

### 数据与阅读提示

- Windows 独立版默认将文献库保存在 `%LOCALAPPDATA%\Asteria\data`，与程序安装目录分开；卸载不会主动删除该文献库。
- 模型凭据不包含在文献备份中；ChatGPT 授权令牌使用当前 Windows 用户的 DPAPI 加密保存。
- 翻译和 AI 解读仅作阅读辅助，专业术语、数字、公式、复杂表格请结合原文核对。
- “缓存齐全”仅说明当前提取内容没有缺少缓存项，不代表译文准确或原文提取无遗漏。
- 中文阅读采用结构化排版，不是保留原版式的中文版 PDF。扫描文档的增强解析与 OCR 需要另行准备本地解析环境和模型。
- 论文来源与全文下载受原站点政策及论文许可约束；Asteria 不提供付费墙绕过功能。

## 从源码运行

<details>
<summary>安装与启动命令</summary>

需要 Python 3.11 或更高版本。Windows 桌面模式需要 WebView2 Runtime。

```powershell
git clone https://github.com/LexianLix/asteria.git
cd asteria
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[desktop]"
python -m paper_reader desktop
```

也可以运行 `python -m paper_reader desktop --browser` 使用本机浏览器界面。源码模式的数据默认保存在项目的 `data/` 下，与独立版文献库分开；如需迁移，请使用应用内备份与恢复。

</details>

## 致谢

- [EasyRead](https://github.com/Edwardxlai/easyread)：重要的设计与架构参考，为结构化中文阅读、原文对照、逐页翻译和本地批注保存提供了启发。感谢作者与贡献者的开源分享。
- [Mozilla PDF.js](https://github.com/mozilla/pdf.js)：原文 PDF 渲染及相关本地资源。
- [pdfplumber](https://github.com/jsvine/pdfplumber) 与 [pdfminer.six](https://github.com/pdfminer/pdfminer.six)：PDF 文字与版面提取。
- [pywebview](https://github.com/r0x0r/pywebview) 与 [pystray](https://github.com/moses-palmer/pystray)：桌面窗口与系统托盘。
- [FastAPI](https://github.com/fastapi/fastapi)、[Uvicorn](https://github.com/encode/uvicorn) 及其依赖：本机服务。
- [Docling](https://github.com/docling-project/docling)：可选的本地增强解析与 OCR。
- [arXiv](https://arxiv.org/) 与 [OpenAlex](https://openalex.org/)：开放论文元数据与来源服务。

第三方项目、字体和资源遵循各自许可证，相关声明保留在源码及发行包中，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。
