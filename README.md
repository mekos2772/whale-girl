# 鲸鱼娘 Mimi — DeepSeek Harness 桌面宠物插件

`mimi-desktop-pet`：DSH 启动时桌宠自动陪伴，实时显示思维链总结 / 工具调用 / 轮次进度，支持拖拽互动与无级缩放。

- **npm**：https://www.npmjs.com/package/mimi-desktop-pet

## 安装

```bash
# 先确认你平时启动 DSH 用的 profile（dsh web 启动的就是 web profile）
dsh plugin --profile web add mimi-desktop-pet     # 用 dsh web 启动 → 装 web
dsh plugin --profile desktop add mimi-desktop-pet # 用 desktop profile 启动 → 装 desktop
```

装到**与你启动命令对应的 profile**，然后**重启 DSH**（Ctrl+C 后重新 `dsh web`）：
桌宠随 DSH 自动出现，DSH 设置面板出现 `mimiPet` 命名空间（enabled / petDir / python / scale）。

> **v0.2.0 起自带完整桌宠本体与全部素材**（`pet/`，约 99MB）——安装即开箱即用，无需自备资源。
> 若本机已有桌宠项目（`~/Desktop/桌宠`），插件会优先使用它（可在设置里自定义 `petDir`）。

## 功能

- DSH 启动时自动拉起桌宠（Python/PySide6 渲染），DSH 退出时自动回收
- 桌宠头顶气泡实时显示：思维链摘要、工具调用（`Pwsh`/`Edit`/`Search`…，DSH 官方图标风格）、进度
- 点击气泡展开完整消息面板（可发送消息、回答问题）
- 状态动作：DSH 思考时托腮、调工具时操作面板、完成时叉腰
- 交互：单击小表情 / 双击大反应 / 悬停害羞 / 甩出捂脸 / 滚轮无级缩放

## 配置（DSH 设置 → `mimiPet` 命名空间）

| 字段 | 说明 |
|---|---|
| `enabled` | 是否随 DSH 启动桌宠（默认 true） |
| `petDir` | 桌宠项目根目录（含 `mimi_app/src`）；留空自动探测 `~/Desktop/桌宠` 或环境变量 `MIMI_PET_DIR` |
| `python` | `pythonw.exe` 完整路径；留空自动探测 |
| `scale` | 缩放百分比（1-200） |

## 环境要求

- 桌宠本体（`mimi_app`）：Python 3.11+、PySide6 6.x
- 插件：Node 18+（零运行时依赖）

## 开发

```powershell
# 本地目录安装（写 profile 层 patch，携带本机路径）
node install.mjs --profile desktop

# 发布新版本
npm version patch
npm publish
```

## 许可

MIT
