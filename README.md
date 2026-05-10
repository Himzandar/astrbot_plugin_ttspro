# astrbot_plugin_ttspro

从 astrbot_plugin_outputpro 中拆分出的独立文转语音插件。

## 功能

- 保留原 TTS 步骤的触发条件：
  - 仅在发送前处理
  - 默认仅处理 LLM 回复
  - 仅处理单段纯文本消息
  - 文本长度小于阈值时才尝试转换
  - 按概率触发
- 支持按用户 ID 强制发送语音
- 支持按输入关键词在 LLM 回复时强制发送语音
- 支持模型 TTS 与 QQ 声聊中转两种模式
- 支持跨平台语音中转
  - QQ 平台保留原始 URL
  - Telegram 优先转为 ogg
  - 其他平台优先转为 wav
- 保留 silk/amr 音频兼容处理

## 配置

- only_llm_result：是否仅作用于 LLM 回复，默认 true
- tts_provider_id：可选 TTS 提供方 ID，填写后优先使用模型 TTS
- group_id：QQ 声聊中转群号，走中转模式时必填
- character_id：QQ AI 语音角色
- threshold：文本长度阈值，默认 50
- prob：触发概率，默认 0.1
- force_tts_user_ids：强制发送语音的用户 ID 列表，支持逗号或换行分隔
- llm_keyword_triggers：用户输入中命中后强制触发语音的关键词列表，仅对 LLM 回复生效，支持逗号或换行分隔

## 行为说明

处理顺序与原插件中的 tts 步骤保持一致：

1. 若配置了 tts_provider_id，则优先使用 TTS Provider 生成语音。
2. 否则如果当前事件来自 aiocqhttp，则直接调用 QQ AI 语音接口。
3. 否则尝试寻找已连接的 aiocqhttp bot 进行跨平台 QQ 声聊中转。
4. 若跨平台中转成功，会根据目标平台自动转码后发送语音。

额外强制触发规则：

1. 若当前发送者 ID 命中 force_tts_user_ids，则跳过概率判定，直接发送语音。
2. 若用户输入文本包含 llm_keyword_triggers 中的任一关键词，且本次回复来自 LLM，则跳过概率判定，直接发送语音。
3. 上述强制触发规则仍要求回复是单段纯文本，且文本长度小于 threshold，避免对长文本或复杂消息错误转码。

## 使用建议

- 如果已启用本插件，建议关闭 AstrBot 自带 TTS，避免重复处理。
- 如果仍在使用 astrbot_plugin_outputpro，请关闭其中的 tts 步骤，避免重复触发。

## 迁移

从原输出增强插件迁移到独立 TTS 插件时，可参考 [astrbot_plugin_ttspro/MIGRATION.md](astrbot_plugin_ttspro/MIGRATION.md)。# astrbot_plugin_ttspro
