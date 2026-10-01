"""98堂（色花堂）论坛签到核心流程实现。

流程：启动内嵌浏览器（CloakBrowser）→ 通过 Cloudflare 校验与站点年龄确认页
→ 使用账号、密码与安全提问登录 Discuz → 打开每日签到页
→ 获取验证码并解答旋转类型 → 提交校验 → 调用签到接口完成签到。
"""

import base64
import importlib
import io
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# Discuz 安全提问编号与说明，登录时按编号提交
QUESTION_CHOICES: List[Dict[str, str]] = [
    {"title": "不设置（留空）", "value": "0"},
    {"title": "1-母亲的名字", "value": "1"},
    {"title": "2-爷爷的名字", "value": "2"},
    {"title": "3-父亲出生的城市", "value": "3"},
    {"title": "4-您其中一位老师的名字", "value": "4"},
    {"title": "5-您个人计算机的型号", "value": "5"},
    {"title": "6-您最喜欢的餐馆名称", "value": "6"},
    {"title": "7-驾驶执照最后四位数字", "value": "7"},
]

# 站点年龄确认页的进入按钮文案
AGE_GATE_TEXTS = ("请点此进入", "please click here", "点击进入")


class SehuaSignError(Exception):
    """签到流程中可直接呈现给用户的异常。"""


def scan_driver_pids() -> set:
    """列出系统中所有 playwright driver 进程的 PID。

    直接读取 /proc，避免引入额外依赖；读取失败时返回空集合。

    :return: playwright driver 进程 PID 集合
    """
    pids: set = set()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if "playwright" in cmdline and "driver" in cmdline:
                pids.add(int(entry))
    except OSError:
        return set()
    return pids


def _pid_alive(pid: int) -> bool:
    """判断进程是否仍然存在。

    :param pid: 进程号
    :return: 进程存在返回 True
    """
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def terminate_pids(pids, grace_seconds: float = 8.0) -> None:
    """终止指定 PID 的进程，先温和再强制。

    浏览器进程在异常状态下可能忽略终止信号，因此先发送 SIGTERM，等待一段时间后
    对仍然存活的进程发送 SIGKILL。

    :param pids: 待终止的 PID 集合
    :param grace_seconds: 发送 SIGKILL 前的等待秒数
    """
    import signal

    targets = [int(pid) for pid in pids if pid]
    alive = [pid for pid in targets if _pid_alive(pid)]
    if not alive:
        return
    for pid in alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    deadline = time.time() + max(0.0, float(grace_seconds))
    while time.time() < deadline:
        alive = [pid for pid in alive if _pid_alive(pid)]
        if not alive:
            return
        time.sleep(0.5)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue


def _decode_image(image_b64: str) -> Any:
    """把 base64 图片内容解码为 PIL 图像对象。

    :param image_b64: 形如 ``data:image/png;base64,xxx`` 的图片字符串
    :return: 转换后的 RGB 图像对象
    """
    from PIL import Image

    raw = image_b64.split(",", 1)[-1]
    return Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")


