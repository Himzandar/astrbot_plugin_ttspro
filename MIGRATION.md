# 从 astrbot_plugin_outputpro 迁移到 astrbot_plugin_ttspro

本文用于把原插件中的文转语音能力平移到独立插件，尽量保持原有行为不变。

## 迁移目标

- 保留原有 TTS 触发条件
- 保留原有模型 TTS 与 QQ 声聊中转逻辑
- 避免和原输出增强插件重复触发

## 配置映射

原插件中的配置位置在 outputpro.tts 节点，新插件中这些字段提升为插件根配置。

对应关系如下：

- outputpro.tts.tts_provider_id -> tts_provider_id
- outputpro.tts.group_id -> group_id
- outputpro.tts.character_id -> character_id
- outputpro.tts.threshold -> threshold
- outputpro.tts.prob -> prob

原插件里“仅作用于 LLM 回复”的行为来自 pipeline.llm_steps 默认包含 tts。

在独立插件中，这个行为由 only_llm_result 控制：

- 如果你想保持和原默认行为一致：设为 true
- 如果你希望普通插件消息也参与文转语音：设为 false

## 推荐迁移步骤

1. 安装并启用 astrbot_plugin_ttspro。
2. 将原 outputpro.tts 下的配置值复制到新插件根配置。
3. 将 only_llm_result 保持为 true，这样默认行为与原流程一致。
4. 回到 astrbot_plugin_outputpro，关闭其中的 tts 步骤。
5. 如果 AstrBot 自带 TTS 已开启，也一并关闭，避免重复把同一条文本转成语音。

## 原插件中需要调整的地方

在 astrbot_plugin_outputpro 中，tts 步骤是否执行由 pipeline.steps 控制。

迁移后建议：

- 从 pipeline.steps 中移除 tts
- 从 pipeline.llm_steps 中移除 tts

这样可以避免：

- 原 outputpro 的 tts 先执行一次
- 独立插件再执行一次

## 行为对齐说明

独立插件默认保留了以下行为：

- 在发送前阶段处理消息
- 默认只处理 LLM 回复
- 只处理单段 Plain 文本消息
- 文本长度必须小于 threshold
- 随机命中 prob 后才触发
- 优先使用 tts_provider_id 对应的 TTS Provider
- 未配置 provider 时，当前 QQ 平台直接调用 QQ AI 语音
- 非 QQ 平台会尝试借已连接的 aiocqhttp bot 做 QQ 声聊中转
- 中转语音仍会按目标平台做格式转换

## 最小迁移示例

如果你原来的 outputpro 配置大致是：

```json
{
  "tts": {
    "tts_provider_id": "my_tts_provider",
    "group_id": "123456",
    "character_id": "lucy-voice-f36",
    "threshold": 50,
    "prob": 0.1
  }
}
```

那么新插件中可对应为：

```json
{
  "only_llm_result": true,
  "tts_provider_id": "my_tts_provider",
  "group_id": "123456",
  "character_id": "lucy-voice-f36",
  "threshold": 50,
  "prob": 0.1
}
```

## 常见问题

### 1. 为什么迁移后完全不触发？

优先检查：

- only_llm_result 是否为 true，而当前消息其实不是 LLM 回复
- 当前结果是否为单段纯文本
- 文本长度是否已经超过 threshold
- prob 是否太低
- 如果走 QQ 声聊中转，group_id 是否为空

### 2. 为什么会重复发语音？

通常是以下原因之一：

- outputpro 里的 tts 步骤没关
- AstrBot 自带 TTS 也开着

### 3. 什么时候必须填 group_id？

当你没有配置 tts_provider_id，需要走 QQ 声聊中转时，group_id 必填。

### 4. 配了 tts_provider_id 还需要 group_id 吗？

不需要。只要 provider 可用，插件会优先走模型 TTS。