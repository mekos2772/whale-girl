# dsh-computer-use

面向 DeepSeek Harness（DSH）的 **Windows 桌面自动化工具**：截图 + 无障碍树双模态操控鼠标键盘，标准 DSH bundle（cordis 插件 + `defineTool` + `dsh.bundle.patch`），另附零依赖 stdio MCP server。

- **10 个工具接口**：`list_apps` `get_app_state` `click` `perform_secondary_action` `set_value` `select_text` `scroll` `drag` `press_key` `type_text`
- **截图 + 无障碍树双模态**：`get_app_state` 返回 UIA 树（扁平 `element_index`，遍历序）+ 窗口截图（作为 image attachment 直接进入模型视觉上下文；DeepSeek adapter 会把工具结果图片发给 vision 模型）
- **树文本带坐标**：每个元素行末尾带 `frame=[x,y,w,h]`；附件管线降采样时 JS 会把 frame 重缩放到附件像素空间（`modelScale`）——树坐标、截图、`click/scroll/drag` 坐标三者始终同一空间，树命中的元素可直接按 frame 中心点击，无需再对照截图
- **element_index 第一公民**：动作优先按索引寻址（Invoke/Value/Text 等 UIA pattern），坐标（截图像素）只是兜底；坐标换算链为 模型附件像素 ×(1/modelScale) → 窗口相对像素 → 窗口 bounds → 屏幕像素
- **强制观察—动作—验证闭环**：每个动作尝试后快照立即失效并返回 `observationRequired: true`；下一动作若未重新 `get_app_state` 会被拒绝，避免旧索引、旧坐标、旧焦点误操作其他窗口
- **精确窗口绑定**：支持进程名/标题片段、`pid:<number>`、`hwnd:<number>` 与裸数字（PID 优先、HWND 回退）；动作始终绑定最近一次快照的 HWND，多窗口应用不会因主窗口切换而串窗
- **动效按交互时序播放**：
  - 桌面软件光标（`overlay.ps1`）：click-through 顶层透明窗（WS_EX_TRANSPARENT|NOACTIVATE，SW_SHOWNOACTIVATE，显示后还原前台），沿 cubic 路径运动；路径进度使用 `response=1.4s, damping=0.9, dt=1/240` 的固定步长 spring 推进；**箭头按 DPI 缩放（左上尖端精确锚定交互点），涟漪/雾圈画在尖端**；动作期间屏幕四周亮起蓝色呼吸光带（高眼提示）
  - **常驻会话**：overlay 跨动作存活——光标停在上一动作终点、蓝光持续呼吸，直到回合结束（`agent/status` idle）、空闲 12s、或插件卸载才淡出回收；进程死亡/断流时 EOF 兜底自动还原光标
  - 动画期 + 真实输入期 `SetSystemCursor` 全局隐藏真光标，结束后 `SPI_SETCURSORS` 还原用户光标方案、位置原样归还；进程被杀时 JS 侧 failsafe 兜底还原
  - 软件光标位置在同一 session 内连续保留，下一次移动从上一个动作终点开始；fresh session 从 `(0,0)` 初始化
  - **指针 = 纯代码矢量渲染**（30 行 contour 绘制，深灰 fill `0.38/0.36/0.35 a0.98` + 白描边 `0.90 a0.92` w1.55 圆角；tipAnchor 布局，箭头尖即目标点）——不依赖外部位图素材，无抠图残留
  - 截图内叠加（模型视角）：同一矢量指针 + 雾圈 + 镜头帧 + 动作轨迹点，坐标按截图比例缩放
- **审批**：`askBeforeActions`（**默认关**，动作直接执行）。开启后经 `ctx.approval` 逐动作询问；fail-closed——无审批通道（无 agent 上下文或审批服务未注册）时变更类动作直接拒绝
- **审计**：默认开启，元数据 JSONL 追加到 `~/.dsh-computer-use/audit/computer-use.jsonl`（方法名、app 哈希、字节量、outcome、耗时、via；**不含参数与内容**）
- **更新提醒**（`updateCheck`，默认开）：插件启动时非阻塞查询 npm registry（官方源；若 shell 配置了 `npm_config_registry` 镜像则跟随），发现新版本通过 logger 提醒——同一版本只提醒一次、同一自然日最多一次，离线/registry 错误时完全静默
- **真光标让位**：动画期与真实输入期用 `SetSystemCursor` 全局隐藏真光标（软件光标是唯一指针），结束后 `SPI_SETCURSORS` 还原用户光标方案，且真光标位置原样归还；JS 侧对进程异常死亡有还原 failsafe
- **零运行时依赖**：Node 侧只 import `@deepseek-ai/schemastery` / `@deepseek-ai/dsh-tools`；UIA 内核是单文件 PowerShell 5.1（`--serve` 常驻进程，stdin/stdout JSON 行协议，空闲 120s / 200 次请求后回收，崩溃自动重启）

