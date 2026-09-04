# Changelog

## 0.6.1 — 2026-09-04

- 适配 DeepSeek Harness `0.1.2-rc.1`：使用 `authenticatedUrl` 完成 Python 端一次性认证交换，认证 Cookie 仅保存在内存中。
- 统一新版 Remote RPC：使用 slash endpoint 与 `payload.args` envelope，并修正 session/model 相关 wire 参数。
- 接入 `remote.mux` 的 `$events`、`session/control`、`session/follow` 流；等待 `ready` 后报告连接，并支持 follow 重连、baseline 回放和序列去重。
- 修正工作模式 session 目标选择与 follow 同步，避免固定项目、自动选择和异步 poll 之间串线。
- 新增桌宠模型目录/选择与 reasoning effort 菜单，和本地回复摘要模型保持隔离。
- 保持内置 Computer Use `0.2.0`；已在 DSH `0.1.2-rc.1` + Mimi 实机验证观察、点击和操作后观察闭环。
- 258 项 Python 测试通过；Remote mux、窗口 E2E、GUI smoke 和 npm 包运行时检查通过。

## 0.6.0 — 2026-09-03

- 内置 `@milkuovo/dsh-computer-use@0.2.0`，Computer Use 直接随 Mimi 安装，提供截图、UI Automation 树、点击、输入、滚动、拖动、按键与操作后验证。
- 强制观察→动作→验证闭环，支持多窗口绑定、编号网格、落点自证、审计和可选逐动作审批。
- 工作模式与桌宠模式进一步区分：工作模式按 DSH 活动显示输入栏，桌宠模式悬停头部后显示聊天输入，并显示模式徽标。
- 新增持久化好感度：桌宠模式会分析正向聊天内容，默认每次 `+1`，90 秒冷却、每日最多 3 点、同文指纹去重；工作模式、工具调用、任务结果和问题卡回答不改变好感度。
- Agent 内置人格加入安全转译的 DeepSeek 鲸鱼娘社区设定，并按关系阶段调整语气；保留破坏性操作确认、隐私边界和外部影响确认规则。
- Windows 中文输入法继续使用稳定的 `QLineEdit` 路径；气泡、活动胶囊、DSH session 隔离和跨线程事件回投保持不变。
- 218 项应用测试通过，重新构建 `mimi-desktop-pet-0.6.0.tgz` 发布包。

## 0.5.0 — 2026-08-30

- 表情系统根治：v5 Live 从"整图替换"改为"分区补丁叠加"，眨眼不再吃掉微笑、虹膜追踪在说话/微笑下不跳变，任何表情状态可自由组合。
- 思维链内容不再被总结；摘要功能改为压缩 assistant 的正常输出（长回复弹"总结：…"气泡），短回复不打扰。
- 新增 npm 插件更新提醒：连接 DSH 后自动比对 registry，发现新版用气泡和右键菜单提示（适配 DSH profiles 安装点）。
- 修正 DSH 插件版本解析：deepseek-official 内置端点、`refs` 嵌套凭据、%APPDATA% 动态寻路。
- 179 项应用测试通过；重建 0.5.0 发布包（素材与代码均为最新）。

## 0.4.0 — 2026-08-24

- 适配并实机验证 DeepSeek Harness `0.1.1-rc.2`。
- 运行库从 57 套历史动作精简为 23 套正式核心动作，PNG 运行副本替换为透明 WebP Q95。
- 加入 Live Rig v5：脚底锚定呼吸、眨眼、微笑、说话和独立虹膜追踪。
- 重做 DSH 交互：头顶气泡、Windows 中文输入、项目联动与独立 Mimi 管家 Agent。
- 加入五分区触摸、组合互动、拖放投喂、久坐久睡场景链与单实例保护。
- 发布包约 47.9 MiB；不包含旧 Rig、候选源图、本机会话、测试或缓存。
- 162 项应用测试通过，并完成 rc.2 HTTP RPC、WebSocket 与插件生命周期烟测。
- 新增 34 秒 1080p 宣传视频与海报。

## 0.3.0 — 2026-08-19

- 加入 DSH 活动胶囊、任务摘要、多项目切换和完整桌宠本体打包。
