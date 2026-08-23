# Mimi 桌宠运行程序（PySide6 v1）

当前状态：交互/DSH 联动/桌宠 Agent 全链路可用（2026-08-23）。**接手先读 `../docs/MIMI_PROJECT_HANDOFF.md`**（环境、启动命令、交互全景、DSH 协议、影子会话与权限）；素材方向再读 `../docs/MIMI_TEXT_MODEL_HANDOFF.md`。注意：本机 PySide6 6.11.1 装在**系统 Python 3.11**（`AppData\Local\Programs\Python\Python311`），项目 `.venv` 里没有——直接用系统解释器启动，命令见交接文档 §2。

新开发者或纯文本模型接手前必须先读 `../docs/MIMI_PROJECT_HANDOFF.md` 与 `../docs/MIMI_TEXT_MODEL_HANDOFF.md`，再读 `MIMI_ACTION_CATALOG.md`、`MIMI_LIVE2D_FOUNDATION.md`、`MIMI_PROJECT_FRAMEWORK.md`。本文只描述程序本身。

## 安装与启动

需要 Python 3.11+。PySide6 是可选依赖，首次运行 GUI 前安装（国内网络建议用镜像源）：

```powershell
# 在项目根目录执行
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "PySide6>=6.7,<7"

# 启动桌宠（透明、无边框、置顶窗口）
$env:PYTHONPATH='mimi_app\src'
.\.venv\Scripts\python.exe -m mimi_pet

# 或双击根目录 start_mimi.bat
```

不装 Qt 也能做全部领域层校验与测试：

```powershell
$env:PYTHONPATH='mimi_app/src'
python -m mimi_pet --dry-run
python -m unittest discover -s mimi_app/tests -v
```

## 操作方式

- **左键点击角色可见区域**：按头部、脸部、腹部分区播放摸头、戳脸或怕痒；双击播放击掌。
- **左键按住或快速移动**：拾取拖拽。按下瞬间记录鼠标与角色根节点的锚点偏移，移动时保持该偏移，角色不会吸到鼠标中心。拖动期间右键菜单不可用。
- **拖入文件或文本**：成功 Drop 后播放接取动作。
- **拖拽中移动**：`DragHybridController` 输出方向、速度档位、身体倾角、头发/裙摆滞后和根节点弹簧偏移，全部显示在调试叠层。
- **释放**：若脚底根节点在可用工作区底边之上 → Falling，按释放瞬间平滑速度施加重力下落；触地校正根节点 → Landing，完整播放 `land_recover_v4_12`，结束回 Idle。若释放时脚已着地 → 直接 Landing。
- **右键菜单**（只提供功能入口，**不提供动作点播**——所有动作由交互、DSH 事件或场景触发器驱动）：
  - 首层：`投喂圆面包`（是否接受由饱食度决定，触发投喂/拒食动作）；
  - `移动与休息`：普通或慢速向左/向右走、停止移动、坐下/站起来、睡觉/叫醒；
  - `尺寸与位置`：三档尺寸与缩放滑块、停靠屏幕底部/自由放置、回到屏幕底部、调试信息开关；
  - `Harness`：打开消息面板、摘要模型选择、回答 DSH 待答问题；
  - `退出`：停止全部 QTimer 并关闭窗口，不留后台进程。

## 代码结构

```text
mimi_app/src/mimi_pet/
├─ action_library.py    # manifest 白名单加载（唯一素材入口）
├─ frame_player.py      # 逐帧独立 duration 播放
├─ state_machine.py     # Idle/Performing/Dragging/Falling/Landing/Sleeping/Closed
├─ live_controller.py   # 呼吸、鼠标注视、表情参数
├─ drag_controller.py   # 速度低通、方向、慢快滞回、倾角与滞后
├─ scheduler.py         # 随机趣味动作冷却与概率
├─ engine.py            # 领域协调器：状态机+播放器+Live+拖拽+下落物理+眨眼
├─ collision.py         # 多显示器工作区与根节点碰撞（纯 Python）
├─ rig_model.py         # live_rig/model.json 解析（纯 Python）
├─ image_cache.py       # QPixmap 按路径缓存，同一 PNG 只解码一次
├─ renderer.py          # RenderSnapshot（纯数据）+ Qt 合成器（完整帧/Rig）
├─ debug_overlay.py     # 可开关调试叠层
├─ qt_window.py         # 透明置顶窗口、Alpha 命中、鼠标事件、右键菜单
├─ qt_app.py            # QApplication、60Hz 定时器、调度定时器、总装配
└─ main.py              # --dry-run 校验；默认启动 GUI
```

