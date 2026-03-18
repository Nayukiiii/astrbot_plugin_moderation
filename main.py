from astrbot.api.all import *
from astrbot.api.event import filter
import asyncio
import base64
import datetime
import json
import os
import re
import aiohttp


@register(
    "astrbot_plugin_moderation",
    "Nayukiiii",
    "群消息内容审核：APK/视频拦截，文本关键词+NIM，图片OpenAI，链接域名检测",
    "2.0.0",
)
class ModerationPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.nim_api_key = config.get("nim_api_key", "")
        self.openai_api_key = config.get("openai_api_key", "")
        self.enabled_groups = config.get("enabled_groups", [])
        self.ban_duration = int(config.get("ban_duration", 600))

        plugin_dir = os.path.dirname(__file__)
        self.log_file = os.path.join(plugin_dir, "moderation_log.jsonl")
        self._keywords: list = []
        self._domains: set = set()
        self._load_wordlists(plugin_dir)

    # ── 词库加载 ────────────────────────────────────────────────────────
    def _load_wordlists(self, plugin_dir: str):
        kw_path = os.path.join(plugin_dir, "keywords.txt")
        domain_path = os.path.join(plugin_dir, "domains.txt")

        if os.path.exists(kw_path):
            with open(kw_path, encoding="utf-8") as f:
                self._keywords = [l.strip() for l in f if l.strip()]
            logger.info("[moderation] 关键词加载: {} 条".format(len(self._keywords)))
        else:
            logger.warning("[moderation] keywords.txt 不存在")

        if os.path.exists(domain_path):
            with open(domain_path, encoding="utf-8") as f:
                self._domains = {l.strip().lower() for l in f if l.strip()}
            logger.info("[moderation] 域名库加载: {} 条".format(len(self._domains)))
        else:
            logger.warning("[moderation] domains.txt 不存在")

    # ── 主入口 ──────────────────────────────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)

        if self.enabled_groups and group_id not in [str(g) for g in self.enabled_groups]:
            return

        sender_id = str(event.message_obj.sender.user_id)
        msg_chain = event.message_obj.message

        violated = False
        reason = ""
        msg_type = "unknown"

        # ── 消息链遍历（APK / 视频 / 图片）──────────────────────────────
        for component in msg_chain:
            # APK
            if isinstance(component, File):
                fname = getattr(component, "name", "") or ""
                if fname.lower().endswith(".apk"):
                    violated = True
                    reason = "APK文件（规则拦截）"
                    msg_type = "apk"
                    break

            # 视频
            if isinstance(component, Video):
                violated = True
                reason = "视频文件（规则拦截）"
                msg_type = "video"
                break

            # 图片
            if isinstance(component, Image):
                url = getattr(component, "url", None) or getattr(component, "file", None)
                if url:
                    hit, detail = await self._check_image(url)
                    if hit:
                        violated = True
                        msg_type = "image"
                        reason = "图片违规: {}".format(detail)
                        break

        # ── 文本检测（关键词 → 域名 → NIM）──────────────────────────────
        if not violated:
            text = event.message_str.strip()
            if text:
                msg_type = "text"

                # 1. 关键词
                hit, detail = self._check_keywords(text)
                if hit:
                    violated = True
                    reason = "关键词: {}".format(detail)

                # 2. 非法域名
                if not violated:
                    hit, detail = self._check_domains(text)
                    if hit:
                        violated = True
                        reason = "非法域名: {}".format(detail)

                # 3. NIM Llama Guard
                if not violated and self.nim_api_key:
                    hit, detail = await self._check_nim(text)
                    if hit:
                        violated = True
                        reason = "NIM: {}".format(detail)

        if not violated:
            return

        await self._handle_violation(event, group_id, sender_id, msg_type, reason)

    # ── 关键词检测 ───────────────────────────────────────────────────────
    def _check_keywords(self, text: str):
        for kw in self._keywords:
            if kw in text:
                return True, kw
        return False, ""

    # ── 域名检测 ─────────────────────────────────────────────────────────
    _URL_RE = re.compile(
        r'(?:https?://)?'
        r'((?:[a-z0-9\-]+\.)+[a-z]{2,})',
        re.IGNORECASE
    )

    def _check_domains(self, text: str):
        for m in self._URL_RE.finditer(text):
            domain = m.group(1).lower()
            if domain in self._domains:
                return True, domain
        return False, ""

    # ── NIM Llama Guard ───────────────────────────────────────────────────
    async def _check_nim(self, text: str):
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Authorization": "Bearer {}".format(self.nim_api_key),
            "Content-Type": "application/json",
        }
        payload = {
            "model": "meta/llama-guard-4-12b",
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 128,
            "temperature": 0,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[moderation] NIM {} ".format(resp.status))
                        return False, ""
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"].strip().lower()
                    if content.startswith("unsafe"):
                        cat = content.replace("unsafe", "").strip()
                        return True, cat if cat else "unsafe"
                    return False, ""
        except asyncio.TimeoutError:
            logger.warning("[moderation] NIM 超时")
            return False, ""
        except Exception as e:
            logger.error("[moderation] NIM 异常: {}".format(e))
            return False, ""

    # ── OpenAI 图片审核 ───────────────────────────────────────────────────
    async def _check_image(self, url_or_path: str):
        if not self.openai_api_key:
            return False, ""

        api_url = "https://api.openai.com/v1/moderations"
        headers = {
            "Authorization": "Bearer {}".format(self.openai_api_key),
            "Content-Type": "application/json",
        }

        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            image_input = {"type": "image_url", "image_url": {"url": url_or_path}}
        else:
            try:
                with open(url_or_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                image_input = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,{}".format(b64)}}
            except Exception as e:
                logger.error("[moderation] 读取图片失败: {}".format(e))
                return False, ""

        payload = {"model": "omni-moderation-latest", "input": [image_input]}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[moderation] OpenAI {} ".format(resp.status))
                        return False, ""
                    data = await resp.json()
                    result = data["results"][0]
                    if result["flagged"]:
                        cats = [k for k, v in result["categories"].items() if v]
                        return True, ", ".join(cats) if cats else "flagged"
                    return False, ""
        except asyncio.TimeoutError:
            logger.warning("[moderation] OpenAI 超时")
            return False, ""
        except Exception as e:
            logger.error("[moderation] OpenAI 异常: {}".format(e))
            return False, ""

    # ── 违规处理 ──────────────────────────────────────────────────────────
    async def _handle_violation(self, event, group_id: str, sender_id: str, msg_type: str, reason: str):
        logger.info("[moderation] 违规 | 群:{} QQ:{} 类型:{} 原因:{}".format(
            group_id, sender_id, msg_type, reason))

        # 撤回
        try:
            msg_id = event.message_obj.message_id
            if msg_id:
                await event.bot.api.delete_msg(message_id=msg_id)
        except Exception as e:
            logger.warning("[moderation] 撤回失败: {}".format(e))

        # 禁言
        try:
            await event.bot.api.set_group_ban(
                group_id=int(group_id),
                user_id=int(sender_id),
                duration=self.ban_duration,
            )
        except Exception as e:
            logger.warning("[moderation] 禁言失败: {}".format(e))

        # 日志
        self._write_log(group_id, sender_id, msg_type, reason)

    # ── 日志 ──────────────────────────────────────────────────────────────
    def _write_log(self, group_id: str, sender_id: str, msg_type: str, reason: str):
        entry = {
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "group_id": group_id,
            "sender_id": sender_id,
            "msg_type": msg_type,
            "reason": reason,
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[moderation] 写日志失败: {}".format(e))

    # ── 指令：查看日志 ────────────────────────────────────────────────────
    @filter.command("modlog")
    async def cmd_modlog(self, event: AstrMessageEvent, n: int = 10):
        """查看最近违规记录，用法：/modlog [条数]"""
        if not os.path.exists(self.log_file):
            yield event.plain_result("暂无违规记录。")
            return
        try:
            with open(self.log_file, encoding="utf-8") as f:
                lines = f.readlines()
            recent = lines[-n:]
            entries = [json.loads(ln) for ln in recent]
            text = "最近 {} 条违规记录：\n".format(len(entries))
            for e in entries:
                text += "[{}] 群{} QQ{} {} | {}\n".format(
                    e["time"], e["group_id"], e["sender_id"], e["msg_type"], e["reason"])
            yield event.plain_result(text.strip())
        except Exception as ex:
            yield event.plain_result("读取日志失败：{}".format(ex))

    # ── 指令：重载词库 ────────────────────────────────────────────────────

    @filter.command("添加词库")
    async def cmd_add_keyword(self, event: AstrMessageEvent, word: str = ""):
        """添加自定义关键词，用法：/添加词库 词语"""
        if not word:
            yield event.plain_result("用法：/添加词库 词语")
            return
        kw_path = os.path.join(os.path.dirname(__file__), "keywords.txt")
        with open(kw_path, "a", encoding="utf-8") as f:
            f.write("\n" + word.strip())
        self._keywords.append(word.strip())
        yield event.plain_result("已添加关键词：{}（当前共 {} 条）".format(word.strip(), len(self._keywords)))

    @filter.command("modreload")
    async def cmd_modreload(self, event: AstrMessageEvent):
        """重载关键词和域名词库"""
        self._load_wordlists(os.path.dirname(__file__))
        yield event.plain_result("词库已重载：关键词 {} 条，域名 {} 条".format(
            len(self._keywords), len(self._domains)))