"""98堂（色花堂）论坛每日自动签到插件。

插件使用内嵌浏览器通过站点的 Cloudflare 校验与年龄确认页，使用账号、密码与
安全提问登录 Discuz 论坛，自动识别并解答旋转验证码后完成每日签到；另提供论坛
板块、帖子浏览与回复入口。
"""

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytz
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request
from fastapi import Response
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MessageType

from .forum import SehuaForumError, SehuaForumSession, list_driver_pids, terminate_pids
from .signer import QUESTION_CHOICES, SehuaSigner

# 模块级论坛会话注册表：插件被重装或重建实例时复用同一个浏览器会话，避免残留占用内存
_FORUM_SESSIONS: Dict[str, SehuaForumSession] = {}
_FORUM_SESSIONS_LOCK = threading.Lock()


# 缓存与并发控制
_IMAGE_CACHE_TTL = 1800
# 图片压缩后的最大宽度与高度
_IMAGE_MAX_WIDTH = 1080
_IMAGE_MAX_HEIGHT = 1440
# 小于该体积的图片无需压缩
_IMAGE_SHRINK_THRESHOLD = 150 * 1024
# 压缩结果缓存（地址 -> (时间, 字节, MIME)），避免重复占用 CPU
_IMAGE_BLOB_CACHE: Dict[str, tuple] = {}
_IMAGE_BLOB_LIMIT = 40
_IMAGE_BLOB_LOCK = threading.Lock()
# 同时抓取的图片数量上限，避免一次渲染把 CPU 与网络占满
_IMAGE_FETCH_SLOTS = threading.Semaphore(4)


