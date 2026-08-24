# Mimi 运行素材库 v1

这是主程序唯一应读取的素材入口。原始生成文件仍保留在 `rebuild_20260812`，本目录只做非破坏式整理。

如果接手者是无法查看图片和历史聊天的纯文本模型，请先完整阅读 `docs/MIMI_TEXT_MODEL_HANDOFF.md`。它包含角色固定外观、17 套正式动作逐套视觉描述、所有失败结论、待补动作分相规格和程序现状。之后再读 `docs/MIMI_ACTION_CATALOG.md`、`docs/MIMI_LIVE2D_FOUNDATION.md` 与 `docs/MIMI_PROJECT_FRAMEWORK.md`。

## 目录规则

- `manifest.json`：运行时白名单；只有 `usable_actions` 中 `enabled=true` 的动作可以播放。
- `usable/actions`：已经验收、不需要修改的完整帧动作。
- `usable/live_rig`：可接入的分层 PNG 与参数配置。
- `candidates/needs_qa`：帧数基本足够但动作语义或观感尚未验收。
- `candidates/needs_more_frames`：主姿势可参考，但缺真正的中间动作相位。
- `candidates/hybrid_drag`：拖拽慢/快姿势与源表，必须配合运行时参数。
- `candidates/redesign`：现有动作被用户退回，只能作为反例，不能继续补帧。
- `candidates/rejected`：明显错误的生成结果。
- `candidates/index.json`：所有备选、历史版和退役素材的索引。

执行 `python scripts/build_mimi_asset_library.py` 可重新构建。脚本不会移动或修改原始生成目录。

当前正式完整帧动作共 17 套：16 套趣味动作和 `land_recover_v4_12`。呼吸、眨眼、微笑、说话脸、鼠标追踪走 Live 层；拖拽走混合层。其余素材均不进入正式运行时。
