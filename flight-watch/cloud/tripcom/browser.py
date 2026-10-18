"""浏览器会话管理。

Playwright 的 sync API 不能跨线程使用，所以这里起一个专属 worker 线程持有
playwright / browser / context，HTTP 层通过队列把任务丢进来串行执行。
和 xiaohongshu-mcp 一样，用一个真实浏览器 + 持久化 storage state，
既保留会话连续性（对风控友好），以后要加登录态也是现成的。
"""

import logging
import os
import queue
import threading
import uuid

from playwright.sync_api import sync_playwright

log = logging.getLogger("tripcom.browser")

STATE_PATH = os.environ.get(
    "TRIPCOM_STATE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.json"),
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class BrowserError(RuntimeError):
    pass


class _Task:
    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error = None


class BrowserWorker:
    """单线程持有 playwright，串行执行浏览器任务。"""

    def __init__(self, headless=True, locale="en-AU", timezone="Australia/Sydney"):
        self.headless = headless
        self.locale = locale
        self.timezone = timezone
        self._q = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._boot_error = None
        self._stopping = False

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="browser-worker", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=120)
        if self._boot_error:
            raise BrowserError(f"浏览器启动失败: {self._boot_error}")
        if not self._ready.is_set():
            raise BrowserError("浏览器启动超时")

    def stop(self):
        self._stopping = True
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=20)

    def _run(self):
        pw = browser = ctx = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx_kwargs = dict(
                locale=self.locale,
                timezone_id=self.timezone,
                viewport={"width": 1440, "height": 900},
                user_agent=UA,
            )
            if os.path.exists(STATE_PATH):
                ctx_kwargs["storage_state"] = STATE_PATH
                log.info("载入已保存的会话: %s", STATE_PATH)
            ctx = browser.new_context(**ctx_kwargs)
            ctx.set_default_timeout(60_000)
            # 抹掉最明显的自动化痕迹
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            self._ctx = ctx
            log.info("浏览器就绪 (headless=%s)", self.headless)
            self._ready.set()

            while True:
                task = self._q.get()
                if task is None:
                    break
                try:
                    task.result = task.fn(ctx)
                except Exception as exc:  # noqa: BLE001 - 要把异常带回调用方
                    log.exception("浏览器任务失败")
                    task.error = exc
                finally:
                    task.done.set()
        except Exception as exc:  # noqa: BLE001
            self._boot_error = exc
            self._ready.set()
        finally:
            for closer in (
                lambda: ctx and ctx.storage_state(path=STATE_PATH),
                lambda: ctx and ctx.close(),
                lambda: browser and browser.close(),
                lambda: pw and pw.stop(),
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 提交任务 ----------

    def submit(self, fn, timeout=180):
        """把 fn(context) 丢给 worker 线程执行，同步等结果。"""
        self.start()
        task = _Task(fn)
        self._q.put(task)
        if not task.done.wait(timeout=timeout):
            raise BrowserError(f"浏览器任务超时（{timeout}s）")
        if task.error:
            raise task.error
        return task.result

    def save_state(self):
        def _save(ctx):
            ctx.storage_state(path=STATE_PATH)
            return STATE_PATH

        return self.submit(_save, timeout=30)


def new_page(ctx):
    """开一个新标签页，带一个随机的 trace id 方便日志排查。"""
    page = ctx.new_page()
    page._trace_id = uuid.uuid4().hex[:8]  # noqa: SLF001 - 只是给日志用
    return page
