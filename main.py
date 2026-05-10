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
    def force_tts_user_ids(self) -> list[str]:
        """获取强制触发语音的用户 ID 列表。"""
        return self._normalize_list(self._cfg.get("force_tts_user_ids", []))

    @property
    def llm_keyword_triggers(self) -> list[str]:
        """获取触发 LLM 语音的关键词列表。"""
        return self._normalize_list(self._cfg.get("llm_keyword_triggers", []))


class TTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)

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
            #平台上出现意外行为。未来如果有更多平台需要支持，可以考虑扩展此处的逻辑。
            if getattr(meta, "name", "") != "aiocqhttp":
                continue
            bot = getattr(platform, "bot", None)
            if bot:
                return bot
        return None

    async def _build_relay_record_for_platform(
        self,
        audio_url: str,
        event: AstrMessageEvent,
        text: str,
    ) -> Record:
        """构建适用于特定平台的 Record 对象，使用 QQ 语音中转功能将音频 URL 转换为平台兼容的格式。对于 aiocqhttp 平台，直接使用 URL；对于其他平台，下载并转换音频文件以确保兼容性。"""
        platform_name = str(event.get_platform_name() or "")
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
            # Some providers may return an error marker instead of binary audio.
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
            return Record.fromURL(audio_url, text=text)

    def _should_handle(self, event: AstrMessageEvent) -> bool:
        """判断是否应该处理当前事件，基于配置的条件进行筛选。只有当事件满足所有条件时，才会返回 True，表示应该处理该事件；否则返回 False，跳过处理。"""
        result = event.get_result()
        if not result or not result.chain:
            return False
        if len(result.chain) != 1:
            return False
        first = result.chain[0]
        if not isinstance(first, Plain):
            return False
        if len(first.text) >= self.cfg.threshold:
            return False

        is_llm_result = result.is_llm_result()
        sender_id = str(event.get_sender_id() or "")
        if sender_id and sender_id in self.cfg.force_tts_user_ids:
            return True

        incoming_text = (getattr(event, "message_str", "") or "").strip()
        if is_llm_result and incoming_text:
            for keyword in self.cfg.llm_keyword_triggers:
                if keyword in incoming_text:
                    return True

        if self.cfg.only_llm_result and not is_llm_result:
            return False
        return random.random() < self.cfg.prob

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在结果装饰阶段处理事件，根据配置的条件判断是否需要将文本消息转换为语音消息。如果满足条件，则尝试使用配置的 TTS 提供商进行转换；如果未配置提供商或转换失败，则根据事件平台选择合适的方式进行语音中转处理。最终将转换后的语音消息替换原有的文本消息链。"""
        if not self._should_handle(event):
            return

        result = event.get_result()
        text = result.chain[0].text
        try:
            provider = self._get_selected_tts_provider()
            if provider:
                audio = await provider.get_audio(text)
                result.chain[:] = [self._build_record_from_audio(audio, text)]
                logger.debug(f"已使用配置的 TTS 模型将文本消息{text[:10]}转为语音")
                return

            if isinstance(event, AiocqhttpMessageEvent):
                audio = await event.bot.get_ai_record(
                    character=self.cfg.character_id,
                    group_id=int(self.cfg.group_id),
                    text=text,
                )
                result.chain[:] = [Record.fromURL(audio)]
                logger.debug(f"已将文本消息{text[:10]}转化为语音消息")
                return

            relay_bot = self._get_qq_relay_bot()
            if relay_bot and self.cfg.group_id:
                audio = await relay_bot.get_ai_record(
                    character=self.cfg.character_id,
                    group_id=int(self.cfg.group_id),
                    text=text,
                )
                result.chain[:] = [
                    await self._build_relay_record_for_platform(
                        audio_url=audio,
                        event=event,
                        text=text,
                    )
                ]
                logger.debug(f"已通过QQ中转将文本消息{text[:10]}转化为语音消息")
        except Exception as exc:
            logger.warning(str(exc))
