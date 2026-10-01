"""98堂（色花堂）论坛浏览与回复实现。

复用同一个内嵌浏览器会话完成登录，之后以该会话读取板块列表、帖子列表与帖子
内容，并按需提交回复。会话在空闲一段时间后自动回收，避免长期占用内存。
"""

import html as html_lib
import importlib
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# 站点年龄确认页的进入按钮文案
AGE_GATE_TEXTS = ("请点此进入", "please click here", "点击进入")
# 单篇帖子正文保留的最大字符数
MAX_POST_CHARS = 4000


class SehuaForumError(Exception):
    """论坛操作中可直接呈现给用户的异常。"""


def html_to_text(raw: str) -> str:
    """把帖子正文 HTML 转换为适合展示的纯文本。

    :param raw: 帖子正文 HTML 片段
    :return: 去除标签并保留换行的纯文本
    """
    if not raw:
        return ""
    text = raw
    # 图片等替换为占位标记，便于用户判断内容构成
    text = re.sub(r"<img[^>]*>", " [图片] ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|li|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > MAX_POST_CHARS:
        text = text[:MAX_POST_CHARS] + "……（内容过长已截断）"
    return text


class SehuaForumSession:
    """可复用的论坛会话，负责登录、浏览与回复。"""

    # 登录接口
    LOGIN_URL = "/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&inajax=1"
    # 回复提交接口模板
    REPLY_URL = "/forum.php?mod=post&action=reply&fid={fid}&tid={tid}&replysubmit=yes&infloat=yes&handlekey=fastpost&inajax=1"

    def __init__(
        self,
        site_url: str,
        username: str,
        password: str,
        question_id: str = "0",
        answer: str = "",
        proxy: str = "",
        headless: bool = True,
        timeout: int = 60,
        idle_seconds: int = 600,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        """初始化论坛会话参数。

        :param site_url: 站点地址
        :param username: 登录用户名
        :param password: 登录密码
        :param question_id: 安全提问编号
        :param answer: 安全提问答案
        :param proxy: 代理地址
        :param headless: 是否使用无头浏览器
        :param timeout: 页面超时秒数
        :param idle_seconds: 会话空闲回收秒数
        :param log: 日志输出回调
        """
        self.site_url = (site_url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.question_id = str(question_id or "0").strip()
        self.answer = (answer or "").strip()
        self.proxy = (proxy or "").strip()
        self.headless = bool(headless)
        self.timeout = max(20, int(timeout or 60))
        self.idle_seconds = max(60, int(idle_seconds or 600))
        self._log = log

        self._lock = threading.RLock()
        self._context: Any = None
        self._page: Any = None
        self._logged_in = False
        self._last_used = 0.0
        self._reaper: Optional[threading.Thread] = None
        self._closed = True

    # ------------------------------------------------------------------ 基础
    def _log_info(self, message: str) -> None:
        """输出日志。

        :param message: 日志内容
        """
        if self._log:
            self._log(message)

    def _ensure_reaper(self) -> None:
        """在需要时启动空闲回收线程。"""
        if self._reaper and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(
            target=self._reap_loop, name="SehuaForumReaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        """周期检查并回收空闲过久的会话。"""
        while True:
            time.sleep(60)
            try:
                with self._lock:
                    if self._closed or self._context is None:
                        return
                    if time.monotonic() - self._last_used > self.idle_seconds:
                        self._log_info("浏览会话空闲超时，已回收浏览器")
                        self._close_locked()
                        return
            except Exception:  # noqa: BLE001 - 回收异常不应影响主流程
                return

    def _close_locked(self) -> None:
        """在已持锁的情况下关闭浏览器会话。"""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续重建
            pass
        self._context = None
        self._page = None
        self._logged_in = False
        self._closed = True

    def close(self) -> None:
        """关闭浏览器会话并释放资源。"""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------ 会话
    def _launch_locked(self) -> None:
        """在已持锁的情况下启动浏览器并完成登录。"""
        if self._context is not None and self._logged_in:
            return
        self._close_locked()

        cloakbrowser = importlib.import_module("cloakbrowser")
        self._log_info("启动浏览器并登录论坛")
        context = cloakbrowser.launch_context(
            headless=self.headless, proxy=self.proxy or None
        )
        page = context.new_page()
        page.set_default_timeout(self.timeout * 1000)
        self._context = context
        self._page = page
        self._logged_in = False
        self._closed = False

        self._pass_age_gate(page)
        self._login(page)
        self._logged_in = True

    def _pass_age_gate(self, page: Any) -> None:
        """通过站点年龄确认页。

        :param page: 浏览器页面
        """
        page.goto(self.site_url + "/", wait_until="domcontentloaded", timeout=self.timeout * 1000)
        time.sleep(4)
        for text in AGE_GATE_TEXTS:
            try:
                target = page.get_by_text(text, exact=False).first
                if target and target.count() > 0:
                    target.click(timeout=8000)
                    time.sleep(3)
                    return
            except Exception:  # noqa: BLE001 - 未出现年龄门属正常情况
                continue

    def _login(self, page: Any) -> None:
        """使用账号密码与安全提问登录论坛。

        :param page: 浏览器页面
        """
        page.goto(
            self.site_url + "/member.php?mod=logging&action=login",
            wait_until="domcontentloaded",
            timeout=self.timeout * 1000,
        )
        time.sleep(2)
        matched = re.search(r'name="formhash"\s+value="([^"]+)"', page.content())
        if not matched:
            raise SehuaForumError("登录页未取到 formhash，站点结构可能已变化")
        script = """
        async ([base, formhash, user, pwd, qid, ans]) => {
          const body = new URLSearchParams({
            formhash: formhash, referer: base + '/', username: user, password: pwd,
            questionid: qid, answer: ans, cookietime: '2592000'
          });
          const response = await fetch(base + '/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&inajax=1', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'},
            body: body.toString(),
            credentials: 'include'
          });
          return (await response.text()).slice(0, 600);
        }
        """
        text = page.evaluate(
            script,
            [self.site_url, matched.group(1), self.username, self.password, self.question_id or "0", self.answer],
        )
        if "欢迎您回来" in text or "succeedhandle_login" in text:
            return
        if "安全提问" in text or "loginperm" in text:
            raise SehuaForumError("登录失败：安全提问编号或答案不正确")
        if "密码" in text:
            raise SehuaForumError("登录失败：账号或密码不正确")
        raise SehuaForumError(f"登录失败：{text[:100]}")

    def _fetch_locked(self, url: str, retry: bool = True) -> str:
        """在已持锁的情况下读取页面 HTML。

        :param url: 目标地址
        :param retry: 失败时是否重建会话后重试一次
        :return: 页面 HTML
        """
        try:
            self._launch_locked()
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            time.sleep(2)
            return self._page.content()
        except Exception as error:  # noqa: BLE001 - 统一转为可重试流程
            if not retry:
                raise SehuaForumError(f"读取页面失败：{error}") from error
            self._log_info(f"读取页面异常，重建会话后重试：{type(error).__name__}")
            self._close_locked()
            time.sleep(2)
            return self._fetch_locked(url, retry=False)

    def _with_session(self, action: Callable[[Any], Any]) -> Any:
        """在会话锁内执行一次浏览器操作。

        :param action: 接收页面对象并返回结果的可调用对象
        :return: action 的返回值
        """
        with self._lock:
            self._ensure_reaper()
            try:
                self._launch_locked()
                return action(self._page)
            except SehuaForumError:
                raise
            except Exception as error:  # noqa: BLE001 - 失败后重建会话重试一次
                self._log_info(f"论坛操作异常，重建会话后重试：{type(error).__name__}")
                self._close_locked()
                time.sleep(2)
                self._launch_locked()
                return action(self._page)
            finally:
                self._last_used = time.monotonic()

    def _goto_html(self, page: Any, url: str) -> str:
        """在当前会话中打开页面并返回 HTML。

        :param page: 浏览器页面
        :param url: 目标地址
        :return: 页面 HTML
        """
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        time.sleep(2)
        return page.content()

    # ------------------------------------------------------------------ 业务
    def list_boards(self) -> List[Dict[str, str]]:
        """获取论坛板块列表。

        :return: 板块列表，元素包含 fid 与名称
        """
        html = self._with_session(lambda page: self._goto_html(page, self.site_url + "/forum.php"))
        boards: Dict[str, str] = {}
        for fid, name in re.findall(
            r'forumdisplay&(?:amp;)?fid=(\d+)"[^>]*>([^<]{1,60})</a>', html
        ):
            name = name.strip()
            if name and fid not in boards:
                boards[fid] = name
        return [{"fid": fid, "name": name} for fid, name in boards.items()]

    def list_threads(self, fid: str, page_no: int = 1) -> Dict[str, Any]:
        """获取指定板块的帖子列表。

        :param fid: 板块 ID
        :param page_no: 页码，从 1 开始
        :return: 含板块名、帖子列表与最大页码的字典
        """
        page_no = max(1, int(page_no or 1))
        url = f"{self.site_url}/forum.php?mod=forumdisplay&fid={fid}&page={page_no}"
        html = self._with_session(lambda page: self._goto_html(page, url))

        board_name = ""
        matched = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if matched:
            plain = re.sub(r"<[^>]+>", " ", matched.group(1))
            plain = html_lib.unescape(plain)
            # <h1> 内含站点统计信息，只保留首行板块名称
            board_name = plain.strip().split("\n")[0]
            board_name = re.split(r"今日[:：]|主题[:：]|排名[:：]", board_name)[0].strip()

        if "抱歉，指定的主题不存在或已被删除或正在被审核" in html:
            raise SehuaForumError("板块不存在或无权访问")

        threads: List[Dict[str, str]] = []
        seen: set = set()
        # 按行块解析，避免标题与作者错位
        for block in re.findall(r'<tbody id="normalthread_(\d+)">(.*?)</tbody>', html, re.S):
            tid, body = block
            if tid in seen:
                continue
            seen.add(tid)
            title_matched = re.search(r'class="s xst"[^>]*>(.*?)</a>', body, re.S)
            title = re.sub(r"<[^>]+>", "", title_matched.group(1)).strip() if title_matched else ""
            if not title:
                continue
            author_matched = re.search(r'<cite>\s*<a[^>]*>([^<]{1,30})</a>', body)
            author = author_matched.group(1).strip() if author_matched else ""
            reply_matched = re.search(r'<td class="num">.*?<a[^>]*class="xi2"[^>]*>(\d+)</a>', body, re.S)
            replies = reply_matched.group(1) if reply_matched else "0"
            threads.append(
                {"tid": tid, "title": title[:80], "author": author, "replies": replies}
            )

        page_numbers = [int(item) for item in re.findall(rf"fid={fid}&(?:amp;)?page=(\d+)", html)]
        return {
            "board": board_name or f"板块 {fid}",
            "threads": threads,
            "page": page_no,
            "max_page": max(page_numbers) if page_numbers else 1,
        }

    def get_thread(self, tid: str, page_no: int = 1) -> Dict[str, Any]:
        """获取帖子内容。

        :param tid: 帖子 ID
        :param page_no: 页码，从 1 开始
        :return: 含标题、楼层列表与最大页码的字典
        """
        page_no = max(1, int(page_no or 1))
        url = f"{self.site_url}/forum.php?mod=viewthread&tid={tid}&page={page_no}"
        html = self._with_session(lambda page: self._goto_html(page, url))

        if "抱歉，指定的主题不存在或已被删除或正在被审核" in html:
            raise SehuaForumError("帖子不存在或已被删除")
        if "您需要登录后才能查看" in html or "请先登录" in html:
            raise SehuaForumError("登录状态失效，请稍后重试")

        subject_matched = re.search(r'id="thread_subject"[^>]*>([^<]+)<', html)
        title = subject_matched.group(1).strip() if subject_matched else ""
        if not title:
            matched = re.search(r"<title>([^<]+)</title>", html)
            title = matched.group(1).split(" - ")[0].strip() if matched else f"帖子 {tid}"

        fid_matched = re.search(r"action=reply&(?:amp;)?fid=(\d+)", html)
        fid = fid_matched.group(1) if fid_matched else ""

        posts: List[Dict[str, str]] = []
        for matched in re.finditer(r'id="postmessage_(\d+)"[^>]*>(.*?)</td>', html, re.S):
            pid, content = matched.group(1), matched.group(2)
            before = html[max(0, matched.start() - 12000): matched.start()]
            authors = re.findall(r'class="xw1"[^>]*>([^<]{1,30})</a>', before)
            author = authors[-1].strip() if authors else ""
            time_matched = re.search(
                rf'<em id="authorposton{pid}"[^>]*>(.*?)</em>', html, re.S
            )
            posted = ""
            if time_matched:
                stamp = re.sub(r"<[^>]+>", " ", time_matched.group(1))
                posted = html_lib.unescape(stamp).replace("发表于", "").strip()
                posted = re.sub(r"\s+", " ", posted)[:24]
            text = html_to_text(content)
            if text or author:
                posts.append({"author": author, "time": posted, "content": text})

        page_numbers = [int(item) for item in re.findall(rf"tid={tid}&(?:amp;)?page=(\d+)", html)]
        return {
            "title": title,
            "fid": fid,
            "tid": tid,
            "posts": posts,
            "page": page_no,
            "max_page": max(page_numbers) if page_numbers else 1,
        }

    def reply(self, tid: str, fid: str, message: str) -> Tuple[bool, str]:
        """在指定帖子中提交回复。

        :param tid: 帖子 ID
        :param fid: 板块 ID
        :param message: 回复内容
        :return: ``(是否成功, 结果说明)``
        """
        content = (message or "").strip()
        if not content:
            return False, "回复内容为空"
        if len(content) > 5000:
            return False, "回复内容过长"

        def _do(page: Any) -> Tuple[bool, str]:
            url = f"{self.site_url}/forum.php?mod=viewthread&tid={tid}"
            html = self._goto_html(page, url)
            matched = re.search(r'name="formhash"\s+value="([^"]+)"', html)
            if not matched:
                raise SehuaForumError("未取到表单校验串，无法回复")
            reply_fid = fid
            fid_matched = re.search(r"action=reply&(?:amp;)?fid=(\d+)", html)
            if fid_matched:
                reply_fid = fid_matched.group(1)
            script = """
            async ([url, payload]) => {
              const response = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'},
                body: payload,
                credentials: 'include'
              });
              return (await response.text()).slice(0, 1200);
            }
            """
            payload = (
                f"formhash={matched.group(1)}&handlekey=fastpost&subject=&"
                f"message={_urlencode(content)}&usesig=1&posttime={int(time.time())}"
            )
            target = self.site_url + self.REPLY_URL.format(fid=reply_fid, tid=tid)
            text = page.evaluate(script, [target, payload])
            return self._judge_reply(text)

        return self._with_session(_do)

    @staticmethod
    def _judge_reply(text: str) -> Tuple[bool, str]:
        """判断回复接口返回内容。

        :param text: 接口返回的 XML 文本
        :return: ``(是否成功, 结果说明)``
        """
        plain = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
        message_matched = re.search(r"errorhandle_\w+\('([^']+)'", text)
        if "succeedhandle" in text:
            return True, "回复成功"
        if message_matched:
            return False, message_matched.group(1).strip()
        plain_text = html_to_text(plain)[:200]
        if "回复" in plain_text and "成功" in plain_text:
            return True, plain_text
        return False, plain_text or "回复失败，站点未返回结果"


def _urlencode(value: str) -> str:
    """对回复内容做表单编码。

    :param value: 原始文本
    :return: URL 编码后的文本
    """
    from urllib.parse import quote

    return quote(value, safe="")
