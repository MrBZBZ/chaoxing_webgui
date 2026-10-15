# 学习通视频自动观看工具

当前目录就是可复用项目目录：`chaoxing_webgui`。

## 快速启动

网页版（推荐）：

```powershell
python .\chaoxing_webui.py --port 8756 --open-browser
```

也可以双击 `start_webui.bat`。如果 `8756` 已被已有实例占用，不要重复启动；直接打开
`http://127.0.0.1:8756/` 即可。

重新打包 exe：

```powershell
python -m pip install -r .\requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

成品位于 `release\ChaoxingWebGUI.zip`。`webui_defaults.json` 必须与 exe 放在同一目录，
以后更新 cookie 或课程链接无需重新打包。

## 项目结构

| 路径 | 用途 |
|---|---|
| `chaoxing_webui.py` | 本机网页 GUI 服务 |
| `chaoxing_autowatch.py` | CLI 与上报核心 |
| `chaoxing_webui_launcher.py` | PyInstaller 入口 |
| `chaoxing_gui.py` | Tk 桌面版备选 |
| `webui_defaults.json` | 本机配置（含登录会话，勿公开） |
| `release/` | exe 和压缩包 |
| `archive/research/` | 历史抓包、逆向、验证材料，不参与运行 |

---

以下为运行逻辑和参数说明。

纯标准库（Python 3.8+，无需 pip 安装任何依赖）。给一个 `studentstudy` 链接和一份 cookie，
即可自动列出整门课的全部视频任务点，并按服务端要求逐条上报直到 `isPassed = true`。

## 0. 图形界面（推荐）

**网页版（推荐，本机网页）：**

```bash
python chaoxing_webui.py --port 8756     # 然后浏览器打开 http://127.0.0.1:8756/
```

1. 粘贴 cookie + studentstudy 链接（可用 `webui_defaults.json` 预填）
2. **打开页面会自动刷新一次状态列表**（可在界面里关掉）
3. **开始全部未完成** → 依次做完所有未完成视频；**停止** 可随时中断（下次续做）
4. **从选中开始继续** → 勾选某个（或几个）视频后点它：
   先重跑勾选的视频（**即使显示“已完成”也会重跑**），再继续把它后面的未完成视频全部做完
5. **重跑时起始方式**（下拉）：
   - `续看`（默认）：从服务端记录的断点继续
   - `从头`：从 0 开始重刷（对应命令行 `--restart`）
6. 可设：上报间隔、速率（1.0 正常 / 2.0 双倍）、限时分钟；有进度条与 `进度 i / n`

> 状态扫描是 **8 并发**的，全课程通常 ~5 秒完成；并且带去重守卫，同一时间重复触发会自动跳过（手动点“刷新状态”仍是强制刷新）。

**桌面版（备选）：** 双击 `chaoxing_gui.py`。注意：若由后台/容器进程启动，窗口可能落在不可见桌面上，请自己双击运行。

两个界面都只调用下面的命令行逻辑，完全等价。

## 1. 你只需要提供两样东西

| 输入 | 从哪来 |
|---|---|
| **cookie** | 浏览器登录学习通后，在 Reqable 里任取一条 `mooc1.chaoxing.com` 请求，把请求头里的 `Cookie:` 整段复制，存成 `cookie.txt`（一行） |
| **studentstudy 链接** | 浏览器里打开课程后地址栏那条：`https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=...&courseId=...&clazzid=...&cpi=...` |

其余全自动：节点列表、`objectId / jobid / attDuration / attDurationEnc / otherInfo`、`dtoken`、签名 `enc`、时间戳。

> 如果只给 `--course-id/--clazz-id/--cpi` 也可以，其它参数同样会自动补齐（`userid` 取 cookie 的 `_uid`，`fid` 取 cookie 的 `fid`）。

## 2. 常用命令

```bash
# 只读体检：列出所有节点、视频时长、哪些已完成（不发任何上报，最安全）
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<studentstudy链接>" --probe

# 不实发，只打印将要发送的那条请求 URL
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<链接>" --dry-run --nodes 1262759012