def solve_rotate_angle(master_b64: str, thumb_b64: str) -> float:
    """通过图像比对计算旋转验证码需要旋转的角度。

    把缩略图按不同角度反向旋转后，与背景图中心区域在圆形掩膜内比较像素差异，
    取差异最小者对应的角度作为答案。

    :param master_b64: 背景图 base64 内容
    :param thumb_b64: 待旋转缩略图 base64 内容
    :return: 最佳旋转角度（0-360）
    """
    import numpy as np
    from PIL import Image

    master = _decode_image(master_b64).convert("L")
    thumb = _decode_image(thumb_b64).convert("L")
    master_width, master_height = master.size
    thumb_width, thumb_height = thumb.size

    grid_y, grid_x = np.mgrid[0:thumb_height, 0:thumb_width]
    center_y, center_x = (thumb_height - 1) / 2, (thumb_width - 1) / 2
    radius = min(thumb_width, thumb_height) / 2 - 3
    mask = ((grid_y - center_y) ** 2 + (grid_x - center_x) ** 2) <= radius * radius

    best: Optional[Tuple[float, float, int, int]] = None

    # 粗搜索：先定偏移与角度的大致范围
    for offset_y in range(-18, 19, 6):
        for offset_x in range(-18, 19, 6):
            left = int((master_width - thumb_width) / 2 + offset_x)
            top = int((master_height - thumb_height) / 2 + offset_y)
            if left < 0 or top < 0:
                continue
            if left + thumb_width > master_width or top + thumb_height > master_height:
                continue
            reference = np.asarray(
                master.crop((left, top, left + thumb_width, top + thumb_height)),
                dtype=np.float32,
            )
            for angle in range(0, 360, 2):
                rotated = np.asarray(
                    thumb.rotate(-angle, resample=Image.BILINEAR, fillcolor=0),
                    dtype=np.float32,
                )
                diff = (rotated - reference)[mask]
                score = float(np.mean(diff * diff))
                if best is None or score < best[0]:
                    best = (score, angle, offset_x, offset_y)
    if best is None:
        raise SehuaSignError("旋转验证码背景尺寸异常，无法比对")

    # 精细搜索：在粗搜索结果附近收敛
    _, angle0, offset_x0, offset_y0 = best
    for offset_y in range(offset_y0 - 5, offset_y0 + 6):
        for offset_x in range(offset_x0 - 5, offset_x0 + 6):
            left = int((master_width - thumb_width) / 2 + offset_x)
            top = int((master_height - thumb_height) / 2 + offset_y)
            if left < 0 or top < 0:
                continue
            if left + thumb_width > master_width or top + thumb_height > master_height:
                continue
            reference = np.asarray(
                master.crop((left, top, left + thumb_width, top + thumb_height)),
                dtype=np.float32,
            )
            for angle in np.arange(angle0 - 3, angle0 + 3.01, 0.5):
                rotated = np.asarray(
                    thumb.rotate(-float(angle), resample=Image.BILINEAR, fillcolor=0),
                    dtype=np.float32,
                )
                diff = (rotated - reference)[mask]
                score = float(np.mean(diff * diff))
                if score < best[0]:
                    best = (score, float(angle), offset_x, offset_y)

    return float(best[1]) % 360


