"""98堂（色花堂）论坛每日自动签到插件。

插件使用内嵌浏览器通过站点的 Cloudflare 校验与年龄确认页，使用账号、密码与
安全提问登录 Discuz 论坛，自动识别并解答旋转验证码后完成每日签到。
"""

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MessageType

from .signer import QUESTION_CHOICES, SehuaSigner


class SehuaSignIn(_PluginBase):
    """98堂（色花堂）论坛每日自动签到插件。"""

    # 插件名称
    plugin_name = "98堂自动签到"
    # 插件描述
    plugin_desc = "色花堂/98堂 Discuz 论坛每日自动签到，内嵌浏览器通过 Cloudflare 与年龄确认页，自动解答旋转验证码。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "local"
    # 插件配置项ID前缀
    plugin_config_prefix = "sehuasignin_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 1

    # 私有配置
    _enabled: bool = False
    _notify: bool = False
    _onlyonce: bool = False
    _cron: str = "0 8 * * *"
    _site_url: str = "https://sehuatang.org"
    _proxy: str = ""
    _username: str = ""
    _password: str = ""
    _question_id: str = "7"
    _answer: str = ""
    _captcha_retry: int = 8
    _headless: bool = True
    _timeout: int = 60

    # 运行状态
    _running: bool = False
    _lock: Optional[threading.Lock] = None

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置字典
        """
        self.stop_service()
        self._running = False
        if self._lock is None:
            self._lock = threading.Lock()

        self._enabled = False
        self._notify = False
        self._onlyonce = False
        self._cron = "0 8 * * *"
        self._site_url = "https://sehuatang.org"
        self._proxy = ""
        self._username = ""
        self._password = ""
        self._question_id = "7"
        self._answer = ""
        self._captcha_retry = 8
        self._headless = True
        self._timeout = 60

        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._cron = str(config.get("cron") or "0 8 * * *")
            self._site_url = str(config.get("site_url") or "https://sehuatang.org").rstrip("/")
            self._proxy = str(config.get("proxy") or "")
            self._username = str(config.get("username") or "")
            self._password = str(config.get("password") or "")
            self._question_id = str(config.get("question_id") or "0")
            self._answer = str(config.get("answer") or "")
            self._captcha_retry = int(config.get("captcha_retry") or 8)
            self._headless = bool(config.get("headless", True))
            self._timeout = int(config.get("timeout") or 60)

        if self._onlyonce and self._enabled:
            logger.info("【98堂签到】立即运行一次")
            self._onlyonce = False
            config = dict(config or {})
            config["onlyonce"] = False
            self.update_config(config)
            threading.Thread(target=self.signin, name="SehuaSignInOnce", daemon=True).start()

    def get_state(self) -> bool:
        """获取插件启用状态。

        :return: 是否已启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。

        :return: 命令定义列表
        """
        return [
            {
                "cmd": "/sehua_signin",
                "event": "PluginAction",
                "desc": "立即执行 98堂签到",
                "category": "签到",
                "data": {"action": "sehua_signin"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。

        :return: API 定义列表
        """
        return [
            {
                "path": "/signin",
                "endpoint": self.api_signin,
                "methods": ["GET"],
                "summary": "立即执行一次 98堂签到",
                "description": "手动触发一次签到流程，返回执行结果。",
            }
        ]

    def api_signin(self) -> Dict[str, Any]:
        """手动触发一次签到并返回结果。

        :return: 执行结果字典
        """
        success, message = self.signin()
        return {"success": success, "message": message}

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件的定时服务列表。

        :return: 定时服务定义列表
        """
        if self._enabled and self._cron:
            try:
                return [
                    {
                        "id": f"{self.__class__.__name__}_signin",
                        "name": "98堂自动签到",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.signin,
                        "kwargs": {},
                    }
                ]
            except Exception as error:  # noqa: BLE001 - 非法 cron 表达式时给出可见日志
                logger.error(f"【98堂签到】定时任务表达式无效：{error}")
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: ``(表单结构, 默认配置)``
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "headless", "label": "无头浏览器"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，如 0 8 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "site_url",
                                            "label": "站点地址",
                                            "placeholder": "https://sehuatang.org",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "username", "label": "账号"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "question_id",
                                            "label": "安全提问",
                                            "items": QUESTION_CHOICES,
                                            "hint": "账号设置的安全提问，未设置请选择不设置",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "answer", "label": "安全提问答案"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "proxy",
                                            "label": "代理地址",
                                            "placeholder": "http://127.0.0.1:7890，留空则不使用代理",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "captcha_retry",
                                            "label": "验证码重试次数",
                                            "type": "number",
                                            "hint": "遇到非旋转类型验证码时的重试上限",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "站点需经代理访问；签到需要浏览器通过 Cloudflare 校验与年龄确认页，单次执行约需 1 分钟。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "账号密码仅保存在本机插件配置中，请勿在公共环境使用。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "headless": True,
            "cron": "0 8 * * *",
            "site_url": "https://sehuatang.org",
            "username": "",
            "password": "",
            "question_id": "7",
            "answer": "",
            "proxy": "",
            "captcha_retry": 8,
            "timeout": 60,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件运行状态页面。

        :return: 页面配置
        """
        status = self.get_data("status") or {}
        history = self.get_data("history") or []

        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "插件未启用，启用后每日按设定周期自动签到。",
                    },
                }
            ]

        last_time = status.get("time") or "尚未执行"
        last_result = status.get("message") or "—"
        last_state = "成功" if status.get("success") else ("失败" if status else "—")
        rows = [
            {
                "component": "VListItem",
                "props": {
                    "title": f"{item.get('time', '')}  {'成功' if item.get('success') else '失败'}",
                    "subtitle": item.get("message", ""),
                },
            }
            for item in history[:10]
        ]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if status.get("success") else "warning",
                    "variant": "tonal",
                    "title": f"最近一次执行：{last_state}",
                    "text": f"{last_time}　{last_result}",
                },
            },
            {
                "component": "VCard",
                "props": {"class": "mt-3", "variant": "tonal"},
                "content": [
                    {"component": "VCardTitle", "props": {"text": "执行记录"}},
                    {
                        "component": "VList",
                        "props": {"density": "compact"},
                        "content": rows,
                    },
                ],
            },
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._running = False

    def signin(self) -> Tuple[bool, str]:
        """执行一次签到并记录结果。

        :return: ``(是否成功, 结果说明)``
        """
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            return False, "已有签到任务正在执行"
        try:
            self._running = True
            signer = SehuaSigner(
                site_url=self._site_url,
                username=self._username,
                password=self._password,
                question_id=self._question_id,
                answer=self._answer,
                proxy=self._proxy,
                captcha_retry=self._captcha_retry,
                headless=self._headless,
                timeout=self._timeout,
                log=lambda message: logger.info(f"【98堂签到】{message}"),
            )
            success, message = signer.run()
        finally:
            self._running = False
            self._lock.release()

        now = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"【98堂签到】执行结果：{'成功' if success else '失败'} {message}")
        self.save_data("status", {"time": now, "success": success, "message": message})

        history = list(self.get_data("history") or [])
        history.insert(0, {"time": now, "success": success, "message": message})
        self.save_data("history", history[:30])

        if self._notify:
            self.post_message(
                mtype=MessageType.Plugin,
                title="【98堂自动签到】成功" if success else "【98堂自动签到】失败",
                text=f"时间：{now}\n结果：{message}",
            )
        return success, message