## 安装

### 作为 DSH 插件（npm）

```sh
# 在 profile 目录安装（或 link 本地开发目录）
npm i @milkuovo/dsh-computer-use        # 或 dsh plugin --profile web add <path>
# 重启 dsh 后生效。设置面板/HMR 改配置：
#   $DSH_HOME/profiles/<profile>/cordis.patch.yml
```

### 源码方式

```sh
git clone https://github.com/mekos2772/dsh-computer-use
cd dsh-computer-use
dsh plugin --profile <profile> add .
```

### 独立 MCP server（任何 MCP 宿主可用）

```sh
npx @milkuovo/dsh-computer-use   # 或本地: node mcp-server.mjs
```

## MCP 模式

`dsh-computer-use-mcp`（源码 `mcp-server.mjs`，零依赖）暴露同样的 10 个工具（`element_index` 为 integer）。单次调用即用即退，元素快照持久化在 `%TEMP%\dsh-cu-mcp-session.json`，跨进程仍可按 `element_index` / `marker` 寻址；截图写入 `%TEMP%\dsh-cu-last-shot.png`。默认不启用审批门（由 MCP 宿主把关），`CU_APPROVAL=1` 可强制 fail-closed 审批；`CU_MAX_DEPTH` / `CU_MAX_NODES` 调整树捕获预算。

真实任务示例（一步步 MCP 调用）：`list_apps` → `get_app_state` → `press_key ctrl+t` → `get_app_state` → `type_text` → `get_app_state` → `press_key Return` → `get_app_state` 找元素 → `click element_index` → `get_app_state` 验证。每个动作之间都重新观察，MCP 服务也会强制执行这条规则。

MCP 的跨进程快照默认保存在临时目录；并行使用多个 MCP 客户端时，用 `CU_SESSION_FILE` 为每个客户端指定独立状态文件，`CU_SCREENSHOT_FILE` 可指定截图落盘位置。

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: computer-use
      name: '@milkuovo/dsh-computer-use'
      config:
        askBeforeActions: false   # 动作前逐次审批（默认关；开启后无审批通道会拒绝动作）
        maxDepth: 8               # UIA 树深度上限
        maxNodes: 400             # UIA 树节点上限
        includeScreenshot: true   # get_app_state 附带截图（需要 vision 模型）
        audit: true               # 元数据审计 JSONL（~/.dsh-computer-use/audit/）
        updateCheck: true         # 启动时查 npm 新版本并提醒（同一版本/同一自然日只提醒一次）
        annotate:
          grid: true              # 截图画编号十字准星网格；click({marker}) 按标记点选并吸附元素中心
          lastPoint: true         # 截图画上一次动作落点的琥珀圈（自证）
        fx:
          screenshot: false       # 调试项；桌面 overlay 不会烘焙进截图
          overlay: true           # 指针型交互前播放桌面软件光标
          trail: false            # 调试项；仅 screenshot=true 时生效
          lens: false             # 旧版/调试 3D LensSequence；普通点击不开启
          lensFrame: 28           # 调试截图中的镜头帧号 (0..44)
```

## 模型使用约定

1. 先 `list_apps` 发现应用，或直接用进程名、窗口标题片段、`pid:<number>` / `hwnd:<number>`；`list_apps` 会列出同一进程的多个可见顶层窗口
2. 每次与 app 交互前调用 `get_app_state({app})` —— 返回带编号的树（每行带 `frame=[x,y,w,h]`）+ 截图，头注声明坐标空间
3. 动作优先 `element_index`；坐标模式使用截图像素 —— 树行 `frame` 中心 `[x+w/2, y+h/2]` 就是合法点击坐标，与截图读点同一空间
4. **网格标记点选**：截图上画有编号十字准星网格（A1.. 列行标号；未被覆盖的可交互元素在中心补 `E1..`），视觉定位只需回答"目标在哪个标记上"，`click({marker: 'D6'})` 把标记映射回坐标并 UIA 吸附到所在最小可交互元素中心 —— 比直接回归像素坐标稳得多；无标记区域才用 x/y（`marker` 优先级高于 `element_index` / `x/y`）
5. **落点自证 + 强制复查**：每次动作后返回 `observationRequired: true`，必须重新调用 `get_app_state`；新截图会用琥珀色圆环标出上一次动作的实际落点，据此确认动作是否生效
6. **观察即激活**：截图是抓屏实现，`get_app_state` 默认先把目标窗口带到前台（`annotate.activate: false` 关闭），否则拍到的是遮挡物
7. `press_key` 用 xdotool 语法：`a`、`Return`、`Tab`、`super+c`(Ctrl+C)、`Up`、`Page_Up`、`F5`

## 工作原理

```
模型 ── tools (defineTool) ──> dsh-computer-use (cordis plugin)
                                     │  session：element_index → path 映射、trail
                                     ▼
                     lib/ps1.js  ── spawn powershell (lib/uia.ps1) ──> UIAutomation
                     lib/overlay.ps1 ──> 桌面软件光标动画（fx 引擎）