class SehuaSigner:
    """执行 98堂 论坛登录与每日签到的执行器。"""

    # 登录接口地址模板
    LOGIN_PATH = "/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&inajax=1"
    # 签到入口
    SIGN_PAGE = "/plugin.php?id=dd_sign"
    # 验证码接口
    CAPTCHA_URL = "/misc.php?mod=captcha"
    # 验证码校验接口
    CAPTCHA_CHECK_URL = "/misc.php?mod=captcha&action=check"
    # 签到提交接口
    SIGN_ACTION = "/plugin.php?id=dd_sign&ac=sign_v2"

    def __init__(
        self,
        site_url: str,
        username: str,
        password: str,
        question_id: str = "7",
        answer: str = "",
        proxy: str = "",
        captcha_retry: int = 8,
        captcha_interval: float = 4.0,
        headless: bool = True,
        timeout: int = 60,
        log: Optional[Callable[[str], None]] = None,
        on_driver: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """初始化签到执行器。

        :param site_url: 站点地址，如 ``https://sehuatang.org``
        :param username: 登录用户名
        :param password: 登录密码
        :param question_id: 安全提问编号，未设置时填 ``0``
        :param answer: 安全提问答案
        :param proxy: 访问站点使用的代理地址，无代理时留空
        :param captcha_retry: 遇到不支持的验证码类型时的最大重试次数
        :param captcha_interval: 两次获取验证码之间的等待秒数
        :param headless: 是否使用无头浏览器
        :param timeout: 页面与请求超时秒数
        :param log: 日志输出回调
        :param on_driver: 浏览器启动后回调，用于回报新增的 driver 进程 PID
        """
        self.site_url = (site_url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.question_id = str(question_id or "0").strip()
        self.answer = (answer or "").strip()
        self.proxy = (proxy or "").strip()
        self.captcha_retry = max(1, int(captcha_retry or 1))
        self.captcha_interval = max(1.0, float(captcha_interval or 1.0))
        self.headless = bool(headless)
        self.timeout = max(20, int(timeout or 60))
        self._log = log
        self._on_driver = on_driver

    def _log_info(self, message: str) -> None:
        """输出流程日志。

        :param message: 日志内容
        """
        if self._log:
            self._log(message)

    def run(self) -> Tuple[bool, str]:
        """执行一次完整的登录与签到流程。

        :return: ``(是否成功, 结果说明)``
        """
        if not self.site_url:
            return False, "未配置站点地址"
        if not self.username or not self.password:
            return False, "未配置账号或密码"
        if self.question_id not in ("", "0") and not self.answer:
            return False, "账号设置了安全提问，但未填写答案"

        cloakbrowser = importlib.import_module("cloakbrowser")
        context = None
        try:
            self._log_info(f"启动浏览器访问 {self.site_url}")
            known = scan_driver_pids()
            context = cloakbrowser.launch_context(
                headless=self.headless,
                proxy=self.proxy or None,
            )
            # 回报本次新增的 driver 进程，便于插件重载后回收
            if self._on_driver:
                try:
                    self._on_driver(sorted(scan_driver_pids() - known))
                except Exception:  # noqa: BLE001 - 回报失败不影响签到
                    pass
            page = context.new_page()
            page.set_default_timeout(self.timeout * 1000)

            self._pass_age_gate(page)
            self._login(page)
            return self._sign(page)
        except SehuaSignError as error:
            return False, str(error)
        except Exception as error:  # noqa: BLE001 - 网络与浏览器异常统一转为可见结果
            return False, f"{type(error).__name__}: {error}"
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响签到结论
                    pass

    def _pass_age_gate(self, page: Any) -> None:
        """打开站点首页并通过年龄确认页。

        :param page: 浏览器页面对象
        """
        page.goto(self.site_url + "/", wait_until="domcontentloaded", timeout=self.timeout * 1000)
        time.sleep(5)
        for text in AGE_GATE_TEXTS:
            try:
                target = page.get_by_text(text, exact=False).first
                if target and target.count() > 0:
                    target.click(timeout=8000)
                    time.sleep(4)
                    self._log_info("已通过站点年龄确认页")
                    return
            except Exception:  # noqa: BLE001 - 未出现年龄门属于正常情况
                continue
        self._log_info("未出现年龄确认页，继续执行")

    def _login(self, page: Any) -> None:
        """使用账号密码与安全提问登录站点。

        :param page: 浏览器页面对象
        """
        page.goto(
            self.site_url + "/member.php?mod=logging&action=login",
            wait_until="domcontentloaded",
            timeout=self.timeout * 1000,
        )
        time.sleep(2)
        matched = re.search(r'name="formhash"\s+value="([^"]+)"', page.content())
        if not matched:
            raise SehuaSignError("登录页未取到 formhash，站点结构可能已变化")

        script = """
        async ([base, formhash, user, pwd, qid, ans]) => {
          const body = new URLSearchParams({
            formhash: formhash,
            referer: base + '/',
            username: user,
            password: pwd,
            questionid: qid,
            answer: ans,
            cookietime: '2592000'
          });
          const response = await fetch(base + '/member.php?mod=logging&action=login&loginsubmit=yes&handlekey=login&inajax=1', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded',
              'X-Requested-With': 'XMLHttpRequest'
            },
            body: body.toString(),
            credentials: 'include'
          });
          return (await response.text()).slice(0, 800);
        }
        """
        text = page.evaluate(
            script,
            [
                self.site_url,
                matched.group(1),
                self.username,
                self.password,
                self.question_id or "0",
                self.answer,
            ],
        )
        if "欢迎您回来" in text or "succeedhandle_login" in text:
            self._log_info("登录成功")
            return
        if "安全提问" in text or "loginperm" in text:
            raise SehuaSignError("登录失败：安全提问的编号或答案不正确")
        if "密码错误" in text or "密码不正确" in text:
            raise SehuaSignError("登录失败：账号或密码不正确")
        if "两次登录" in text or "尝试次数" in text:
            raise SehuaSignError(f"登录失败：尝试次数过多，请稍后再试（{text[:80]}）")
        raise SehuaSignError(f"登录失败：{text[:120]}")

    def _sign(self, page: Any) -> Tuple[bool, str]:
        """打开签到页并完成签到。

        :param page: 浏览器页面对象
        :return: ``(是否成功, 结果说明)``
        """
        page.goto(
            self.site_url + self.SIGN_PAGE,
            wait_until="domcontentloaded",
            timeout=self.timeout * 1000,
        )
        time.sleep(4)
        if not page.query_selector("#signin-btn"):
            body = page.inner_text("body")
            if "今日已签到" in body:
                return True, "今日已签到，无需重复签到"
            if "每日签到" not in body:
                raise SehuaSignError("无法打开签到页，可能是登录状态失效或站点结构变化")
            return True, "站点未显示签到按钮，判定为今日已签到"

        last_type = ""
        for attempt in range(1, self.captcha_retry + 1):
            captcha = page.evaluate(
                """async (url) => {
                  const response = await fetch(url, {credentials: 'include'});
                  return await response.text();
                }""",
                self.site_url + self.CAPTCHA_URL,
            )
            try:
                payload = json.loads(captcha)
            except Exception:  # noqa: BLE001 - 站点限流时可能返回非 JSON
                self._log_info(f"第 {attempt} 次获取验证码返回异常，等待后重试")
                time.sleep(self.captcha_interval)
                continue

            data = payload.get("data") or {}
            captcha_type = data.get("type")
            last_type = captcha_type or last_type
            if captcha_type != "rotate":
                self._log_info(
                    f"第 {attempt} 次验证码类型为 {captcha_type or '未知'}，暂不支持，换一张重试"
                )
                time.sleep(self.captcha_interval)
                continue

            angle = solve_rotate_angle(
                data.get("master_image_base64", ""),
                data.get("thumb_image_base64", ""),
            )
            answer = int(round(angle))
            self._log_info(f"第 {attempt} 次验证码为旋转类型，计算角度 {answer}°")
            check_result = page.evaluate(
                """async ([url, value]) => {
                  const response = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'text/plain'},
                    body: String(value),
                    credentials: 'include'
                  });
                  return (await response.text()).slice(0, 200);
                }""",
                [self.site_url + self.CAPTCHA_CHECK_URL, answer],
            )
            if '"ok"' not in check_result:
                self._log_info(f"验证码校验未通过：{check_result[:80]}")
                time.sleep(self.captcha_interval)
                continue

            sign_result = page.evaluate(
                """async (url) => {
                  const response = await fetch(url, {credentials: 'include'});
                  return (await response.text()).slice(0, 300);
                }""",
                self.site_url + self.SIGN_ACTION,
            )
            try:
                result = json.loads(sign_result)
            except Exception:  # noqa: BLE001 - 非 JSON 响应按失败处理
                return False, f"签到接口返回异常：{sign_result[:120]}"
            message = str(result.get("message") or "").strip()
            if result.get("code") == 200:
                return True, message or "签到成功"
            if "已签到" in message:
                return True, message
            return False, message or "签到失败"

        return False, f"连续 {self.captcha_retry} 次均未遇到旋转类型验证码（最近类型：{last_type or '未知'}）"
