# Mimi 桌宠项目交接文档（应用运行时与 DSH 联动）

更新日期：**2026-09-04**。本文件是应用与 DSH 联动方向的**第一阅读入口**；素材/视觉身份方向的第一入口仍是 `MIMI_TEXT_MODEL_HANDOFF.md`。两者都读后再看 `MIMI_ACTION_CATALOG.md`（素材清单）与 `工作记录.md`（逐日变更，#28-#36 为 08-23 连续工作）。

---

## 1. 项目一句话现状

PySide6 透明桌宠（鲸鱼娘小咪），23 套完整帧动作 + Live 层（呼吸/眨眼/微笑/说话/虹膜视线追踪）+ 分区触摸交互 + 久坐久睡场景；通过本地 DSH（DeepSeek Harness, http://127.0.0.1:3080）提供两种模式：**DSH 联动**（镜像用户工作会话）与 **桌宠 Agent**（归档影子会话「Mimi 管家」，danger-full-access，可直接操作电脑）。UI 已极简定稿：**一个白色输入框 + 全部信息走头顶白色气泡**。

## 2. 环境与启动（重要，照抄可跑）

- **Python**：系统 Python 3.11（`C:\Users\14408\AppData\Local\Programs\Python\Python311\python.exe`），已装 PySide6 6.11.1 与 websocket-client 1.9.0。**项目 `.venv` 里没有 PySide6**，别用错解释器。
- 启动（手动）：

```powershell
$env:PYTHONPATH='C:\Users\14408\Desktop\桌宠\mimi_app\src'
$env:MIMI_ASSET_ROOT='C:\Users\14408\Desktop\桌宠'
C:\Users\14408\AppData\Local\Programs\Python\Python311\pythonw.exe -m mimi_pet.main
```
  （工作目录 `mimi_app/`；pythonw 静默，python.exe 可看报错）
- **DSH**：本机已升级到 `0.1.2-rc.1`，Node.js 24.15.0。已实机验证新版认证、基础 Remote RPC、`/api/remote.mux`、bundle 装配、插件自动拉起和 Computer Use 观察/操作/验证闭环。DSH 是 developer preview，升级前仍应保留回退版本。
- **DSH 启动会经插件自动拉起桌宠**（`dsh-plugin-mimi/lib/index.js`，settings 命名空间 `mimiPet`）。桌宠有 **QLocalServer 单实例保护**（`mimi-pet-singleton`）：插件自启与手动启动撞车时后来者静默退出。
- 测试：`cd mimi_app && python -m pytest tests/ -q` → 当前 **258 项通过，4 项 opt-in/live gate 跳过**（offscreen 平台，无 Qt 也能跑大部分领域层）。
- 诊断日志：`%TEMP%\mimi-pet-debug.log`（WS 连接/错误、follow 目标、事件接纳判定、气泡入层逐帧）——排 DSH 联动问题先看它。
- 杀进程注意：Git Bash 里 `taskkill /PID` 会被 MSYS 转义弄坏，用 `powershell Stop-Process -Id <pid> -Force`。

## 3. 模块地图（mimi_app/src/mimi_pet/）

| 模块 | 职责 | 备注 |
|---|---|---|
| `engine.py` | 领域协调器（无 Qt 依赖）：状态机、动作播放、场景链、触摸/组合/投喂、拖拽物理 | `touch_region()` 分区映射；好感度成功事件由独立 tracker 记录 |
| `affection.py` | 本机好感度、阶段、事件冷却/每日上限和原子状态保存 | 默认 `%APPDATA%\\MimiDesktopPet\\state.json`，可用 `MIMI_STATE_PATH` 覆盖 |
| `qt_window.py` | 唯一窗口：alpha 命中、鼠标/拖放/右键菜单 | 菜单**无动作播放**（用户原则：动作必须由交互触发）；Harness 菜单含模式单选/项目子菜单/摘要模型 |
| `qt_app.py` | 装配：QApplication、60Hz tick、单实例、气泡层接线 | `on_bubble_requested` 里有 dbg 打点（排查气泡链用） |
| `dsh_bridge.py` | 纯 Python DSH 客户端：HTTP RPC 信封 + events.mux WS 线程 | `dbg()` 已抽到 `debug_log.py` |
| `dsh_integration.py` | 事件→宠物反应的适配层：气泡发射、思考/工具动画、问题/批准、模式切换、影子会话管理 | 人格 v2、权限舞蹈都在这 |
| `dsh_panel.py` | **白色输入框**（250 宽）+ 待答问题卡 | 一切信息 sink 均为 no-op（信息只走气泡）；IME 行为见 §6 |
| `bubble_layer.py` | 头顶白色气泡栈（最多 3，6s 消散，悬停暂停，点击→聚焦输入框） | question 橙边 / summary 青边 |
| `renderer.py` / `rig_model.py` | v5 扁平 rig：character_master 整图 + 虹膜 `_tracked_pixmap` 合成 | 母版即 2× 动作帧，无配准变换 |
| `config/app.json` | 全部阈值 | `interaction` 段新增 sit/sleep/combo 参数 |

