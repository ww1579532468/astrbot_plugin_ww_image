"""WW 画图插件：文生图 / 图生图。

只保留一个 LLM 工具 ww_image。调用后立即返回给主对话，画图在后台进行，
完成后由插件直接把图片和一句说明发给用户。
"""
import asyncio
import base64
import json
import mimetypes
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_ww_image"
PLUGIN_AUTHOR = "ww"
PLUGIN_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "timeout": 120,
}

MAX_IMAGE_BYTES = 20 * 2048 * 2048
REF_DOWNLOAD_TIMEOUT = 20.0
REF_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
MAX_REF_IMAGES = 3
INDEPENDENT_LLM_TIMEOUT = 30.0
HISTORY_FAIL_MAX_LEN = 200

TOOL_RETURN_TO_MAIN_LLM = (
    "已开始画图，完成后会直接发给用户。先用一两句告诉用户正在画。"
)
INDEPENDENT_SYSTEM_PROMPT = (
    "用可爱语气，简单告知用户画图的内容和结果。失败时简短说明API返回的原因。不要提问，不要解释技术细节，不超过50字。"
)
HISTORY_USER_NOTE = "[画图任务完成]"
HISTORY_OK = "已画完并发送"
API_LOG_TEXT_MAX = 1000


@register(PLUGIN_NAME, PLUGIN_AUTHOR, "WW画图", PLUGIN_VERSION)
class WWImagePlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)

        cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            for key in DEFAULT_CONFIG:
                value = config.get(key)
                if value is not None and str(value).strip() != "":
                    cfg[key] = value

        self.base_url = str(cfg["base_url"] or "").rstrip("/")
        self.api_key = str(cfg["api_key"] or "").strip()
        self.model = str(cfg["model"] or "").strip()
        try:
            self.timeout = max(1, int(float(str(cfg["timeout"] or 120))))
        except (TypeError, ValueError):
            self.timeout = 120

        self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
        self._bg_tasks: set[asyncio.Task] = set()
        self._history_locks: dict[str, asyncio.Lock] = {}

    async def terminate(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"[WWImage] 后台画图任务异常: {exc}", exc_info=exc)

    def _parse_refs(self, refs: str) -> List[str]:
        text = str(refs or "").strip()
        if not text:
            return []
        items: Any = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
        if items is None:
            items = re.split(r"[\r\n]+", text)
        seen = set()
        result: List[str] = []
        for item in items:
            ref_text = str(item or "").strip()
            if ref_text and ref_text not in seen:
                seen.add(ref_text)
                result.append(ref_text)
        return result

    def _parse_extra_params(self, extra_params: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        text = str(extra_params or "").strip()
        if not text:
            return result
        for key, value in re.findall(r"--(\S+)\s+(\S+)", text):
            if key:
                result[key] = value
        return result

    async def _to_data_url(self, source: str, session: aiohttp.ClientSession) -> str:
        source = str(source or "").strip()
        if not source:
            raise RuntimeError("参考图地址为空")
        if source.startswith("data:image"):
            return source

        if source.startswith("http"):
            timeout_obj = aiohttp.ClientTimeout(total=REF_DOWNLOAD_TIMEOUT)
            async with session.get(
                source, headers=REF_DOWNLOAD_HEADERS, timeout=timeout_obj
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"参考图下载失败，服务器返回状态码 {response.status}")
                image_bytes = await response.read()
            content_type = str(response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip()
            mime_type = (
                content_type
                if content_type.startswith("image/")
                else (mimetypes.guess_type(source)[0] or "image/png")
            )
        elif os.path.exists(source):
            with open(source, "rb") as image_file:
                image_bytes = image_file.read()
            mime_type = mimetypes.guess_type(source)[0] or "image/png"
        else:
            raise RuntimeError(f"参考图无法读取: {source}")

        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise RuntimeError("参考图超过大小限制")
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    def _sanitize_for_log(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in ("b64_json", "base64", "image_base64", "image_data") and isinstance(item, str):
                    result[key] = f"<{key_text} len={len(item)}>"
                else:
                    result[key] = self._sanitize_for_log(item)
            return result
        if isinstance(value, list):
            return [self._sanitize_for_log(item) for item in value]
        if isinstance(value, str):
            if value.startswith("data:image"):
                return f"<data_url len={len(value)}>"
            if len(value) > 500 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value or ""):
                return f"<b64 len={len(value)}>"
            return value
        return value

    def _render_api_body(self, text: str, max_len: int = 0) -> str:
        raw = str(text or "")
        try:
            payload = json.loads(raw)
            rendered = json.dumps(self._sanitize_for_log(payload), ensure_ascii=False)
        except Exception:
            rendered = raw
        if max_len and len(rendered) > max_len:
            return rendered[:max_len] + f"… <truncated total={len(rendered)}>"
        return rendered

    def _log_api_response(self, status: int, text: str) -> None:
        rendered = self._render_api_body(text, API_LOG_TEXT_MAX)
        logger.info(f"[WWImage] 绘图 API 返回 HTTP {status}: {rendered}")

    def _extract_error(self, text: str, status: int) -> str:
        try:
            data = json.loads(text)
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                msg = str(error.get("message") or json.dumps(error, ensure_ascii=False))
            elif error:
                msg = str(error)
            else:
                msg = text[:240]
        except Exception:
            msg = text[:240]
        if status:
            return f"HTTP {status}: {msg}"
        return msg

    def _extract_image_url(self, payload: Any) -> str:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                raise RuntimeError(f"API 返回异常: {payload[:240]}")
        if not isinstance(payload, dict):
            raise RuntimeError("API 返回结构异常，未找到图片数据")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("API 返回结构异常，未找到图片数据")
        first = data[0]
        if not isinstance(first, dict):
            raise RuntimeError("API 返回结构异常，未找到图片数据")
        url = str(first.get("url") or "").strip()
        if url:
            return url
        b64_text = re.sub(r"\s+", "", str(first.get("b64_json") or ""))
        if b64_text:
            return "data:image/png;base64," + b64_text
        raise RuntimeError("API 返回结构异常，未找到图片数据")

    async def _generate(
        self,
        prompt: str,
        refs: List[str],
        aspect_ratio: str = "",
        size: str = "",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        if not self.base_url:
            raise RuntimeError("未配置 API 地址（base_url）")
        if not self.api_key:
            raise RuntimeError("未配置 API Key")
        if not self.model:
            raise RuntimeError("未配置绘图模型（model）")

        url = f"{self.base_url}/images/generations"
        payload: Dict[str, Any] = {"model": self.model, "prompt": prompt, "n": 1}
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if size:
            payload["size"] = size
        if extra_params:
            payload.update(extra_params)

        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key}
        timeout_obj = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession() as session:
            if refs:
                for index, ref in enumerate(refs[:MAX_REF_IMAGES], start=1):
                    data_url = await self._to_data_url(ref, session)
                    key = "image" if index == 1 else f"image{index}"
                    payload[key] = data_url
            logger.info(f"[WWImage] 请求绘图: {url} model={self.model} refs={len(refs)}")
            async with session.post(url, json=payload, headers=headers, timeout=timeout_obj) as response:
                text = await response.text()
                self._log_api_response(response.status, text)
                image_url = ""
                try:
                    image_url = self._extract_image_url(text)
                except Exception:
                    pass
                if not image_url:
                    logger.warning(
                        f"[WWImage] 未获得图片 HTTP {response.status}: {self._extract_error(text, response.status)}"
                    )
                return image_url, text

    def _image_component(self, image_url: str) -> Image:
        if image_url.startswith("data:image"):
            try:
                header, b64 = image_url.split(",", 1)
                image_bytes = base64.b64decode(b64, validate=False)
                mime_type = header[5:].split(";", 1)[0].strip() or "image/png"
                ext = mimetypes.guess_extension(mime_type) or ".png"
                save_dir = os.path.join(self.data_dir, "temp_images")
                os.makedirs(save_dir, exist_ok=True)
                file_path = os.path.abspath(
                    os.path.join(save_dir, f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}")
                )
                with open(file_path, "wb") as image_file:
                    image_file.write(image_bytes)
                return Image.fromFileSystem(file_path)
            except Exception as exc:
                logger.warning(f"[WWImage] data URL 图片落盘失败: {exc}")
        return Image.fromURL(image_url)

    def _truncate_error(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) > HISTORY_FAIL_MAX_LEN:
            return cleaned[:HISTORY_FAIL_MAX_LEN] + "…"
        return cleaned or "未知错误"

    async def _send_chain(self, event: AstrMessageEvent, umo: str, components: list) -> bool:
        chain = MessageChain(chain=list(components))
        try:
            await event.send(chain)
            return True
        except Exception as exc:
            logger.warning(f"[WWImage] event.send 失败，改用 send_message: {exc}")
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as exc:
            logger.error(f"[WWImage] send_message 失败: {exc}", exc_info=True)
            return False

    async def _independent_caption(
        self,
        provider_id: Optional[str],
        trigger_text: str,
        api_return: str,
    ) -> str:
        if not provider_id:
            return ""
        user_payload = (
            f"触发原话：\n{trigger_text or '（无）'}\n\n"
            f"绘图 API 返回：\n{self._render_api_body(api_return) or '（无）'}"
        )
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=user_payload,
                    system_prompt=INDEPENDENT_SYSTEM_PROMPT,
                    contexts=[],
                    tools=None,
                ),
                timeout=INDEPENDENT_LLM_TIMEOUT,
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[WWImage] 独立请求失败: {exc}")
            return ""

    async def _append_history(self, umo: str, cid: Optional[str], assistant_text: str) -> None:
        if not umo or not cid:
            return
        lock = self._history_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            try:
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if not conv:
                    return
                history = json.loads(conv.history or "[]")
                if not isinstance(history, list):
                    history = []
                history.append({"role": "user", "content": HISTORY_USER_NOTE})
                history.append({"role": "assistant", "content": assistant_text})
                await self.context.conversation_manager.update_conversation(
                    umo,
                    cid,
                    history=history,
                )
            except Exception as exc:
                logger.warning(f"[WWImage] 写入对话历史失败: {exc}")

    async def _snapshot_provider_id(self, umo: str) -> Optional[str]:
        try:
            prov = await self.context.get_using_provider_async(umo)
            if not prov:
                return None
            return prov.meta().id
        except Exception as exc:
            logger.warning(f"[WWImage] 获取当前会话对话模型失败: {exc}")
            return None

    async def _snapshot_cid(self, umo: str) -> Optional[str]:
        try:
            return await self.context.conversation_manager.get_curr_conversation_id(umo)
        except Exception as exc:
            logger.warning(f"[WWImage] 获取当前会话 cid 失败: {exc}")
            return None

    async def _run_background(
        self,
        event: AstrMessageEvent,
        umo: str,
        cid: Optional[str],
        provider_id: Optional[str],
        trigger_text: str,
        prompt: str,
        ref_list: List[str],
        aspect_ratio: str,
        size: str,
        extra: Dict[str, Any],
    ) -> None:
        image_url = ""
        api_error = ""
        api_body = ""
        try:
            image_url, api_body = await self._generate(
                prompt,
                ref_list,
                aspect_ratio=aspect_ratio,
                size=size,
                extra_params=extra,
            )
        except Exception as exc:
            api_error = str(exc)
            logger.error(f"[WWImage] 画图失败: {exc}", exc_info=True)

        image_sent = False
        if image_url:
            image_sent = await self._send_chain(
                event,
                umo,
                [self._image_component(image_url)],
            )
            if not image_sent and not api_error:
                api_error = "图片发送失败"

        caption_source = api_body or api_error
        caption = await self._independent_caption(
            provider_id,
            trigger_text,
            caption_source,
        )
        if caption:
            await self._send_chain(event, umo, [Plain(caption)])
        elif not image_url and caption_source:
            await self._send_chain(event, umo, [Plain(self._extract_error(api_body, 0) if api_body else api_error)])

        if image_url and image_sent:
            history_text = HISTORY_OK
        else:
            fail_text = api_error
            if not fail_text and api_body:
                fail_text = self._extract_error(api_body, 0)
            history_text = f"画图失败：{self._truncate_error(fail_text)}"
        await self._append_history(umo, cid, history_text)

    @llm_tool(name="ww_image")
    async def tool_ww_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect_ratio: str = "",
        size: str = "",
        extra_params: str = "",
        refs: str = "",
    ) -> str:
        """AI 画图工具：根据文字描述生成图片。如果用户要求基于其已有图片修改、重绘或换风格，请把该图片地址通过 refs 参数传入，即图生图。该工具立即返回，不会阻塞对话；图片在后台生成完成后会直接发给用户，请先用一两句话告诉用户正在画，不要等待结果。

        Args:
            prompt (string): 图片提示词，描述主体、风格、场景、构图和细节。
            aspect_ratio (string): 宽高比例，例如 1:1、3:4、9:16、16:9。
            size (string): 分辨率或尺寸参数，例如 1024x1024。
            extra_params (string): 其他模型参数透传，格式为 --key value，可同时传多个。
            refs (string): 参考图片地址，用于图生图/改图。可以是图片 URL、data URL 或本地路径；多张用换行分隔，也可传 JSON 数组字符串。
        """
        prompt = str(prompt or "").strip()
        ref_list = self._parse_refs(refs)
        if not prompt and not ref_list:
            return "画图失败：缺少 prompt 或 refs 参数。"

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        trigger_text = str(getattr(event, "message_str", "") or "")
        extra = self._parse_extra_params(extra_params)
        provider_id = await self._snapshot_provider_id(umo) if umo else None
        cid = await self._snapshot_cid(umo) if umo else None

        self._spawn(
            self._run_background(
                event=event,
                umo=umo,
                cid=cid,
                provider_id=provider_id,
                trigger_text=trigger_text,
                prompt=prompt,
                ref_list=ref_list,
                aspect_ratio=str(aspect_ratio or "").strip(),
                size=str(size or "").strip(),
                extra=extra,
            )
        )
        return TOOL_RETURN_TO_MAIN_LLM
