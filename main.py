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

    @property
    def tts_provider_id(self) -> str:
        return (self._cfg.get("tts_provider_id", "") or "").strip()

    @property
    def group_id(self) -> str:
        return (self._cfg.get("group_id", "") or "").strip()

    @property
    def character_id(self) -> str:
        return (
            self._cfg.get("character_id", "lucy-voice-f36")
            or "lucy-voice-f36"
        ).strip()

    @property
    def threshold(self) -> int:
        return int(self._cfg.get("threshold", 50) or 50)

    @property
    def prob(self) -> float:
        return float(self._cfg.get("prob", 0.1) or 0.1)

    @property
    def only_llm_result(self) -> bool:
        return bool(self._cfg.get("only_llm_result", True))


class TTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)

    def _build_record_from_audio(self, audio: str, text: str) -> Record:
        audio = (audio or "").strip()
        if audio.startswith(("http://", "https://")):
            return Record.fromURL(audio, text=text)
        if audio.startswith("file:///"):
            return Record(file=audio, url=audio, text=text)
        return Record.fromFileSystem(audio, text=text, url=audio)

    def _get_selected_tts_provider(self) -> TTSProvider | None:
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
        for platform in self.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
            except Exception:
                continue
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
            if probe_head.startswith(b"INVALID"):
                raise ValueError("relay audio payload is INVALID")

            normalized_input = raw_path
            with open(raw_path, "rb") as file_obj:
                head = file_obj.read(64)
            upper_head = head.upper()

            is_silk = b"SILK" in upper_head
            is_amr = b"#!AMR" in upper_head
            if is_silk:
                with open(raw_path, "rb") as file_obj:
                    raw_bytes = file_obj.read()
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
        result = event.get_result()
        if not result or not result.chain:
            return False
        if self.cfg.only_llm_result and not result.is_llm_result():
            return False
        if len(result.chain) != 1:
            return False
        first = result.chain[0]
        if not isinstance(first, Plain):
            return False
        if len(first.text) >= self.cfg.threshold:
            return False
        return random.random() < self.cfg.prob

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
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