## 4. 交互全景（2026-08-23 定稿版）

**分区触摸**（`engine.touch_region`，按母版 alpha 包络实测：角色占窗口 y 0.40–1.0）：

| 部位 | 命中区 | 反应 |
|---|---|---|
| 头顶/发侧 | y<0.52（或 0.52–0.62 两侧） | `head_pat` |
| 脸颊 | y 0.52–0.62 中央 | `cheek_poke` |
| 肚子 | y 0.62–0.88 中央 | `belly_ticklish` |
| 手 | 两侧 x<0.28 / ≥0.72 | 单击 `high_five` |
| 脚 | y≥0.88 | 哼一声「别挠我脚啦～」向另一侧小步走开 |

**组合/连击**：摸头后 3 秒内击掌 → `celebrate`＋「最喜欢了你！」；同部位 0.8s 内连摸 3 次 → 升格欢喜气泡（分部位文案）。双击＝全身击掌；休息中双击先起身。
**拖拽**：v13 混合姿势 + 释放抛掷落地 `land_recover_v4_12`。
**拖放**：图片文件＝喂面包（饱腹拒绝 `feed_refuse`）；其他文件/文本＝`file_drop_receive`。
**久置场景链**（`scenario_check`）：静置 15s 偶发散步 → 60s `sit_down`→`sit_idle` 常驻 → 坐满 300s 转入 `sleep_lie_down`→`sleep_loop`。醒/立全由交互/DSH 驱动（触摸唤醒、坐姿触摸直接弹起回应、`interrupt_rest()` 供 DSH 活动调用）。阈值在 config `interaction` 段。
**欢迎回来**：指针离开 ≥30s 回来 → `wave`（90s 冷却）。
**好感度**：独立于饱腹度的 `0..100` 本机关系值，默认 50；成功摸头/击掌/投喂/双击/文件接收/欢迎回来会按冷却小幅增加，桌宠 Agent 输入框发送的正向聊天也会增加（每日 3 点、90 秒冷却、同文指纹去重）。Agent 的 assistant 回复、工具调用和问题卡回答不单独计分。**工作模式（`link`）的 DSH 工作、工具调用、完成/失败、问题回答、断线和模式切换均不改变好感度**。状态保存到 `%APPDATA%\\MimiDesktopPet\\state.json`，坏文件隔离后回退默认；详见 `MIMI_AFFECTION_DESIGN.md`。
**DSH 驱动**：思考 `harness_task_thinking` / 工具 `harness_tool_working`（去重工具气泡：换工具或 30s 重复才弹，8s 全局下限）/ 提问·等待批准 `listen`（橙边气泡+问题卡）/ 回答后 `nod` / 完成 `celebrate` / 失败 `fun_facepalm`。

## 5. DSH 联动协议知识（挖包+实测得出，别再踩）

- **HTTP Remote RPC**：`POST http://127.0.0.1:3080/api/<namespace>/<method>`，信封 `{"type":"client-request","rpcId":"...","method":"namespace/method","payload":{"args":{...}}}`。`session/list` 使用 `_request`；`session/create`、`session/rename`、`session/cancel`、`session/prompt` 使用 `request`；`settings/mutate` 使用同一 `args` 中的 `ns`、`ops`、`expectedRevision`。
- **session/prompt 正确载荷**（旧 dotted method 或 `{text}` 会 bad-request）：
  `{"sessionId":..,"mode":"queue"|"steer","content":[{"type":"text","text":..}],"clientTimeZone":"Asia/Shanghai"}` 放在 `payload.args.request`。