class SehuaSignIn(_PluginBase):
    """98堂（色花堂）论坛每日自动签到插件。"""

    # 插件名称
    plugin_name = "98堂自动签到"
    # 插件描述
    plugin_desc = "色花堂/98堂 Discuz 论坛每日自动签到，并可在插件内浏览板块与帖子、参与回复。"
    # 插件图标
    plugin_icon = "world.png"
    # 插件版本
    plugin_version = "1.7.2"
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
    # 论坛浏览相关配置
    _forum_enabled: bool = True
    _forum_page_size: int = 20
    _forum_idle_minutes: int = 10
    _forum_allow_reply: bool = False
    _forum_show_images: bool = True
    _forum_max_images: int = 12

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
        self._forum_enabled = True
        self._forum_page_size = 20
        self._forum_idle_minutes = 10
        self._forum_allow_reply = False
        self._forum_show_images = True
        self._forum_max_images = 12

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
            self._forum_enabled = bool(config.get("forum_enabled", True))
            self._forum_page_size = max(5, min(50, int(config.get("forum_page_size") or 20)))
            self._forum_idle_minutes = max(1, int(config.get("forum_idle_minutes") or 10))
            self._forum_allow_reply = bool(config.get("forum_allow_reply"))
            self._forum_show_images = bool(config.get("forum_show_images", True))
            self._forum_max_images = max(1, min(50, int(config.get("forum_max_images") or 12)))

        # 回收上一次插件实例遗留的浏览器进程
        self._cleanup_orphan_browsers()
        # 每次初始化都把浏览位置复位到板块列表，保证打开插件先看到全部板块
        self.save_data(
            "nav",
            {"view": "index", "fid": "", "page": 1, "tid": "", "tpage": 1,
             "time": datetime.now().timestamp()},
        )

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
            },
            {
                "path": "/nav",
                "endpoint": self.api_nav,
                "methods": ["GET"],
                "summary": "切换论坛浏览视图",
                "description": "读取板块列表、帖子列表或帖子内容，并记录当前浏览位置。",
            },
            {
                "path": "/reply",
                "endpoint": self.api_reply,
                "methods": ["POST"],
                "summary": "在帖子中提交回复",
                "description": "以浏览器表单方式提交回复，返回可自动关闭的结果页。",
            },
            {
                "path": "/image",
                "endpoint": self.api_image,
                "methods": ["GET"],
                "summary": "代理读取帖子图片",
                "description": "站点图床校验 Referer，由插件带正确来源抓取图片后回传给浏览器。",
            },
        ]

    # ------------------------------------------------------------ 通用响应封装
    @staticmethod
    def _envelope(success: bool, message: str, data: Any = None) -> Dict[str, Any]:
        """构造插件 API 的标准响应体。

        MoviePilot 前端要求插件接口返回恰好包含 success、message、data 三个字段的
        响应体，多一个或少一个字段都会被判定为无效响应；因此统一在此处封装。

        :param success: 是否成功
        :param message: 结果说明
        :param data: 附加数据
        :return: 标准响应字典
        """
        return {"success": bool(success), "message": str(message or ""), "data": data}

    def api_signin(self) -> Dict[str, Any]:
        """手动触发一次签到并返回结果。

        :return: 执行结果字典
        """
        success, message = self.signin()
        return self._envelope(success, message)

    # ------------------------------------------------------------ 论坛浏览支持
    def _forum_signature(self) -> str:
        """计算当前论坛会话的配置指纹。

        :return: 配置指纹字符串
        """
        return "|".join(
            [
                self._site_url,
                self._username,
                self._proxy,
                str(self._headless),
                str(self._timeout),
                str(self._forum_idle_minutes),
            ]
        )

    def _forum_session(self) -> SehuaForumSession:
        """获取论坛浏览会话，配置变化时自动重建。

        :return: 论坛会话对象
        """
        signature = self._forum_signature()
        with _FORUM_SESSIONS_LOCK:
            # 配置变化后关闭并清理所有旧会话，保证只保留一个浏览器
            for old_signature in list(_FORUM_SESSIONS):
                if old_signature == signature:
                    continue
                logger.info("【98堂浏览】配置已变化，关闭旧的浏览会话")
                try:
                    _FORUM_SESSIONS.pop(old_signature).close()
                except Exception:  # noqa: BLE001 - 清理失败不影响新建会话
                    pass
            session = _FORUM_SESSIONS.get(signature)
            if session is None:
                session = SehuaForumSession(
                    site_url=self._site_url,
                    username=self._username,
                    password=self._password,
                    question_id=self._question_id,
                    answer=self._answer,
                    proxy=self._proxy,
                    headless=self._headless,
                    timeout=self._timeout,
                    idle_seconds=self._forum_idle_minutes * 60,
                    log=lambda message: logger.info(f"【98堂浏览】{message}"),
                    on_driver=self._remember_driver,
                )
                _FORUM_SESSIONS[signature] = session
            return session

    def _remember_driver(self, pids: Any) -> None:
        """记录本次启动的浏览器进程 PID，便于插件重载后回收。

        :param pids: 本次启动新增的 driver 进程 PID 列表
        """
        try:
            self.save_data("forum_driver_pids", [int(pid) for pid in pids])
        except Exception:  # noqa: BLE001 - 记录失败不影响浏览
            pass

    def _cleanup_orphan_browsers(self) -> None:
        """回收上次插件实例遗留的浏览器进程。

        插件重载会丢弃模块内的会话注册表，旧浏览器进程不再被引用；这里依据上次
        记录的 PID 将其关闭，避免长期占用内存。只会终止命令行确认为 playwright
        driver 的进程，并在后台线程中执行以免拖慢插件加载。
        """
        try:
            recorded = self.get_data("forum_driver_pids") or []
        except Exception:  # noqa: BLE001 - 读取失败时跳过清理
            recorded = []
        if not recorded:
            return
        alive = list_driver_pids()
        targets = {int(pid) for pid in recorded if int(pid) in alive}
        self.save_data("forum_driver_pids", [])
        if not targets:
            return
        logger.info(f"【98堂浏览】回收上次遗留的浏览器进程：{sorted(targets)}")

        def _worker() -> None:
            """在后台线程中回收遗留的浏览器进程。"""
            terminate_pids(targets)

        threading.Thread(
            target=_worker, name="SehuaSignInCleanup", daemon=True
        ).start()

    @staticmethod
    def close_forum_sessions() -> None:
        """关闭所有论坛浏览会话，释放浏览器进程。"""
        with _FORUM_SESSIONS_LOCK:
            for signature in list(_FORUM_SESSIONS):
                try:
                    _FORUM_SESSIONS.pop(signature).close()
                except Exception:  # noqa: BLE001 - 释放失败不影响插件卸载
                    pass

    def _nav_state(self) -> Dict[str, Any]:
        """读取当前浏览位置，长时间未操作时回到板块列表。

        :return: 浏览状态字典
        """
        state = self.get_data("nav") or {}
        view = str(state.get("view") or "index")
        # 距上次浏览超过一定时间视为新一次访问，回到板块列表
        last = float(state.get("time") or 0)
        if last and datetime.now().timestamp() - last > 600:
            view = "index"
        if view not in ("index", "forum", "thread"):
            view = "index"
        return {
            "view": view,
            "fid": str(state.get("fid") or ""),
            "page": max(1, int(state.get("page") or 1)),
            "tid": str(state.get("tid") or ""),
            "tpage": max(1, int(state.get("tpage") or 1)),
        }

    def _save_nav_state(self, state: Dict[str, Any]) -> None:
        """保存当前浏览位置。

        :param state: 浏览状态字典
        """
        payload = dict(state)
        payload["time"] = datetime.now().timestamp()
        self.save_data("nav", payload)

    def _cached_view(self, key: str) -> Optional[Dict[str, Any]]:
        """读取浏览缓存。

        :param key: 缓存键
        :return: 命中且未过期的缓存内容
        """
        cache = self.get_data("nav_cache") or {}
        if cache.get("key") != key:
            return None
        if datetime.now().timestamp() - float(cache.get("time") or 0) > 120:
            return None
        return cache.get("data")

    def _store_view(self, key: str, data: Dict[str, Any]) -> None:
        """写入浏览缓存。

        :param key: 缓存键
        :param data: 待缓存内容
        """
        self.save_data(
            "nav_cache",
            {"key": key, "time": datetime.now().timestamp(), "data": data},
        )

    @staticmethod
    def _cache_key(state: Dict[str, Any]) -> str:
        """计算浏览状态的缓存键。

        键中带有版本前缀，数据结构调整后旧缓存会自动失效。

        :param state: 浏览状态字典
        :return: 缓存键
        """
        return (
            f"v2:{state['view']}:{state['fid']}:{state['page']}"
            f":{state['tid']}:{state['tpage']}"
        )

    def _load_view(self, state: Dict[str, Any], refresh: bool = False) -> Dict[str, Any]:
        """按浏览状态读取数据，必要时回源站点。

        :param state: 浏览状态字典
        :param refresh: 是否强制回源
        :return: 视图数据
        """
        key = self._cache_key(state)
        if not refresh:
            cached = self._cached_view(key)
            if cached is not None:
                return cached
        session = self._forum_session()
        if state["view"] == "forum":
            data = session.list_threads(state["fid"], state["page"])
            data["view"] = "forum"
        elif state["view"] == "thread":
            data = session.get_thread(state["tid"], state["tpage"])
            data["view"] = "thread"
            if not data.get("fid"):
                data["fid"] = state["fid"]
        else:
            data = {"view": "index", "groups": session.list_boards()}
        self._store_view(key, data)
        return data

    def api_nav(
        self,
        view: str = "index",
        fid: str = "",
        page: int = 1,
        tid: str = "",
        tpage: int = 1,
        refresh: int = 0,
    ) -> Dict[str, Any]:
        """切换浏览视图并缓存目标页面内容。

        :param view: 视图类型，index、forum 或 thread
        :param fid: 板块 ID
        :param page: 帖子列表页码
        :param tid: 帖子 ID
        :param tpage: 帖子内容页码
        :param refresh: 是否强制重新读取
        :return: 执行结果
        """
        if not self._forum_enabled:
            return self._envelope(False, "论坛浏览未启用")
        state = {
            "view": view if view in ("index", "forum", "thread") else "index",
            "fid": str(fid or ""),
            "page": max(1, int(page or 1)),
            "tid": str(tid or ""),
            "tpage": max(1, int(tpage or 1)),
        }
        if state["view"] == "forum" and not state["fid"]:
            state["view"] = "index"
        if state["view"] == "thread" and not state["tid"]:
            state["view"] = "forum" if state["fid"] else "index"
        self._save_nav_state(state)
        try:
            view_data = self._load_view(state, refresh=bool(refresh))
            return self._envelope(True, "", view_data)
        except SehuaForumError as error:
            return self._envelope(False, str(error))
        except Exception as error:  # noqa: BLE001 - 统一返回可读错误
            logger.error(f"【98堂浏览】读取失败：{error}")
            return self._envelope(False, f"读取失败：{error}")

    # ------------------------------------------------------------ 图片代理
    def _images_html(self, images: List[str]) -> str:
        """生成楼层图片的 HTML 片段。

        图片地址带查询参数，属性中需要转义 & 等字符；同时加入加载失败提示，
        便于在网络异常时看清原因。

        :param images: 原始图片地址列表
        :return: HTML 片段
        """
        import html as html_lib

        parts: List[str] = []
        for image_url in images:
            src = html_lib.escape(self._image_proxy_url(image_url), quote=True)
            parts.append(
                "<img "
                f"src=\"{src}\" "
                "style=\"display:block;max-width:100%;height:auto;margin:8px 0;"
                "border-radius:6px;background:#f5f5f5\" "
                "loading=\"lazy\" "
                "referrerpolicy=\"no-referrer\" "
                "onerror=\"this.style.display='none';"
                "this.insertAdjacentHTML('afterend','<div style=\\'font-size:12px;color:#c62828\\'>"
                "图片加载失败，可点击「刷新」重试</div>')\">"
            )
        return "".join(parts)

    def _image_proxy_url(self, image_url: str) -> str:
        """把原图地址转换为插件图片代理地址。

        :param image_url: 站点正文中的原始图片地址
        :return: 指向插件代理接口的地址
        """
        from urllib.parse import quote

        plugin_id = self.__class__.__name__
        base = settings.MP_DOMAIN(f"/api/v1/plugin/{plugin_id}/image") or (
            f"/api/v1/plugin/{plugin_id}/image"
        )
        return f"{base}?url={quote(image_url, safe='')}&apikey={settings.API_TOKEN or ''}"

    @staticmethod
    def _unsafe_image_target(url: str) -> bool:
        """判断图片地址是否指向内网等不安全目标。

        :param url: 待校验的图片地址
        :return: 属于不安全目标时返回 True
        """
        from urllib.parse import urlsplit

        import ipaddress
        import socket

        try:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                return True
            host = parts.hostname or ""
            if not host:
                return True
            if host.lower() in ("localhost", "localhost.localdomain"):
                return True
            try:
                infos = socket.getaddrinfo(host, None)
            except OSError:
                return False
            for info in infos:
                address = info[4][0]
                try:
                    ip = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                ):
                    return True
            return False
        except Exception:  # noqa: BLE001 - 解析异常按不安全处理
            return True

    def api_image(self, url: str = "") -> Response:
        """代理抓取帖子图片并回传。

        站点图床会校验来源，缺失正确 Referer 时返回 403；由插件携带站点来源抓取，
        浏览器即可正常显示。

        :param url: 原始图片地址
        :return: 图片响应
        """
        target = (url or "").strip()
        if not target:
            return Response(status_code=400, content="缺少图片地址")
        if self._unsafe_image_target(target):
            logger.warn(f"【98堂浏览】已拒绝不安全的图片地址：{target[:120]}")
            return Response(status_code=400, content="图片地址不受支持")

        now = time.time()
        with _IMAGE_BLOB_LOCK:
            cached = _IMAGE_BLOB_CACHE.get(target)
        if cached and now - cached[0] < _IMAGE_CACHE_TTL:
            data, mime = cached[1], cached[2]
            return Response(
                content=data,
                media_type=mime,
                headers={"Cache-Control": "public, max-age=3600"},
            )

        headers = {
            "Referer": self._site_url + "/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        try:
            response = None
            # 图床的 IPv6 连接经常长时间挂起，因此收紧连接超时并多次重试，
            # 只要有一次连上 IPv4 就能很快取回图片。并发槽位限制避免一次渲染
            # 同时抓取过多图片，把 CPU 与出口带宽占满。
            timeout = httpx.Timeout(connect=3.0, read=25.0, write=10.0, pool=3.0)
            acquired = _IMAGE_FETCH_SLOTS.acquire(timeout=30)
            try:
                for attempt in range(4):
                    try:
                        with httpx.Client(
                            timeout=timeout, follow_redirects=True
                        ) as client:
                            response = client.get(target, headers=headers)
                        break
                    except Exception as error:  # noqa: BLE001 - 失败后重试
                        if attempt == 3:
                            raise
                        logger.warn(
                            f"【98堂浏览】图片抓取第 {attempt + 1} 次失败，重试：{error}"
                        )
            finally:
                if acquired:
                    _IMAGE_FETCH_SLOTS.release()
            if response is None or response.status_code != 200:
                code = response.status_code if response else "N/A"
                logger.warn(f"【98堂浏览】图片抓取失败 {code}：{target[:120]}")
                return Response(status_code=502, content="图片抓取失败")

            content_type = (response.headers.get("content-type") or "").lower()
            if not content_type.startswith("image/"):
                logger.warn(f"【98堂浏览】图片类型异常 {content_type}：{target[:120]}")
                return Response(status_code=415, content="返回内容不是图片")

            data = response.content
            if not data:
                return Response(status_code=502, content="图片内容为空")
            data, content_type = self._shrink_image(data, content_type)
            mime = content_type.split(";")[0].strip()
            with _IMAGE_BLOB_LOCK:
                if len(_IMAGE_BLOB_CACHE) >= _IMAGE_BLOB_LIMIT:
                    oldest = min(_IMAGE_BLOB_CACHE, key=lambda key: _IMAGE_BLOB_CACHE[key][0])
                    _IMAGE_BLOB_CACHE.pop(oldest, None)
                _IMAGE_BLOB_CACHE[target] = (now, data, mime)
            return Response(
                content=data,
                media_type=mime,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except Exception as error:  # noqa: BLE001 - 统一转为可读错误
            logger.error(f"【98堂浏览】图片代理异常：{error}")
            return Response(status_code=502, content="图片抓取异常")

    async def api_reply(self, request: Request) -> HTMLResponse:
        """提交帖子回复并返回可自动关闭的结果页。

        :param request: 请求对象，接受表单或 JSON 提交
        :return: 结果页面
        """
        form: Dict[str, Any] = {}
        try:
            form = dict(await request.form())
        except Exception:  # noqa: BLE001 - 非表单提交时按 JSON 解析
            form = {}
        if not form:
            try:
                form = dict(await request.json())
            except Exception:  # noqa: BLE001 - 无法解析时保持空字典
                form = {}

        tid = str(form.get("tid") or "")
        fid = str(form.get("fid") or "")
        message = str(form.get("message") or "")

        if not self._forum_enabled:
            return self._reply_page(False, "论坛浏览未启用")
        if not self._forum_allow_reply:
            return self._reply_page(False, "插件内回复未启用，请在插件配置中开启")
        if not tid or not message.strip():
            return self._reply_page(False, "缺少帖子 ID 或回复内容")

        try:
            success, result = self._forum_session().reply(tid, fid, message)
        except SehuaForumError as error:
            success, result = False, str(error)
        except Exception as error:  # noqa: BLE001 - 统一转为可读结果
            logger.error(f"【98堂浏览】回复失败：{error}")
            success, result = False, f"回复失败：{error}"

        if success:
            self.save_data(
                "reply_history",
                (
                    [
                        {
                            "time": datetime.now(pytz.timezone("Asia/Shanghai")).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "tid": tid,
                            "message": message[:100],
                            "success": True,
                        }
                    ]
                    + (self.get_data("reply_history") or [])
                )[:20],
            )
        else:
            logger.warn(f"【98堂浏览】回复未成功：{result}")
        return self._reply_page(success, result)

    @staticmethod
    def _reply_page(success: bool, message: str) -> HTMLResponse:
        """生成回复结果页面，供同页内嵌框展示。

        :param success: 是否成功
        :param message: 结果说明
        :return: 内嵌框结果页
        """
        color = "#2e7d32" if success else "#c62828"
        title = "回复成功" if success else "回复未成功"
        body = (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{title}</title></head>"
            f"<body style=\"margin:0;padding:12px 14px;"
            f"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
            f"line-height:1.6;color:#333;background:transparent\">"
            f"<div style='color:{color};font-weight:600;margin-bottom:4px'>{title}</div>"
            f"<div style='color:#555;font-size:13px'>{message}</div>"
            f"<div style='color:#888;font-size:12px;margin-top:8px'>"
            f"如需查看最新楼层，请点击上方「刷新」</div>"
            f"</body></html>"
        )
        return HTMLResponse(content=body)

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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "forum_enabled",
                                            "label": "启用论坛浏览",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "forum_allow_reply",
                                            "label": "允许插件内回复",
                                            "hint": "开启后可在帖子页面直接提交回复",
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
                                            "model": "forum_page_size",
                                            "label": "列表每页条数",
                                            "type": "number",
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
                                            "model": "forum_idle_minutes",
                                            "label": "会话空闲回收（分钟）",
                                            "type": "number",
                                            "hint": "浏览会话空闲超时后自动关闭浏览器",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "forum_show_images",
                                            "label": "显示帖子图片",
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
                                            "model": "forum_max_images",
                                            "label": "每层最多显示图片数",
                                            "type": "number",
                                            "hint": "图片由本机经插件转发，调小可省流量",
                                            "persistent-hint": True,
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
            "forum_enabled": True,
            "forum_allow_reply": False,
            "forum_page_size": 20,
            "forum_idle_minutes": 10,
            "forum_show_images": True,
            "forum_max_images": 12,
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
        ] + self._forum_page()

    # ------------------------------------------------------------ 论坛浏览页面
    def _forum_page(self) -> List[dict]:
        """构建论坛浏览页面元素。

        :return: 页面元素列表
        """
        if not self._forum_enabled:
            return []

        state = self._nav_state()
        try:
            data = self._load_view(state)
        except SehuaForumError as error:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "title": "论坛读取失败",
                        "text": str(error),
                    },
                }
            ]
        except Exception as error:  # noqa: BLE001 - 统一降级为可读提示
            logger.error(f"【98堂浏览】页面渲染失败：{error}")
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "title": "论坛读取失败",
                        "text": f"{error}",
                    },
                }
            ]

        view = data.get("view") or "index"
        header = [
            {
                "component": "VCard",
                "props": {"class": "mt-3", "variant": "tonal"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {
                            "class": "d-flex align-center",
                            "text": "论坛浏览",
                        },
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pt-0"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex flex-wrap ga-2"},
                                "content": self._forum_nav_buttons(view, state),
                            }
                        ],
                    },
                ],
            }
        ]

        if view == "index":
            groups = data.get("groups") or []
            items = self._group_items(groups)
            board_total = sum(len(group.get("boards") or []) for group in groups)
            body = [
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0 pb-0 text-caption text-medium-emphasis"},
                    "text": "按站点总目录分类，点击板块名称查看该板块的帖子列表",
                },
                {
                    "component": "VCardTitle",
                    "props": {"text": f"全部板块（{board_total}，共 {len(groups)} 个总目录）"},
                },
                {"component": "VList", "props": {"density": "compact"}, "content": items},
            ]
        elif view == "forum":
            threads = (data.get("threads") or [])[: self._forum_page_size]
            items = [
                {
                    "component": "VListItem",
                    "props": {
                        "title": thread.get("title", ""),
                        "subtitle": f"作者 {thread.get('author') or '未知'}　回复 {thread.get('replies')}　ID {thread.get('tid')}",
                        "append-icon": "mdi-message-text-outline",
                    },
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/nav",
                            "method": "get",
                            "params": {
                                "view": "thread",
                                "tid": thread.get("tid", ""),
                                "fid": state["fid"],
                                "tpage": 1,
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                }
                for thread in threads
            ]
            body = [
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0 pb-0 text-caption text-medium-emphasis"},
                    "text": "点击帖子标题查看内容；使用下方按钮翻页",
                },
                {
                    "component": "VCardTitle",
                    "props": {
                        "text": f"{data.get('board') or '板块'}　第 {data.get('page', 1)}/{data.get('max_page', 1)} 页"
                    },
                },
                {"component": "VList", "props": {"density": "compact"}, "content": items},
            ]
            body.extend(self._pager("forum", state, data))
        else:
            posts = (data.get("posts") or [])[: self._forum_page_size]
            body = [
                {
                    "component": "VCardTitle",
                    "props": {
                        "text": f"{data.get('title') or '帖子'}　第 {data.get('page', 1)}/{data.get('max_page', 1)} 页"
                    },
                }
            ]
            for index, post in enumerate(posts, start=1):
                body.append(self._post_card(index, post))
            body.extend(self._pager("thread", state, data))
            if self._forum_allow_reply:
                body.append(self._reply_form(state, data))

        return header + [
            {
                "component": "VCard",
                "props": {"class": "mt-3", "variant": "tonal"},
                "content": body,
            }
        ]

    def _post_card(self, index: int, post: Dict[str, Any]) -> dict:
        """构建一个楼层卡片，正文按需附带图片。

        :param index: 楼层序号
        :param post: 楼层数据
        :return: 楼层卡片元素
        """
        content: List[dict] = [
            {
                "component": "div",
                "props": {"style": "white-space: pre-wrap;"},
                "text": post.get("content")
                or ("（本层为图片内容）" if post.get("images") else "（无正文内容）"),
            }
        ]

        images = list(post.get("images") or []) if self._forum_show_images else []
        if images:
            shown = images[: self._forum_max_images]
            caption = f"图片 {len(images)} 张"
            if len(images) > len(shown):
                caption += f"（仅显示前 {len(shown)} 张）"
            content.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis mt-2"},
                    "text": caption,
                }
            )
            content.append(
                {
                    "component": "div",
                    "props": {},
                    "html": self._images_html(shown),
                }
            )

        return {
            "component": "VCard",
            "props": {"class": "ma-2", "variant": "outlined"},
            "content": [
                {
                    "component": "VCardSubtitle",
                    "props": {
                        "text": f"{index}. {post.get('author') or '匿名'}　{post.get('time') or ''}"
                    },
                },
                {
                    "component": "VCardText",
                    "props": {"class": "text-body-2"},
                    "content": content,
                },
            ],
        }

    def _nav_event(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """构建切换浏览视图的点击事件，并附带插件 API 鉴权参数。

        :param params: 视图切换参数
        :return: events 定义中的 click 事件
        """
        payload = dict(params)
        # 页面渲染器直接以查询参数调用插件 API，需要显式带上 apikey 才能通过鉴权
        payload["apikey"] = settings.API_TOKEN
        return {
            "api": f"plugin/{self.__class__.__name__}/nav",
            "method": "get",
            "params": payload,
        }

    def _group_items(self, groups: List[Dict[str, Any]]) -> List[dict]:
        """按站点总目录构建板块列表元素。

        :param groups: 分组数据，元素包含 group 名称与 boards 列表
        :return: 列表元素，包含分组标题与各板块项
        """
        items: List[dict] = []
        for group in groups:
            items.append(
                {
                    "component": "VListSubheader",
                    "props": {"class": "text-subtitle-2 font-weight-bold text-primary"},
                    "text": group.get("group") or "其它",
                }
            )
            for board in group.get("boards") or []:
                items.append(
                    {
                        "component": "VListItem",
                        "props": {
                            "title": board.get("name", ""),
                            "append-icon": "mdi-chevron-right",
                        },
                        "events": {
                            "click": self._nav_event(
                                {"view": "forum", "fid": board.get("fid", ""), "page": 1}
                            )
                        },
                    }
                )
        return items

    def _forum_nav_buttons(self, view: str, state: Dict[str, Any]) -> List[dict]:
        """构建论坛浏览顶部操作按钮。

        :param view: 当前视图
        :param state: 浏览状态
        :return: 按钮元素列表
        """
        buttons: List[dict] = []

        def button(text: str, params: Dict[str, Any], icon: str = "") -> dict:
            """生成一个带事件绑定的按钮。

            :param text: 按钮文本
            :param params: 事件参数
            :param icon: 图标名称
            :return: 按钮元素
            """
            props: Dict[str, Any] = {"variant": "tonal", "size": "small"}
            if icon:
                props["prepend-icon"] = icon
            return {
                "component": "VBtn",
                "props": props,
                "text": text,
                "events": {"click": self._nav_event(params)},
            }

        if view != "index":
            target = {"view": "index"} if view == "forum" else {
                "view": "forum",
                "fid": state["fid"],
                "page": state["page"],
            }
            buttons.append(button("返回上一级", target, "mdi-arrow-left"))
        if view == "thread":
            buttons.append(
                button("返回板块帖子列表", {"view": "forum", "fid": state["fid"], "page": state["page"]},
                       "mdi-format-list-bulleted")
            )
        if view != "index":
            buttons.append(button("全部板块", {"view": "index"}, "mdi-view-grid-outline"))
        buttons.append(button("刷新", {"view": view, "fid": state["fid"], "tid": state["tid"],
                                       "page": state["page"], "tpage": state["tpage"],
                                       "refresh": 1}, "mdi-refresh"))
        return buttons

    def _pager(self, mode: str, state: Dict[str, Any], data: Dict[str, Any]) -> List[dict]:
        """构建翻页按钮。

        :param mode: forum 或 thread
        :param state: 浏览状态
        :param data: 当前视图数据
        :return: 翻页元素列表
        """
        current = int(data.get("page") or 1)
        max_page = max(1, int(data.get("max_page") or 1))
        if max_page <= 1:
            return []

        def pager_button(text: str, target_page: int) -> dict:
            """生成翻页按钮。

            :param text: 按钮文本
            :param target_page: 目标页码
            :return: 按钮元素
            """
            if mode == "forum":
                params: Dict[str, Any] = {
                    "view": "forum",
                    "fid": state["fid"],
                    "page": target_page,
                }
            else:
                params = {
                    "view": "thread",
                    "tid": state["tid"],
                    "fid": state["fid"],
                    "tpage": target_page,
                }
            return {
                "component": "VBtn",
                "props": {"variant": "text", "size": "small"},
                "text": text,
                "events": {"click": self._nav_event(params)},
            }

        controls: List[dict] = []
        if current > 1:
            controls.append(pager_button("上一页", current - 1))
        controls.append(
            {
                "component": "span",
                "props": {"class": "px-2 text-caption align-self-center"},
                "text": f"{current} / {max_page}",
            }
        )
        if current < max_page:
            controls.append(pager_button("下一页", current + 1))
        return [
            {
                "component": "VCardText",
                "props": {"class": "d-flex align-center"},
                "content": controls,
            }
        ]

    @staticmethod
    def _shrink_image(data: bytes, content_type: str) -> Tuple[bytes, str]:
        """压缩图片，降低浏览时的传输与解码开销。

        图床原图常有 1MB 以上，一次渲染多张会让页面长时间空白，因此在服务端先缩到
        适合阅读的尺寸并转成 JPEG。任何异常都退回原图。

        :param data: 原始图片字节
        :param content_type: 原始 MIME 类型
        :return: 处理后的字节与 MIME 类型
        """
        if len(data) <= _IMAGE_SHRINK_THRESHOLD:
            return data, content_type
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image = image.convert("RGB")
                image.thumbnail((_IMAGE_MAX_WIDTH, _IMAGE_MAX_HEIGHT), Image.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=82, optimize=True)
            shrunk = buffer.getvalue()
            if 0 < len(shrunk) < len(data):
                return shrunk, "image/jpeg"
        except Exception as error:  # noqa: BLE001 - 压缩失败时保留原图
            logger.warn(f"【98堂浏览】图片压缩失败，改为返回原图：{error}")
        return data, content_type

    def _reply_form(self, state: Dict[str, Any], data: Dict[str, Any]) -> dict:
        """构建回复表单，使用同页内嵌框提交，避免跳转外部浏览器。

        :param state: 浏览状态
        :param data: 当前帖子数据
        :return: 表单元素
        """
        plugin_id = self.__class__.__name__
        token = settings.API_TOKEN or ""
        domain = settings.MP_DOMAIN(f"/api/v1/plugin/{plugin_id}/reply") or (
            f"/api/v1/plugin/{plugin_id}/reply"
        )
        action = f"{domain}?apikey={token}"
        fid = data.get("fid") or state.get("fid") or ""
        frame_name = f"sehua-reply-{data.get('tid') or state['tid']}"
        html = (
            f"<form method='post' target='{frame_name}' "
            f"action=\"{action}\" style='margin-top:8px'>"
            f"<input type='hidden' name='tid' value='{data.get('tid') or state['tid']}'>"
            f"<input type='hidden' name='fid' value='{fid}'>"
            "<textarea name='message' rows='4' required "
            "style='width:100%;box-sizing:border-box;padding:8px;border:1px solid #ccc;"
            "border-radius:6px;font-family:inherit;font-size:14px' "
            "placeholder='输入回复内容后点击发送'></textarea>"
            "<button type='submit' style='margin-top:8px;padding:8px 18px;border:none;"
            "border-radius:6px;background:#1976d2;color:#fff;font-size:14px;cursor:pointer'>"
            "发送回复</button>"
            "<button type='reset' style='margin:8px 0 0 8px;padding:8px 14px;border:1px solid #ccc;"
            "border-radius:6px;background:transparent;font-size:14px;cursor:pointer'>清空</button>"
            "</form>"
            f"<iframe name='{frame_name}' title='回复结果' "
            "style='width:100%;height:118px;border:1px dashed #d0d0d0;border-radius:6px;"
            "margin-top:8px;background:transparent'></iframe>"
        )
        return {
            "component": "VCardText",
            "content": [
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis mb-1"},
                    "text": "发表回复：结果会显示在下方框内，发送后点击上方「刷新」查看最新楼层",
                },
                {"component": "div", "props": {}, "html": html},
            ],
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._running = False
        self.close_forum_sessions()

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
