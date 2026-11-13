# 知乎内容搜集器

Windows 桌面工具，用于将自己有权访问的知乎回答、收藏夹和想法归档到本地，支持 Markdown、Word 和 PDF 导出。

当前版本：**0.1.7** · [更新记录](CHANGELOG.md) · [测试与已知限制](docs/TESTING.md) · [MIT 许可证](LICENSE)

## 功能

- 单条回答、答主主页、问题回答、收藏夹回答和想法采集；链接自动识别、批量队列及常用配置。
- 收藏夹先读取成员清单，再采集正文；过滤非回答条目并保留范围说明。
- 正文图片本地归档，支持 Markdown、Word、PDF，逐篇导出或合并为一本文件。
- SQLite 本地内容库，支持搜索、阅读预览、分页选择、标签、星标和已读管理。
- 离线重新导出、跳过/覆盖/生成副本、失败正文及图片重试。
- 暂停、继续、停止、断点续传及异常任务恢复。
- 显示连接和 PDF 阶段诊断；PDF 独立进程排版，同一任务重试时可复用已打印片段。

## 安装与启动

需要 **Windows 10/11 64 位、Python 3.12 或更高版本**，当前在 Python 3.13 下验证。macOS/Linux 尚未验收。

```powershell
git clone https://github.com/Will-McAvoy/zhihu-collector.git
cd zhihu-collector
```

首次安装双击 `安装并启动.bat`，完成后日常使用双击 `启动.bat`。也可手动执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m zhihu_collector
```

Playwright 固定为已验证的 1.63.0。升级该依赖时需要同时验证自定义同步等待和进程回收逻辑。

## 使用

1. 点击“登录知乎”，用自己的知乎 App 扫码并在手机确认；程序核对网页实际登录状态后保存凭据。
2. 粘贴回答、作者、问题或收藏夹链接，选择采集模式和篇数上限。
3. 选择 Markdown、Word、PDF，以及分篇或合并输出，确认范围后开始采集。
4. 在内容库中阅读、筛选或离线重新导出；在失败项中重试未完成的正文、图片及格式。
5. 如遇登录失效或网站验证，在可见浏览器中自行处理后继续。程序不会自动解决验证码或绕过权限。

链接示例中的 ID 应替换为你有权访问的目标：

```text
https://www.zhihu.com/collection/<收藏夹ID>
https://www.zhihu.com/question/<问题ID>/answer/<回答ID>
https://www.zhihu.com/people/<作者标识>
```

达到篇数上限、网站参考数量存在差额、用户中断或仍有失败时，程序会保留“部分完成/完整性未确认”等状态；这不代表整个收藏夹已完整归档。视频保留标题和观看入口，暂不下载视频文件。

## 数据位置

| 运行方式 | 数据与登录状态 | 日志 |
| --- | --- | --- |
| 源码运行 | 项目内 `data/` | 项目内 `logs/` |
| 默认目录版 EXE | `%LOCALAPPDATA%\ZhihuCollector\data\` | `%LOCALAPPDATA%\ZhihuCollector\logs\` |
| EXE 便携模式 | EXE 同级 `data/` | EXE 同级 `logs/` |

EXE 同级放置 `portable.flag` 或设置 `ZHIHU_COLLECTOR_PORTABLE=1` 可使用便携模式。源码和 EXE 的默认数据库可能不同。

临时浏览器/PDF 缓存优先使用可写的 `%LOCALAPPDATA%\ZhihuCollector\runtime-cache`，不可写时回退；可设置 `ZHIHU_COLLECTOR_RUNTIME_DIR` 指定缓存位置。该选项不会迁移数据库、登录状态或导出文件。

`data/` 包含 Cookie、数据库和个人内容，不能提交或公开分享。本仓库的 `.gitignore` 已排除这些目录。开源代码不包含任何登录状态、个人数据库、真实采集样本和本机历史日志。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m playwright install chromium
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest tests -q --tb=short
```

测试使用模拟数据和离线 PDF 排版，不需要提交 Cookie。真实联网验收需要自己的账号和目标；公开测试套件与历史私有验收的统计范围见 [测试说明](docs/TESTING.md)。

## 打包

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\packaging\build.ps1
```

目录版位于 `dist/知乎内容搜集器/`，必须保留整个目录。Chromium 不包含在 EXE 内，目标机器仍需准备 Playwright 浏览器。`packaging/installer.iss` 提供 Inno Setup 脚本。

## 限制与反馈

目前仍观察到个别浏览器初始化/退出超时、首次 EXE 启动偏慢及缓存暂时被占用的情况。程序使用有限等待、本会话进程回收和后续缓存清理，具体证据及未测场景见 [测试说明](docs/TESTING.md)。

提交 Issue 时请提供版本、复现步骤和脱敏错误信息，不要附上 Cookie、个人数据库、二维码或真实私人内容。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

本项目源码采用 MIT 许可证。第三方依赖遵循各自许可证；知乎内容的权利不随本项目代码许可转移。请只归档自己有权访问和保存的内容。