# 正式跑：全部未完成视频，按 1 倍速（墙钟 ≈ 视频时长）自动看
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<链接>"

# 只做指定节点
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<链接>" --nodes 1262759012,1262759013

# 限时 60 分钟，做完/超时即停；下次重跑自动从服务端记录的位置续做
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<链接>" --max-minutes 60

# 页面允许 2 倍速时的实验参数（每 62 秒推进 124 秒视频）
python chaoxing_autowatch.py --cookie-file cookie.txt --from-url "<链接>" --rate 2.0
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--cookie` / `--cookie-file` / `CX_COOKIE` | — | 三选一 |
| `--from-url` | — | studentstudy 链接，自动解析 courseId/clazzid/cpi/chapterId |
| `--nodes` | 全部 | 只处理这些节点，逗号分隔 |
| `--force-nodes` | 关 | `--nodes` 里的节点即使已完成也强制重跑（界面的“从选中开始继续”用的就是它） |
| `--interval` | 62 | 上报间隔（秒）。**不要低于 60**，服务端按真实间隔结算 |
| `--rate` | 1.0 | 每秒墙钟推进多少秒视频。1.0 = 正常速度；**2.0 = 2 倍速**；0.5 = 半速 |
| `--jitter` | 2 | 间隔随机抖动（秒） |
| `--final-retries` | 1 | 到片尾后的额外确认次数 |
| `--max-minutes` | 0（不限） | 总运行时长上限 |
| `--max-nodes` | 0（不限） | 最多完成几个视频 |
| `--probe` / `--dry-run` / `--verbose` | — | 只读 / 只打印 / 调试输出 |

## 3. 工作原理（均已实测验证）

1. **签名**（本地算，已 3/3 命中真实抓包）：
   ```
   enc = md5("[clazzId][userid][jobid][objectId][playingTime*1000][d_yHJ!$pdA~5][duration*1000][clipTime]")
   ```
   `clipTime = "0_<duration>"`（`startTime_endTime`，本课程都是从 0 开始）。
2. **dtoken**：`GET /ananas/status/<objectId>?fid=<fid>`，**必须带视频模块 `Referer`**，否则 403。
3. **上报**：`GET /mooc-ans/multimedia/log/a/<cpi>/<dtoken>?...&enc=<签名>`。
   `isdrag`：`play=3`（起始）、`playing=0`（心跳）、`ended=4`（片尾）。
4. **判定**：只有响应 `{"isPassed":true}` 才算完成；服务端按**真实时间间隔**累计有效观看时长。
   - 单次伪造片尾、无间隔连发、回填历史时间戳 —— 实测**全部不通过**；
   - 按真实节奏上报 —— 实测通过（本课程 1.2 节由 `unfinished 2→1` 且 `isPassed` 变 `true`）。

## 4. 已知限制

- **时间不可压缩**：本工具能省掉“人得盯着”，但省不掉服务端要的真实时长。理论上限是页面允许的倍速（本课 `doublespeed=1` → 2x），是否被接受取决于服务端是否允许 2:1 报量比，尚未实测。
- 只覆盖**视频**任务点。同节点里的**测验/作业**（如 1.2 节 436s 处的判断题）是独立链路，本工具不处理。
- cookie 过期会直接失败（日志接口 403 / status 403），重新复制一份即可。
- 人脸采集字段（`videoFaceCaptureEnc`）本课程实测不影响 `isPassed`。

## 5. 配套文件

| 文件 | 说明 |
|---|---|
| `chaoxing_autowatch.py` | CLI 与上报核心 |
| `archive/research/forge_findings.md` | 逆向与实测证据 |
| `archive/research/verify_enc.py` | 签名算法回归验证 |
| `archive/research/poc_forge_log.py` | 单条请求排查工具 |
| `archive/research/` | 历史实验和抓包材料，默认不参与运行 |

## 6. 出网注意

如果本机设了代理环境变量且指向不可用代理，请先清掉：
```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
```
（用 Reqable 抓包时则改回指向 Reqable 的代理端口。）
