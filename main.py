import json
import os
import random
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.io import download_file
from astrbot.core.utils.media_utils import convert_audio_format, convert_audio_to_wav
from astrbot.core.utils.tencent_record_helper import tencent_silk_to_wav


class PluginConfig:
    def __init__(self, cfg: AstrBotConfig, context: Context):
        self._cfg = cfg
        self.context = context

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if isinstance(value, str):
            items = value.replace("\r", "\n").replace(",", "\n").split("\n")
            return [item.strip() for item in items if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @property
    def tts_provider_id(self) -> str:
        """获取配置的 TTS 提供商 ID。"""
        return (self._cfg.get("tts_provider_id", "") or "").strip()

    @property
    def group_id(self) -> str:
        """获取配置的群组 ID。"""
        return (self._cfg.get("group_id", "") or "").strip()

    @property
    def character_id(self) -> str:
        """获取配置的角色 ID。"""
        return (
            self._cfg.get("character_id", "lucy-voice-f36")
            or "lucy-voice-f36"
        ).strip()

    @property
    def threshold(self) -> int:
        """获取配置的阈值。"""
        return int(self._cfg.get("threshold", 50) or 50)

    @property
    def prob(self) -> float:
        """获取配置的概率。"""
        return float(self._cfg.get("prob", 0.1) or 0.1)

    @property
    def only_llm_result(self) -> bool:
        """获取配置的 only_llm_result。"""
        return bool(self._cfg.get("only_llm_result", True))

    @property
    def enable_llm_tool(self) -> bool:
        """获取 llm_tool 开关。"""
        return bool(self._cfg.get("enable_llm_tool", True))

    @property
    def force_tts_user_ids(self) -> list[str]:
        """获取强制触发语音的用户 ID 列表。"""
        return self._normalize_list(self._cfg.get("force_tts_user_ids", []))


class TTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        logger.info(
            "TTS plugin init: enable_llm_tool=%s only_llm_result=%s provider_id=%s group_id=%s character_id=%s threshold=%s prob=%.4f force_user_count=%s",
            self.cfg.enable_llm_tool,
            self.cfg.only_llm_result,
            self.cfg.tts_provider_id or "<empty>",
            self.cfg.group_id or "<empty>",
            self.cfg.character_id,
            self.cfg.threshold,
            self.cfg.prob,
            len(self.cfg.force_tts_user_ids),
        )

    @staticmethod
    def _preview_text(text: str, limit: int = 80) -> str:
        text = (text or "").replace("\n", "\\n").strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    async def _append_voice_tool_context(
        self, event: AstrMessageEvent, voice_text: str
    ) -> None:
        """将语音工具的成功结果追加到当前会话历史中。"""
        context_obj: Any = self.context
        unified_msg_origin = getattr(event, "unified_msg_origin", "") or ""
        if not unified_msg_origin:
            logger.debug("TTS llm_tool debug: skip appending context because unified_msg_origin is empty")
            return

        conversation_id = await context_obj.conversation_manager.get_curr_conversation_id(
            unified_msg_origin
        )
        if not conversation_id:
            logger.debug("TTS llm_tool debug: skip appending context because conversation_id is empty")
            return

        conversation = await context_obj.conversation_manager.get_conversation(
            unified_msg_origin,
            conversation_id,
        )
        if not conversation:
            logger.debug(
                "TTS llm_tool debug: skip appending context because conversation not found conversation_id=%s",
                conversation_id,
            )
            return

        try:
            history = json.loads(conversation.history or "[]")
        except Exception:
            logger.exception("TTS llm_tool debug: failed to parse conversation history")
            return

        history.append(
            {
                "role": "tool",
                "Args": (
                    f"text:{voice_text}"
                ),
                "Result": "[TOOL_DIRECT_RETURN] Sent a TTS voice message directly without returning text result.",
            }
        )
        await context_obj.conversation_manager.update_conversation(
            unified_msg_origin,
            conversation_id,
            history,
        )
        logger.debug(
            "TTS llm_tool debug: appended voice tool context conversation_id=%s text_snippet=%s",
            conversation_id,
            self._preview_text(voice_text),
        )

    def _build_record_from_audio(self, audio: str, text: str) -> Record:
        """根据音频输入构建 Record 对象。"""
        audio = (audio or "").strip()
        if audio.startswith(("http://", "https://")):
            return Record.fromURL(audio, text=text)
        if audio.startswith("file:///"):
            return Record(file=audio, url=audio, text=text)
        return Record.fromFileSystem(audio, text=text, url=audio)

    def _get_selected_tts_provider(self) -> TTSProvider | None:
        """根据配置获取选定的 TTS 提供商实例。如果未配置或找不到提供商，则返回 None。"""
        provider_id = self.cfg.tts_provider_id
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if not provider:
            raise ValueError(f"未找到 TTS 提供商: {provider_id}")
        if not isinstance(provider, TTSProvider):
            raise ValueError(
                f"提供商 {provider_id} 不是 TTS 类型，实际类型: {type(provider)}"
            )
        return provider

    def _get_qq_relay_bot(self) -> Any | None:
        """在 aiocqhttp 平台中查找可用的 bot 实例，用于 QQ 语音中转功能。如果找到符合条件的 bot，则返回该实例；否则返回 None。"""
        for platform in self.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
            except Exception:
                continue
            # 目前仅在 aiocqhttp 平台启用 QQ 语音中转功能，因为其他平台的适配复杂度较高，且主要受众是使用 aiocqhttp 的用户群体，同时也避免了在不支持的
            # 平台上出现意外行为。未来如果有更多平台需要支持，可以考虑扩展此处的逻辑。
            if getattr(meta, "name", "") != "aiocqhttp":
                continue
            bot = getattr(platform, "bot", None)
            if bot:
                return bot
        return None

    async def _generate_record_for_text(
        self, text: str, event: AstrMessageEvent
    ) -> Record:
        """将文本转换为当前平台可发送的语音 Record。"""
        logger.debug(
            "TTS debug: generate record platform=%s sender=%s text_len=%s text_snippet=%s",
            event.get_platform_name(),
            event.get_sender_id(),
            len(text or ""),
            self._preview_text(text),
        )
        provider = self._get_selected_tts_provider()
        logger.debug("TTS debug: selected provider=%s", provider)
        if provider:
            logger.debug("TTS debug: using configured provider path provider=%s", provider)
            audio = await provider.get_audio(text)
            return self._build_record_from_audio(audio, text)

        if isinstance(event, AiocqhttpMessageEvent):
            logger.debug(
                "TTS debug: using aiocqhttp direct path group_id=%s character_id=%s",
                self.cfg.group_id,
                self.cfg.character_id,
            )
            audio = await event.bot.get_ai_record(
                character=self.cfg.character_id,
                group_id=int(self.cfg.group_id),
                text=text,
            )
            return Record.fromURL(audio)

        relay_bot = self._get_qq_relay_bot()
        if relay_bot and self.cfg.group_id:
            logger.debug(
                "TTS debug: using relay bot path group_id=%s character_id=%s platform=%s",
                self.cfg.group_id,
                self.cfg.character_id,
                event.get_platform_name(),
            )
            audio = await relay_bot.get_ai_record(
                character=self.cfg.character_id,
                group_id=int(self.cfg.group_id),
                text=text,
            )
            return await self._build_relay_record_for_platform(
                audio_url=audio,
                event=event,
                text=text,
            )

        logger.debug(
            "TTS debug: no available generation path provider_id=%s group_id=%s has_relay_bot=%s platform=%s",
            self.cfg.tts_provider_id,
            self.cfg.group_id,
            bool(relay_bot),
            event.get_platform_name(),
        )
        raise ValueError("未找到可用的 TTS Provider 或 QQ 语音中转配置")

    async def _build_relay_record_for_platform(
        self,
        audio_url: str,
        event: AstrMessageEvent,
        text: str,
    ) -> Record:
        """构建适用于特定平台的 Record 对象，使用 QQ 语音中转功能将音频 URL 转换为平台兼容的格式。对于 aiocqhttp 平台，直接使用 URL；对于其他平台，下载并转换音频文件以确保兼容性。"""
        platform_name = str(event.get_platform_name() or "")
        logger.debug(f"Relay debug: enter _build_relay_record_for_platform platform={platform_name} audio_url={audio_url}")
        if platform_name == "aiocqhttp":
            return Record.fromURL(audio_url)

        try:
            temp_dir = get_astrbot_temp_path()
            os.makedirs(temp_dir, exist_ok=True)
            parsed = urlparse(audio_url)
            suffix = os.path.splitext(parsed.path)[1] or ".audio"
            raw_path = os.path.join(
                temp_dir, f"ttspro_relay_{uuid.uuid4().hex}{suffix}"
            )
            await download_file(audio_url, raw_path)

            with open(raw_path, "rb") as file_obj:
                probe_head = file_obj.read(16)
            logger.debug(
                "Relay debug: downloaded raw_path=%s probe_head=%s",
                raw_path,
                probe_head[:16],
            )
            # 相同的错误标记在不同提供商的语音中转中可能会有不同的表现形式，但通常包含 "INVALID" 这样的关键词，因此通过检查文件头部是否包含该关键词来初步判断下载的内容是否有效。如果检测到无效标记，则抛出异常以触发后续的备用处理逻辑。
            if probe_head.startswith(b"INVALID"):
                raise ValueError("relay audio payload is INVALID")

            normalized_input = raw_path
            with open(raw_path, "rb") as file_obj:
                head = file_obj.read(64)
            upper_head = head.upper()

            # Detect the upstream container early so downstream conversion uses
            # the right decoder path.
            is_silk = b"SILK" in upper_head
            is_amr = b"#!AMR" in upper_head
            if is_silk:
                with open(raw_path, "rb") as file_obj:
                    raw_bytes = file_obj.read()
                # Tencent voice payloads may carry a variant SILK header; rewrite
                # it into the form expected by the converter when needed.
                if raw_bytes and raw_bytes[0] in (0x02, 0x03) and b"#!SILK" in raw_bytes[:16]:
                    normalized_silk = bytes([0x02]) + raw_bytes[1:]
                    silk_path = str(
                        Path(temp_dir) / f"ttspro_relay_{uuid.uuid4().hex}.silk"
                    )
                    with open(silk_path, "wb") as file_obj:
                        file_obj.write(normalized_silk)
                    normalized_input = silk_path

                wav_path = os.path.join(temp_dir, f"ttspro_relay_{uuid.uuid4().hex}.wav")
                await tencent_silk_to_wav(normalized_input, wav_path)
                normalized_input = wav_path
            elif is_amr:
                normalized_input = await convert_audio_to_wav(raw_path)

            # Telegram accepts OGG more reliably, while other platforms keep the
            # shared WAV fallback path.
            if platform_name == "telegram":
                converted_path = await convert_audio_format(
                    normalized_input, output_format="ogg"
                )
            else:
                converted_path = await convert_audio_to_wav(normalized_input)

            return Record.fromFileSystem(converted_path, text=text, url=converted_path)
        except Exception:
            logger.exception("Relay debug: failed to build relay record for platform")
            return Record.fromURL(audio_url, text=text)

    def _should_handle(self, event: AstrMessageEvent) -> bool:
        """判断是否应该处理当前事件，基于配置的条件进行筛选。只有当事件满足所有条件时，才会返回 True，表示应该处理该事件；否则返回 False，跳过处理。"""
        result = event.get_result()
        if not result or not result.chain:
            logger.debug("TTS debug: skip decorating because result chain is empty")
            return False
        if len(result.chain) != 1:
            logger.debug(
                "TTS debug: skip decorating because chain size is not 1 chain_size=%s",
                len(result.chain),
            )
            return False
        first = result.chain[0]
        # 目前仅处理纯文本消息链，如果消息链中包含非纯文本组件（如图片、表情等），则不进行语音转换，以避免不必要的复杂性和潜在的错误。
        if not isinstance(first, Plain):
            logger.debug(
                "TTS debug: skip decorating because first component is not Plain type=%s",
                type(first),
            )
            return False
        # 如果消息文本长度超过配置的阈值，则不处理，避免将过长的文本转换为语音导致性能问题或不必要的资源消耗。
        if len(first.text) >= self.cfg.threshold:
            logger.debug(
                "TTS debug: skip decorating because text is too long text_len=%s threshold=%s text_snippet=%s",
                len(first.text),
                self.cfg.threshold,
                self._preview_text(first.text),
            )
            return False

        is_llm_result = result.is_llm_result()
        # 如果配置了 only_llm_result 且当前结果不是 LLM 结果，则不处理。
        if self.cfg.only_llm_result and not is_llm_result:
            logger.debug(
                "TTS debug: skip decorating because result is not llm result and only_llm_result is enabled"
            )
            return False
        # 强制触发的用户 ID 优先级最高，如果发送者 ID 在配置的强制触发列表中，则直接处理。
        sender_id = str(event.get_sender_id() or "")
        if sender_id and sender_id in self.cfg.force_tts_user_ids:
            logger.debug(
                "TTS debug: decorate forced by sender_id=%s text_snippet=%s",
                sender_id,
                self._preview_text(first.text),
            )
            return True

        random_value = random.random()
        should_handle = random_value < self.cfg.prob
        logger.debug(
            "TTS debug: decorate probability check random_value=%.4f prob=%.4f should_handle=%s text_snippet=%s",
            random_value,
            self.cfg.prob,
            should_handle,
            self._preview_text(first.text),
        )
        return should_handle

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool: Any, tool_args: dict | None
    ) -> None:
        tool_name = getattr(tool, "name", "")
        if tool_name != "send_tts_voice":
            return
        args = tool_args or {}
        logger.info(
            "TTS llm_tool debug: invoking tool=%s platform=%s sender=%s text_len=%s text_snippet=%s",
            tool_name,
            event.get_platform_name(),
            event.get_sender_id(),
            len(str(args.get("text", "") or "")),
            self._preview_text(str(args.get("text", "") or "")),
        )

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        tool_name = getattr(tool, "name", "")
        if tool_name != "send_tts_voice":
            return
        args = tool_args or {}
        logger.info(
            "TTS llm_tool debug: tool finished tool=%s platform=%s sender=%s result_type=%s result_preview=%s text_snippet=%s",
            tool_name,
            event.get_platform_name(),
            event.get_sender_id(),
            type(tool_result).__name__ if tool_result is not None else "NoneType",
            self._preview_text(str(tool_result), 120) if tool_result is not None else "sent_directly_or_no_result",
            self._preview_text(str(args.get("text", "") or "")),
        )

    @filter.llm_tool(name="send_tts_voice")
    async def send_tts_voice_tool(self, event: AstrMessageEvent, text: str):
        """将指定文本直接转换成语音并发送到当前会话。

        【优先调用场景】
        - 用户明确要求你发语音、语音回复、朗读、播报、念一遍、用声音说。
        - 用户给出一小段话，要求你“帮我读出来”“帮我念成语音”。
        - 你判断此时用一条短语音回复，比发纯文本更符合用户要求。

        【不要调用的场景】
        - 文本太长、内容是多段说明、列表、代码、链接时，不要调用，改用普通文本回复。
        - 你还没确定最终要说哪一句话时，不要先调用；先整理成一句自然、可直接朗读的口语句子。

        【调用要求】
        - text 必须直接填写最终要朗读给用户听的话，不要加“以下是语音内容”“帮你转成语音”等说明。
        - 句子尽量短、口语化、一次说清，避免书面腔和大段文本。
        - 如果用户只是想听你说一句话，优先直接调用本工具，而不是先输出同样的文字再转语音。

        【目标】
        更稳定地在“用户要语音/朗读/播报”的场景下直接发出一条自然的语音消息。

        Args:
            text(string): 要转换并发送为语音的文本内容。应直接提供最终要朗读的话，不要包含额外说明。
        """
        if not self.cfg.enable_llm_tool:
            logger.debug("TTS llm_tool debug: tool disabled by config")
            return (
                "[TOOL_UNAVAILABLE] 当前语音工具暂时不可用。"
                "请直接用自然语气回复用户，不要提工具、配置或指令。"
            )

        voice_text = (text or "").strip()
        if not voice_text:
            logger.debug("TTS llm_tool debug: tool called with empty text")
            return (
                "[TOOL_FAILED] 没有拿到要朗读的文本。"
                "请先整理好一句简短的话，再决定是否调用语音工具。"
            )
        if len(voice_text) >= self.cfg.threshold:
            logger.debug(
                "TTS llm_tool debug: text too long text_len=%s threshold=%s text_snippet=%s",
                len(voice_text),
                self.cfg.threshold,
                self._preview_text(voice_text),
            )
            return (
                f"[TOOL_FAILED] 要发送的语音文本过长，当前上限为 {self.cfg.threshold} 字。"
                "请改成更短、更口语化的一句后再试，不要重复硬调工具。"
            )

        try:
            record = await self._generate_record_for_text(voice_text, event)
            await event.send(event.chain_result([record]))
            await self._append_voice_tool_context(event, voice_text)
            logger.debug("已通过 llm_tool 发送语音，text_snippet=%s", voice_text[:120])
            return None
        except Exception:
            logger.exception("LLM TTS tool processing failed")
            return (
                "[TOOL_FAILED] 语音发送失败了。"
                "请直接用自然语言继续回复用户，不要重复调用这个工具。"
            )

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在结果装饰阶段处理事件，根据配置的条件判断是否需要将文本消息转换为语音消息。如果满足条件，则尝试使用配置的 TTS 提供商进行转换；如果未配置提供商或转换失败，则根据事件平台选择合适的方式进行语音中转处理。最终将转换后的语音消息替换原有的文本消息链。"""
        if not self._should_handle(event):
            return

        result = event.get_result()
        if not result or not result.chain:
            return
        first = result.chain[0]
        if not isinstance(first, Plain):
            return
        text = first.text
        logger.debug(
            "TTS debug: handling event platform=%s sender=%s text_snippet=%s",
            event.get_platform_name(),
            event.get_sender_id(),
            (text or "")[:120],
        )
        try:
            result.chain[:] = [await self._generate_record_for_text(text, event)]
            logger.debug(f"已将文本消息{text[:10]}转化为语音消息")
        except Exception:
            logger.exception("TTS processing failed")