- **Remote mux WebSocket**（`ws://127.0.0.1:3080/api/remote.mux`）：统一承载 `$events`、`session/control` 和 `session/follow` 三类逻辑流；必须收到 `$events` 的 `ready` 后才报告连接。帧为 `server-request`，覆盖 session/event（assistant/chunk 流式、assistant/message、user/message、tool/call、tool/result、turn/start|end…）、session/projection、session/jobs、question/requested、approval/requested。
- **Remote mux 连接策略**：握手后将 `recv` 超时设为短轮询，以便处理动态 follow 订阅；`$events` 必须先收到 `ready` 才报告连接，events/control 流异常会触发重连，follow 流支持 baseline 回放和 seq 去重。
- **权限档位**（read-only / workspace-write / danger-full-access，approval ask/never）：**改不了已有会话**——网页端走 WS remote `commands/execute({agentId, line:"/permission <preset>"})`，HTTP 面无此方法（直接把 `/permission` 当 prompt 发只会被当聊天）。可行路径：`settings.mutate`（ns `permission`，op set `defaultPreset`）改默认 → **新建**会话继承 → 立即还原默认。桌宠的"权限舞蹈"已固化在 `ensure_agent_session`（还原失败不阻断、有测试）。
- **影子会话**：归档（web UI 隐藏）+ 更名「Mimi 管家」+ 人格注入；id 与 `persona_version` 持久化在 `mimi_app/config/agent_session.json`，人格升级自动对旧会话重种一次。当前线上：`session-b62a5b94-…`，danger-full-access 已验证。
- **人格 v2 要点**（`AGENT_PERSONA_PROMPT`）：简短口语中文；可查时间/电量/磁盘/网络/天气、Start-Process 开程序网页、文件整理、系统操作、写 PowerShell；破坏性操作先确认、优先可逆、防乱码 chcp 65001；只服务主人。

## 6. UI 形态与已知敏感行为

- 白色输入框按模式与状态显隐：**工作模式**（原 `link`）只在 DSH 工作/思考/调用工具/等待回答/刚完成时出现，空闲或断开自动隐藏；**桌宠模式**（原 `agent`）默认安静，鼠标悬停 Mimi 头部时才出现聊天输入。输入框左侧始终标出当前模式；点击气泡或右键菜单仍可主动聚焦输入，断开时不会强制打开。打字期间窗口跟随冻结（IME 组合不被打断）。**QLineEdit 不是 QPlainTextEdit**（后者 Windows 拼音 IME 坏）。
- 气泡信息去重规则（#36）：回复只消费一次；纯聊天轮不弹总结；总结与回复互含判定为重复跳过。
- 截图排障：GDI CopyFromScreen 抓不到分层置顶窗；用 `PrintWindow(PW_RENDERFULLCONTENT)`（temp 里有现成脚本模式）。注意**外部枚举有时看不到桌宠的子窗口**（input 框/气泡层 HWND 枚举盲区，原因未深究）——以 dbg 日志的 `visible=True` + 用户实操为准，别只信截图脚本。

## 7. 待办与遗留

1. **0.4.0 已发布（2026-08-24）**：npm `mimi-desktop-pet@0.4.0` 已成为 `latest`；GitHub `master` 与 `v0.4.0` 均指向提交 `262412c`。本地归档包为 `mimi-desktop-pet-0.4.0.tgz`，宣传片位于 `release/mimi-promo-0.4.0.mp4`。
2. 外部窗口枚举盲区（§6）未根治，仅记录。
3. 纯聊天后的「任务完成～」气泡略冗余，待用户拍板去留。
4. 可选增强曾列出：影子会话 cwd 指定记忆目录、按会话选模型（session.models/selectModel 已有封装）、恢复归档方法（workspace 无 unarchive RPC，需另找）。
5. `MIMI_ACTION_TRIGGERS.md`（08-21 版）的触发表已被本文 §4 取代，仅作历史参考。

## 8. 变更速查（工作记录 #28-#36）

#28-30 v5 适配与 Live 注视幅度 / #31 气泡层 / #32 桌宠 Agent 影子会话 / #33 面板极简（白输入框+全气泡）+ WS 超时根因 + 单实例 / #34 分区触摸+组合+久坐久睡场景 / #35 电脑管家 full-access+人格 v2 / #36 信息流去重。细节见 `工作记录.md`。
