from __future__ import annotations

import json
import time
import asyncio
import random
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig


WELCOME_MODE_TEXT = "text"
WELCOME_MODE_AI = "ai"
DEFAULT_WELCOME_TEMPLATE = "🎉 欢迎 {name} 加入本群！很高兴认识你～{count_text}"
DEFAULT_AI_PROMPT = (
    "请根据以下昵称，生成一句简短、温暖、有趣的入群欢迎语"
    "（不超过30字，不要带引号）：{name}"
)
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _parse_id_list(value) -> set:
    """解析 list 或逗号分隔的群号字符串为集合（兼容旧版字符串格式）。"""
    if isinstance(value, list):
        return set(str(item) for item in value if str(item).strip())
    if not isinstance(value, str):
        return set()
    return set(item.strip() for item in value.split(",") if item.strip())


def _serialize_id_list(id_set: set) -> list:
    """序列化群号集合为列表，与 _conf_schema.json 的 list 类型对应。"""
    return sorted(id_set)


def _parse_group_templates(value) -> dict:
    """解析群欢迎语模板 JSON。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception as e:
        logger.error(f"[group_welcome] 解析群模板失败: {e}")
        return {}


def _serialize_group_templates(templates: dict) -> str:
    """序列化群欢迎语模板。"""
    return json.dumps(templates, ensure_ascii=False)


def _normalize_welcome_mode(value, default: str = WELCOME_MODE_TEXT) -> str:
    """兼容中英文配置值，并将欢迎模式规范为 text/ai。"""
    aliases = {
        "text": WELCOME_MODE_TEXT,
        "fixed": WELCOME_MODE_TEXT,
        "固定欢迎词": WELCOME_MODE_TEXT,
        "ai": WELCOME_MODE_AI,
        "固定ai提示词": WELCOME_MODE_AI,
        "固定 AI 提示词": WELCOME_MODE_AI,
    }
    return aliases.get(str(value or "").strip(), default)


class GroupWelcomePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 锁初始化 (Python 3.10+ 安全)
        self._lock = asyncio.Lock()

        # 运行状态标记
        self._is_running = True

        # 【Fix #2】改为实例变量，避免热重载时状态残留
        self._global_cooldown = {}
        self._last_cleanup_time = 0

        # 配置加载
        self._enable_member_count: bool = config.get("enable_member_count", True)
        self._enable_private_rules: bool = config.get("enable_private_rules", False)
        self._enable_ai_welcome: bool = config.get("enable_ai_welcome", False)
        configured_mode = _normalize_welcome_mode(
            config.get("welcome_mode"),
            WELCOME_MODE_TEXT,
        )
        # AstrBot 更新 Schema 时会先补入 welcome_mode=text。旧配置若曾开启
        # enable_ai_welcome，需要在第一次加载 v3 时迁移成新模式并清除旧开关，
        # 避免用户之后主动选择 text 又被旧值覆盖。
        if self._enable_ai_welcome:
            self._welcome_mode = WELCOME_MODE_AI
            self._enable_ai_welcome = False
            self.config["welcome_mode"] = WELCOME_MODE_AI
            self.config["enable_ai_welcome"] = False
            self.config.save_config()
        else:
            self._welcome_mode = configured_mode
        self._enable_welcome_image = bool(
            config.get("enable_welcome_image", False)
        )

        self._whitelist: set = _parse_id_list(config.get("group_whitelist", []))
        self._blacklist: set = _parse_id_list(config.get("group_blacklist", []))

        # 迁移旧版字符串格式 → list，避免 UI 把字符串逐字符展开
        self._migrate_id_lists()

        self.data_dir = StarTools.get_data_dir()
        self.cooldown_file = self.data_dir / "cooldowns.json"

        # 加载持久化的冷却数据
        self._load_cooldowns()

        # 【Fix #1】保存 Task 引用，避免 GC 回收 & 支持 terminate() 主动取消
        self._register_task = asyncio.create_task(self._safe_register_handler())

    # ──────────────────────────────────────────
    # 生命周期管理
    # ──────────────────────────────────────────

    async def _safe_register_handler(self):
        """稳健的事件监听注册逻辑。"""
        max_retries = 15
        for _ in range(max_retries):
            if not self._is_running:
                return

            client = self._get_client()
            if client:
                try:
                    if hasattr(client, "on_notice"):

                        @client.on_notice("group_increase")
                        async def _group_increase_handler(event):
                            if not self._is_running:
                                return
                            await self._on_notice(event)

                        logger.info(
                            "[group_welcome] OneBot 11 入群事件监听已成功注册。"
                        )
                        return
                except Exception as e:
                    logger.error(f"[group_welcome] 注册监听失败: {e}")

            await asyncio.sleep(5)

        logger.warning("[group_welcome] 超时未找到 OneBot 适配器，插件功能可能受限。")

    async def terminate(self):
        """插件卸载回调。"""
        self._is_running = False
        # 【Fix #1】主动取消 Task，避免插件卸载后幽灵任务残留
        if self._register_task and not self._register_task.done():
            self._register_task.cancel()
            try:
                await self._register_task
            except asyncio.CancelledError:
                pass
        self._save_cooldowns()
        logger.info("[group_welcome] 插件已卸载，冷却数据已保存。")

    # ──────────────────────────────────────────
    # 冷却数据持久化
    # ──────────────────────────────────────────

    def _load_cooldowns(self):
        """从文件加载冷却数据。"""
        if not self.cooldown_file.exists():
            return
        try:
            with open(self.cooldown_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                count = 0
                for k, v in data.items():
                    if now - v < 86400:
                        self._global_cooldown[k] = v
                        count += 1
                logger.debug(f"[group_welcome] 已加载 {count} 条有效冷却记录。")
        except Exception as e:
            logger.warning(f"[group_welcome] 加载冷却文件失败: {e}")

    def _save_cooldowns(self):
        """保存冷却数据到文件。"""
        try:
            with open(self.cooldown_file, "w", encoding="utf-8") as f:
                json.dump(self._global_cooldown, f)
        except Exception as e:
            logger.warning(f"[group_welcome] 保存冷却数据失败: {e}")

    # ──────────────────────────────────────────
    # 核心逻辑
    # ──────────────────────────────────────────

    def _get_client(self):
        """
        使用鸭子类型判断适配器是否可用，
        避免依赖类名字符串匹配导致的脆弱性。
        只要适配器拥有 bot 对象且 bot 具备 api 属性，即视为有效客户端。
        """
        try:
            for adapter in self.context.platform_manager.get_insts():
                if (
                    hasattr(adapter, "bot")
                    and adapter.bot
                    and hasattr(adapter.bot, "api")
                ):
                    return adapter.bot
        except Exception as e:
            logger.debug(f"[group_welcome] _get_client 遍历适配器异常: {e}")
        return None

    def _clean_expired_cooldowns(self):
        now = time.time()
        if now - self._last_cleanup_time < 3600:
            return

        expired = [k for k, ts in self._global_cooldown.items() if now - ts > 86400]
        for key in expired:
            del self._global_cooldown[key]

        self._last_cleanup_time = now
        self._save_cooldowns()

    async def _on_notice(self, event):
        try:
            notice_type = event.get("notice_type")
            group_id = str(event.get("group_id", ""))
            user_id = str(event.get("user_id", ""))
        except Exception:
            return

        if notice_type != "group_increase" or not group_id or not user_id:
            return

        if not self._check_group_allowed(group_id):
            return

        self._clean_expired_cooldowns()

        key = f"{group_id}:{user_id}"
        cooldown = self.config.get("cooldown_seconds", 300)

        async with self._lock:
            now = time.time()
            if now - self._global_cooldown.get(key, 0) < cooldown:
                return
            self._global_cooldown[key] = now

        client = self._get_client()
        if not client:
            return

        name = await self._get_member_name(client, group_id, user_id)

        count_text = ""
        if self._enable_member_count:
            count = await self._get_group_member_count(client, group_id)
            if count:
                count_text = f"\n你是当前群里第 {count} 位成员！"

        # 从全局设置和每群设置中合并本次欢迎配置。
        group_settings = self._get_group_settings(group_id)
        template = group_settings["welcome_template"]

        try:
            welcome_text = template.format(name=name, count_text=count_text)
        except Exception as e:
            logger.warning(f"[group_welcome] 群 {group_id} 欢迎语模板格式错误: {e}")
            welcome_text = f"🎉 欢迎 {name} 加入本群！{count_text}"

        if group_settings["mode"] == WELCOME_MODE_AI:
            ai_text = await self._gen_ai_welcome(
                name=name,
                group_id=group_id,
                count_text=count_text,
                prompt_fmt=group_settings["ai_prompt"],
            )
            if ai_text:
                welcome_text = ai_text

        image_path = self._choose_welcome_image(group_settings)
        await self._send_group_welcome(
            client,
            group_id,
            user_id,
            welcome_text,
            image_path=image_path,
        )

        if self._enable_private_rules:
            await self._send_private_rules(client, user_id)

    def _check_group_allowed(self, group_id: str) -> bool:
        if self._whitelist:
            return group_id in self._whitelist
        return group_id not in self._blacklist

    def _get_welcome_template(self, group_id: str) -> str:
        return self._get_group_settings(group_id)["welcome_template"]

    def _get_group_settings(self, group_id: str) -> dict:
        """
        合并全局默认设置与指定群设置。

        group_templates 同时兼容旧格式：
        {"群号": "欢迎词"}

        以及新版格式：
        {
          "群号": {
            "mode": "text|ai",
            "welcome_template": "...",
            "ai_prompt": "...",
            "send_image": true,
            "images": ["图片文件名或上传后路径"]
          }
        }
        """
        default_template = self.config.get(
            "welcome_template",
            DEFAULT_WELCOME_TEMPLATE,
        )
        default_prompt = self.config.get("ai_welcome_prompt", DEFAULT_AI_PROMPT)
        settings = {
            "mode": self._welcome_mode,
            "welcome_template": default_template,
            "ai_prompt": default_prompt,
            "send_image": self._enable_welcome_image,
            "images": [],
        }

        raw_group_setting = self._load_group_templates().get(group_id)
        if isinstance(raw_group_setting, str):
            # v2.3.0 及更早版本：群号直接映射到固定欢迎词。
            settings["welcome_template"] = raw_group_setting
            return settings

        if not isinstance(raw_group_setting, dict):
            return settings

        settings["mode"] = _normalize_welcome_mode(
            raw_group_setting.get("mode"),
            settings["mode"],
        )
        welcome_template = raw_group_setting.get(
            "welcome_template",
            raw_group_setting.get("text"),
        )
        if isinstance(welcome_template, str) and welcome_template.strip():
            settings["welcome_template"] = welcome_template

        ai_prompt = raw_group_setting.get(
            "ai_prompt",
            raw_group_setting.get("prompt"),
        )
        if isinstance(ai_prompt, str) and ai_prompt.strip():
            settings["ai_prompt"] = ai_prompt

        if isinstance(raw_group_setting.get("send_image"), bool):
            settings["send_image"] = raw_group_setting["send_image"]

        images = raw_group_setting.get("images")
        if isinstance(images, list):
            settings["images"] = [
                str(item).strip() for item in images if str(item).strip()
            ]
        return settings

    def _choose_welcome_image(self, group_settings: dict) -> str | None:
        """从 WebUI 上传池中安全地选择一张可用图片。"""
        if not group_settings.get("send_image"):
            return None

        configured = self.config.get("welcome_images", [])
        if not isinstance(configured, list):
            return None

        requested = group_settings.get("images") or []
        requested_names = {Path(item).name for item in requested}
        candidates: list[Path] = []
        data_root = self.data_dir.resolve(strict=False)

        for raw_path in configured:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            if requested_names and Path(raw_path).name not in requested_names:
                continue

            image_path = (data_root / raw_path).resolve(strict=False)
            try:
                image_path.relative_to(data_root)
            except ValueError:
                logger.warning(
                    f"[group_welcome] 欢迎图片路径越界，已跳过: {raw_path}"
                )
                continue

            if (
                image_path.is_file()
                and image_path.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS
            ):
                candidates.append(image_path)

        if not candidates:
            if configured:
                logger.warning("[group_welcome] 未找到可用的欢迎图片，已仅发送文字。")
            return None
        return str(random.choice(candidates))

    async def _get_member_name(self, client, group_id: str, user_id: str) -> str:
        try:
            if not group_id.isdigit() or not user_id.isdigit():
                return user_id
            res = await client.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return res.get("card") or res.get("nickname") or user_id
        except Exception as e:
            logger.debug(f"[group_welcome] 获取成员信息失败: {e}")
            return user_id

    async def _get_group_member_count(self, client, group_id: str):
        try:
            if not group_id.isdigit():
                return None
            res = await client.api.call_action(
                "get_group_info", group_id=int(group_id), no_cache=True
            )
            return res.get("member_count")
        except Exception:
            return None

    async def _send_group_welcome(
        self,
        client,
        group_id: str,
        user_id: str,
        text: str,
        image_path: str | None = None,
    ):
        try:
            if not group_id.isdigit() or not user_id.isdigit():
                return
            message = [
                {"type": "at", "data": {"qq": user_id}},
                {"type": "text", "data": {"text": f" {text}"}},
            ]
            if image_path:
                message.append(
                    {
                        "type": "image",
                        "data": {
                            "file": image_path,
                            "summary": "[欢迎表情包]",
                        },
                    }
                )
            await client.api.call_action(
                "send_group_msg", group_id=int(group_id), message=message
            )
        except Exception as e:
            logger.error(f"[group_welcome] 发送欢迎语异常: {e}")

    async def _send_private_rules(self, client, user_id: str):
        await asyncio.sleep(2)
        rules = self.config.get("group_rules", "📋 请遵守群规，友善交流！")
        try:
            if not user_id.isdigit():
                return
            await client.api.call_action(
                "send_private_msg", user_id=int(user_id), message=rules
            )
        except Exception as e:
            logger.warning(f"[group_welcome] 私聊发送群规失败: {e}")

    async def _gen_ai_welcome(
        self,
        *,
        name: str,
        group_id: str,
        count_text: str,
        prompt_fmt: str,
    ) -> str:
        """
        使用指定的 LLM Provider 生成欢迎语。
        """
        try:
            provider_id = self.config.get("llm_provider", "")
            provider = None

            if provider_id:
                # 使用官方接口获取指定 Provider
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    logger.warning(
                        f"[group_welcome] 未找到指定的 LLM ({provider_id})，回退到默认模型。"
                    )
                    provider = self.context.get_using_provider()
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return ""

            final_prompt = (
                prompt_fmt.replace("{name}", name)
                .replace("{group_id}", group_id)
                .replace("{count_text}", count_text)
            )
            if not final_prompt.strip():
                final_prompt = DEFAULT_AI_PROMPT.replace("{name}", name)

            resp = await provider.text_chat(
                prompt=final_prompt,
                session_id=f"gw_{group_id}_{name}",
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[group_welcome] AI 生成失败: {e}")
            return ""

    # ──────────────────────────────────────────
    # 配置辅助
    # ──────────────────────────────────────────
    def _migrate_id_lists(self) -> None:
        """若白/黑名单配置仍是旧版字符串，一次性转换为 list 并写回，防止 UI 逐字符展开。"""
        changed = False
        for key, id_set in [
            ("group_whitelist", self._whitelist),
            ("group_blacklist", self._blacklist),
        ]:
            if isinstance(self.config.get(key), str):
                self.config[key] = _serialize_id_list(id_set)
                logger.info(
                    f"[group_welcome] 已将 {key} 从旧版字符串格式迁移为列表格式。"
                )
                changed = True
        if changed:
            self.config.save_config()

    def _save_switches(self):
        self.config["enable_member_count"] = self._enable_member_count
        self.config["enable_private_rules"] = self._enable_private_rules
        self.config["enable_ai_welcome"] = False
        self.config["welcome_mode"] = self._welcome_mode
        self.config["enable_welcome_image"] = self._enable_welcome_image
        self.config.save_config()

    def _save_lists(self):
        self.config["group_whitelist"] = _serialize_id_list(self._whitelist)
        self.config["group_blacklist"] = _serialize_id_list(self._blacklist)
        self.config.save_config()

    def _load_group_templates(self) -> dict:
        return _parse_group_templates(self.config.get("group_templates", "{}"))

    def _save_group_template(self, group_id: str, template: str):
        templates = self._load_group_templates()
        current = templates.get(group_id)
        if isinstance(current, dict):
            current["welcome_template"] = template
            templates[group_id] = current
        else:
            # 保持旧格式简洁，同时新版解析器可直接兼容。
            templates[group_id] = template
        self.config["group_templates"] = _serialize_group_templates(templates)
        self.config.save_config()

    def _del_group_template(self, group_id: str):
        templates = self._load_group_templates()
        if templates.pop(group_id, None):
            self.config["group_templates"] = _serialize_group_templates(templates)
            self.config.save_config()

    # ──────────────────────────────────────────
    # 指令
    # ──────────────────────────────────────────

    @filter.command_group("welcome")
    async def welcome(self, event: AstrMessageEvent):
        """指令入口。"""
        pass

    @welcome.command("count")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_count(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_member_count = True
            self._save_switches()
            yield event.plain_result("✅ 群人数统计已开启")
        elif action == "off":
            self._enable_member_count = False
            self._save_switches()
            yield event.plain_result("🔕 群人数统计已关闭")
        else:
            status = "开启" if self._enable_member_count else "关闭"
            yield event.plain_result(
                f"当前群人数统计：{status}\n用法：/welcome count on|off"
            )

    @welcome.command("rules")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_rules(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_private_rules = True
            self._save_switches()
            yield event.plain_result("✅ 私聊群规已开启")
        elif action == "off":
            self._enable_private_rules = False
            self._save_switches()
            yield event.plain_result("🔕 私聊群规已关闭")
        else:
            status = "开启" if self._enable_private_rules else "关闭"
            yield event.plain_result(
                f"当前私聊群规：{status}\n用法：/welcome rules on|off"
            )

    @welcome.command("ai")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toggle_ai(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        if action == "on":
            self._enable_ai_welcome = False
            self._welcome_mode = WELCOME_MODE_AI
            self._save_switches()
            yield event.plain_result("✅ 已切换为固定 AI 提示词模式")
        elif action == "off":
            self._enable_ai_welcome = False
            self._welcome_mode = WELCOME_MODE_TEXT
            self._save_switches()
            yield event.plain_result("🔕 已切换为固定欢迎词模式")
        else:
            status = (
                "固定 AI 提示词"
                if self._welcome_mode == WELCOME_MODE_AI
                else "固定欢迎词"
            )
            yield event.plain_result(
                f"当前欢迎模式：{status}\n用法：/welcome ai on|off"
            )

    @welcome.command("set")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_template(self, event: AstrMessageEvent):
        """设置欢迎语。"""
        raw_msg = event.message_obj.message_str.strip()
        parts = raw_msg.split(maxsplit=2)
        text_content = parts[2].strip() if len(parts) > 2 else ""
        text = text_content.replace("｛", "{").replace("｝", "}")

        current_group_id = (
            str(event.message_obj.group_id) if event.message_obj.group_id else ""
        )

        target_group_id = current_group_id
        final_content = text
        op_type = "set"

        if not current_group_id:
            sub_parts = text.split(maxsplit=1)
            first_word = sub_parts[0] if sub_parts else ""

            if first_word.isdigit():
                target_group_id = first_word
                remaining = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                if remaining in ["reset", "show"]:
                    op_type = remaining
                else:
                    final_content = remaining
            elif first_word in ["reset", "show"]:
                op_type = first_word
                if len(sub_parts) < 2:
                    yield event.plain_result(
                        f"❌ 私聊请指定群号，例如：/welcome set {first_word} 123456"
                    )
                    return
                target_group_id = sub_parts[1].strip()
                if not target_group_id.isdigit():
                    yield event.plain_result(f"❌ 群号格式错误：{target_group_id}")
                    return
            else:
                yield event.plain_result("❌ 私聊模式请先写群号或操作(reset/show)。")
                return
        else:
            if text in ["reset", "show"]:
                op_type = text

        if op_type == "reset":
            self._del_group_template(target_group_id)
            yield event.plain_result(f"✅ 群 {target_group_id} 已恢复默认。")
        elif op_type == "show":
            tmpl = self._get_welcome_template(target_group_id)
            yield event.plain_result(f"📋 群 {target_group_id} 当前欢迎语：\n{tmpl}")
        elif op_type == "set":
            if not final_content:
                yield event.plain_result("❌ 内容不能为空。")
                return
            self._save_group_template(target_group_id, final_content)
            yield event.plain_result(
                f"✅ 群 {target_group_id} 欢迎语已设置：\n{final_content}"
            )

    @welcome.command("wl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            content = "、".join(sorted(self._whitelist)) if self._whitelist else "空"
            yield event.plain_result(f"📋 白名单：{content}")
        elif action == "add" and group_id:
            self._whitelist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入白名单 {group_id}")
        elif action == "del" and group_id:
            self._whitelist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除白名单 {group_id}")
        else:
            yield event.plain_result(
                "用法：\n/welcome wl add <群号>\n/welcome wl del <群号>\n/welcome wl list"
            )

    @welcome.command("bl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(
        self, event: AstrMessageEvent, action: str = "", group_id: str = ""
    ):
        action = action.strip().lower()
        group_id = group_id.strip()

        if action == "list":
            content = "、".join(sorted(self._blacklist)) if self._blacklist else "空"
            yield event.plain_result(f"🚫 黑名单：{content}")
        elif action == "add" and group_id:
            self._blacklist.add(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已加入黑名单 {group_id}")
        elif action == "del" and group_id:
            self._blacklist.discard(group_id)
            self._save_lists()
            yield event.plain_result(f"✅ 已移除黑名单 {group_id}")
        else:
            yield event.plain_result(
                "用法：\n/welcome bl add <群号>\n/welcome bl del <群号>\n/welcome bl list"
            )

    @welcome.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_status(self, event: AstrMessageEvent, target_group: str = ""):
        target_group = target_group.strip()
        curr_gid = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        query_gid = target_group if target_group else curr_gid

        templates = self._load_group_templates()
        wl = "、".join(sorted(self._whitelist)) if self._whitelist else "（空）"
        bl = "、".join(sorted(self._blacklist)) if self._blacklist else "（空）"

        if query_gid:
            source = "群专属" if query_gid in templates else "全局默认"
            tip = f"📌 群 {query_gid} 欢迎语 [{source}]：\n{self._get_welcome_template(query_gid)}"
        else:
            tip = f"📌 已自定义群数：{len(templates)}\n💡 提示：私聊可带群号查询。"

        result = f"""📊 group_welcome 插件状态
{"─" * 24}
名单模式：{"白名单模式" if self._whitelist else "黑名单模式"}
白名单：{wl}
黑名单：{bl}
{"─" * 24}
群人数统计：{"✅ 开启" if self._enable_member_count else "🔕 关闭"}
私聊群规：{"✅ 开启" if self._enable_private_rules else "🔕 关闭"}
欢迎模式：{"🤖 固定 AI 提示词" if self._welcome_mode == WELCOME_MODE_AI else "📝 固定欢迎词"}
欢迎表情包：{"✅ 开启" if self._enable_welcome_image else "🔕 关闭"}
冷却时间：{self.config.get("cooldown_seconds", 300)}s
{"─" * 24}
{tip}"""
        yield event.plain_result(result)