## 当前限制（第一版如实说明）

- **拖拽姿势已升级到 v13 密集力度点**：左右各 11 张直接图生图绘制的姿势（`drag_left/right_dense_v13_11`），从轻微受力到高速拖尾依次为 `t00..t10`。运行时按平滑速度选点，每次更新最多前进/后退一格，禁止从低速图硬跳高速图；所有帧只经过统一等比缩放和头顶抓点平移，不做单图旋转拉伸。程序额外微调限制为 ±0.35°。
- **停止时无回弹帧**：速度下降时沿密集力度点逐格回到 `t00`，hold（<90 px/s）后冻结在 `t00`；不播放旧 v6 反向放下/回弹序列。
- **边界框**：拖拽时窗口约束在虚拟桌面联合矩形内（可跨屏拖动、不会被扔出屏幕）；下落时水平方向同样夹住（不会飞出屏幕）。
- 垂直拖拽素材未完成：up/down 方向保持最近水平姿势组（同档位），**不旋转图片冒充**。
- 无语音系统：talk 整脸通过调试菜单手动触发。
- Rig 是分层 PNG 参数 Rig（Live-like），不是 Cubism 网格模型，无 IK/物理骨骼。
- `skirt_hem`（实验性裙摆前层）**不渲染**：它在 `fit_report.json` 中没有校准、官方预览脚本也不使用，未校准合成会在身体前面多出一条错位裙子。参数（skirt_sway/skirt_drag_lag）保留，等素材完成校准后再接入。
- **固定比例注册**：Rig 与完整帧共用同一比例与脚底根节点，校准常数在 `mimi_app/config/app.json` 的 `rig` 段（由 `tools/calibrate_rig_root.py` 一次性计算，含 X/Y 方向）。此外 manifest 可为尺寸不一致的旧素材记录**一次性注册缩放**。v13 拖拽帧使用整套公共倍率并对齐同一头顶抓点，严禁按每帧 alpha 外框重新 fit；坐姿、蹲姿等姿势帧在画布内自然变化，不参与对齐。
- 新 62 帧 `walk_left/right` 已接入；世界位移由根节点控制。慢走素材退出精简库，跑步未接入，跳跃已退役。
- 随机完整帧趣味池为空；自主完整帧行为只保留空闲后的左右走路。
- 表情优先级：完整帧自带表情（喝茶/进食/遮脸/喷嚏等动作锁定整脸）> talk > blink > happy > neutral；Falling/Landing 关闭呼吸与注视；Performing 注视权重 20%。
- **鼠标注视采用“独立虹膜 + 颈部微转”**：旧 `eyes_open_v1` 仍是比例错误的研究候选，禁止接入；正式 `irises_original_v2.png` 从批准 neutral 母版提取。neutral 时虹膜在眼裂裁剪内最多移动母版坐标 ±8/±4px，头部平移仍衰减到约 1/3，并保留 ±4° 绕颈侧 pivot 的旋转和后发反向滞后。静止中心直接显示原版 neutral；blink/talk/happy 绕过虹膜合成，使用审核过的整脸帧。

## 验证工具（tools/）

- `tools/calibrate_rig_root.py`：计算 Rig→完整帧固定注册常数（写入 config）。
- `tools/smoke_gui.py` + `tools/verify_smoke.py`：offscreen 全状态链渲染检查（idle→表演→拖拽→下落→落地→idle）并输出 ASCII 预览。
- `tools/e2e_window_test.py`：QTest 模拟鼠标的窗口级端到端测试。
- 运行示例：`$env:QT_QPA_PLATFORM='offscreen'; python tools\smoke_gui.py`

## 下一步工作

1. 播放验收 v13 左右 11 点拖拽；若仍有局部身份漂移，只重画对应相邻点，不允许退回 v11 三档或 v12 单图旋转方案。垂直拖拽继续沿用最近水平姿势，等待独立素材。
2. 语音系统接入后把 talk 从调试菜单升级为自动触发，并实现说话节拍驱动嘴型。
3. 逐项桌面验收新接入的 38 套 H3 动作与状态切换；发现素材问题时退回制作区，不做逐帧自动缩放。
4. 跑步、爬墙、顶部悬挂和边缘隐藏仍需独立素材与交互验收。