```

- `uia.ps1`：窗口解析（进程名/PID/HWND/标题）→ `AutomationElement.FromHandle` → 受 `maxNodes` 访问预算约束的广度遍历（包含不可见中间节点，避免 UIA 大树失控）→ 扁平树（index/path/role/name/value/patterns/settable/frame）→ System.Drawing 截窗口 PNG → fx 叠加 → base64
- `get_app_state` 成功后：截图经 `ctx.attachments.saveImages` 持久化，render 输出 `[{text: 树}, {image: 截图}]`；默认截图保持原始画面，调试时才可显式开启合成效果
- **坐标空间统一**：内核输出的树 frame 是窗口相对物理像素（= 截图捕获空间）。DSH 附件管线可能降采样截图（≤2048px + 字节上限），工具层以附件 ref 的宽高算出 `modelScale`，把树 frame 与模型点击坐标统一到**附件像素空间**；MCP 模式无降采样（从 PNG IHDR 读原生尺寸，scale=1）
- 指针型动作统一走 `withPointerMotion`：从 session 上一终点规划路径 → 等待 overlay 到位 → 启动真实动作并提交视觉反馈 → 成功后记录新终点；`press_key` / `type_text` 不触发光标
- **网格标记（`annotate.grid`，默认开）**：内核在 fx 层之上给截图画编号十字准星网格 —— 间距取可交互元素（secondaryActions 含 Invoke 或 settable、enabled、frame ≥ 20px 且在窗口内）`min(w,h)` 中位数 × 0.4（捕获像素），行列由窗口宽高除间距得出，总数上限 100（超出按 1.15 倍步进放大间距）；任何候选元素内一个标记都没有时在其中心补 `E1..` 标记。标注尺寸按 `k = min(1, displayWidth/捕获宽)` 反向放大（十字臂 5/k、线宽 1.5/k、字号 14/k，crimson + 白 halo + 白底标签），附件降采样后依然可读。标记以捕获像素存入 `session.markers`（`click({marker})` 直接 `toScreenPoint`，不过 modelScale），命中后吸附到包含该点的最小可交互元素中心，note 写明吸附结果
- **落点自证（`annotate.lastPoint`，默认开）**：`get_app_state` 把 session 上一次动作的屏幕坐标换算为窗口相对，若在窗口内则在截图上画琥珀圈（RGB 255,140,0，半径 12/k + 短十字 + 白 halo）；treeText 追加图例（Amber ring / Grid）提示模型按图索骥，`markers`/`lastPointDrawn` 仅在内核结果与 session 内部流转，不进 execute 返回值（DSH 严格校验 output schema）
- 点击反馈是灰色径向雾场 + 260ms 光标形变 + 扁平蓝色涟漪
- `fx.lens=true` 仅用于旧版 LensSequence 素材调试，不属于普通点击逻辑

## 素材说明

**指针光标为纯代码矢量渲染**，分层窗口使用逐像素 alpha 合成，因此不会出现色键抠图残边。

## 限制

- Windows 前台语义：动作自动激活目标窗口（前台应用模型）
- `select_text` 为 best-effort：TextPattern 可用时 Select，否则聚焦元素
- UIA 内核常驻（首个调用含 ~0.9s 冷启动，之后每请求 ~60ms；空闲 120s 回收）

## 测试

```sh
npm test                  # 无需预先打开应用的默认逻辑/协议测试
npm run test:integration  # 需要按用例提示打开 Calculator / 编辑器窗口
npm run test:e2e          # 自动拉起浏览器与记事本，覆盖完整工具面
```

## License

MIT
