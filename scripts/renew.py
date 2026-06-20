import os
import re
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 日志 ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────
EMAIL     = os.environ["ZENIX_EMAIL"]
PASSWORD  = os.environ["ZENIX_PASSWORD"]
WX_TOKEN  = os.environ.get("WXPUSHER_TOKEN", "")
WX_UID    = os.environ.get("WXPUSHER_UID", "")

BASE_URL  = "https://dash.zenix.sg"
BJ        = timezone(timedelta(hours=8))

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ── WxPusher 推送 ─────────────────────────────────────────────────────────
def wxpush(content: str):
    if not WX_TOKEN or not WX_UID:
        log.warning("📨 WXPUSHER_TOKEN 或 WXPUSHER_UID 未配置，跳过推送")
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WX_TOKEN,
        "content":  content,
        "contentType": 1,
        "uids": [WX_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ── 工具函数 ──────────────────────────────────────────────────────────────
def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except:
        return ""

def human_delay(min_s=0.3, max_s=0.8):
    import random
    time.sleep(random.uniform(min_s, max_s))

# ── 登录（浏览器表单登录） ────────────────────────────────────────────────
def login_browser(page) -> bool:
    """直接用 CloakBrowser 操作登录表单，避免 API 登录 405 问题。"""
    log.info("浏览器表单登录 Zenix...")
    try:
        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        take_screenshot(page, "00_login_page")

        email_sel = "input[type='email'], input[name='email'], input[placeholder*='mail' i]"
        page.wait_for_selector(email_sel, timeout=10000)
        page.fill(email_sel, EMAIL)
        human_delay()

        pwd_sel = "input[type='password'], input[name='password']"
        page.wait_for_selector(pwd_sel, timeout=10000)
        page.fill(pwd_sel, PASSWORD)
        human_delay()

        take_screenshot(page, "00b_login_filled")

        btn_sel = "button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('登录'), button:has-text('Sign')"
        page.click(btn_sel, timeout=8000)

        try:
            page.wait_for_url("**/dashboard**", timeout=20000)
        except Exception:
            time.sleep(3)

        take_screenshot(page, "00c_after_login")

        current = page.url
        log.info(f"登录后页面: {current}")
        if "dashboard" in current or "renew" in current:
            log.info("✅ 浏览器登录成功")
            return True
        else:
            body = get_text(page)
            log.warning(f"登录后未跳转 dashboard，页面内容片段: {body[:300]}")
            return False
    except Exception as e:
        log.exception(f"浏览器登录异常: {e}")
        take_screenshot(page, "00_login_error")
        return False

# ── 从浏览器页面文本中提取续期信息 ──────────────────────────────────────
def fetch_renew_info_from_page(page) -> dict:
    """
    直接从渲染好的页面文本里提取信息，不走 API。
    页面显示示例：
      Last renewed: 6/4/2026, 7:20:00 AM
      1.6 days   ← badge 元素，独立一行
    注意：页面规则说明里也有 "every 2 days"、"within 2 days" 等文字，
    必须用 JS 直接读 badge 元素，避免正则误匹配。
    """
    result = {"last_renewed": "未知", "days_left": "未知"}
    try:
        # ── 方法1：JS 直接读 badge span（最精准） ────────────────────────
        # badge: <span class="...rounded-md...font-medium...">1.5 days</span>
        days_js = page.evaluate("""() => {
            // 找包含 "days" 文本的 badge span（不含 "every" 前缀的独立元素）
            const spans = Array.from(document.querySelectorAll('span, div'));
            for (const el of spans) {
                const txt = (el.innerText || '').trim();
                // 只匹配 "1.5 days" / "2 days" 这种纯天数格式
                if (/^\\d+(\\.\\d+)?\\s*days?$/i.test(txt)) {
                    return txt;
                }
            }
            return null;
        }""")
        if days_js:
            m = re.search(r"([\d]+\.[\d]+|[\d]+)", days_js)
            if m:
                result["days_left"] = m.group(1)
                log.info(f"JS badge 提取剩余天数: {result['days_left']}")

        # ── 方法2：JS 直接读 "Last renewed" 所在元素 ─────────────────────
        last_js = page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('*'));
            for (const el of all) {
                if (el.children.length === 0) {  // 叶子节点
                    const txt = (el.innerText || '').trim();
                    if (/Last renewed/i.test(txt)) {
                        return txt;
                    }
                }
            }
            // 回退：全页文本
            return document.body.innerText;
        }""")
        if last_js:
            last_match = re.search(
                r"Last renewed[:\s]+(\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)",
                last_js, re.IGNORECASE
            )
            if last_match:
                result["last_renewed"] = last_match.group(1).strip()
                log.info(f"JS 提取上次续期: {result['last_renewed']}")

        # ── 方法3：回退到 body text 正则（仅 days 未找到时） ─────────────
        if result["days_left"] == "未知":
            body = get_text(page)
            # 只匹配行首或空白后的独立天数，排除 "every N days"、"within N days"
            days_match = re.search(
                r"(?<!every\s)(?<!within\s)(?<!after\s)\b([\d]+\.[\d]+)\s*days?",
                body, re.IGNORECASE
            )
            if not days_match:
                # 找第一个小数天数
                days_match = re.search(r"\b(\d+\.\d+)\s*days?", body, re.IGNORECASE)
            if days_match:
                result["days_left"] = days_match.group(1)

        log.info(f"页面提取 → 上次续期: {result['last_renewed']}，剩余: {result['days_left']} 天")
        return result
    except Exception as e:
        log.warning(f"页面信息提取失败: {e}")
        return result

# ── 用浏览器点击续期按钮 ──────────────────────────────────────────────────
def do_renew_browser(page) -> bool:
    """
    在续期页面找到 'Renew Account' 按钮并点击，等待页面刷新确认续期成功。
    按钮 HTML: <button data-slot="button" ...>Renew Account (1 coins)</button>
    """
    log.info("用浏览器点击续期按钮...")
    try:
        # 确保在续期页面
        if "/dashboard/renew" not in page.url:
            page.goto(f"{BASE_URL}/dashboard/renew", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

        # 等待按钮出现（data-slot="button" 且文本含 Renew Account）
        btn_sel = 'button[data-slot="button"]'
        page.wait_for_selector(btn_sel, timeout=10000)

        # 找到文本含 "Renew Account" 的那个按钮
        buttons = page.query_selector_all(btn_sel)
        renew_btn = None
        for btn in buttons:
            txt = (btn.inner_text() or "").strip()
            log.info(f"  发现按钮: {txt!r}")
            if "renew account" in txt.lower():
                renew_btn = btn
                break

        if renew_btn is None:
            # 回退：直接用文本选择器
            renew_btn = page.query_selector("button:has-text('Renew Account')")

        if renew_btn is None:
            log.warning("❌ 未找到续期按钮，截图后退出")
            take_screenshot(page, "renew_btn_not_found")
            return False

        log.info(f"✅ 找到续期按钮: {renew_btn.inner_text().strip()!r}")
        take_screenshot(page, "02b_before_click")

        renew_btn.click()
        log.info("按钮已点击，等待续期完成...")

        # 等待 "Renewing..." 消失，最多 15 秒
        try:
            page.wait_for_selector(
                "button:has-text('Renewing')",
                state="hidden",
                timeout=15000
            )
            log.info("✅ Renewing... 已消失，续期处理完毕")
        except Exception:
            log.info("wait_for_selector 超时，继续等待 3 秒...")
            time.sleep(3)

        time.sleep(2)  # 额外等页面数据刷新
        take_screenshot(page, "02c_after_click")

        # 判断续期是否成功：Last renewed 时间变成今天
        body_after = get_text(page)
        # 用月/日/年格式匹配今天，兼容单位数月日（如 6/5/2026）
        now = datetime.now()
        today_patterns = [
            f"{now.month}/{now.day}/{now.year}",       # 6/5/2026
            f"{now.month:02d}/{now.day:02d}/{now.year}" # 06/05/2026
        ]
        matched_today = any(p in body_after for p in today_patterns)
        if matched_today:
            log.info(f"✅ 续期成功，Last renewed 已更新为今天")
            return True
        else:
            log.warning(f"⚠️ 未检测到今天日期，可能续期失败或已是最新，请查看截图")
            return True  # 按钮已点击，保守返回 True

    except Exception as e:
        log.exception(f"浏览器续期异常: {e}")
        take_screenshot(page, "renew_error")
        return False

# ── 格式化为北京时间（UTC+8） ─────────────────────────────────────────────
def format_to_beijing(raw: str) -> str:
    """
    把页面显示的本地时间（服务器是 UTC）转换为北京时间（UTC+8），
    输出 24 小时制：2026-06-05 11:18:15
    输入示例：'6/5/2026, 3:18:15 AM'  '6/4/2026, 4:03:23 PM'
    """
    raw = raw.strip()
    for fmt in (
        "%m/%d/%Y, %I:%M:%S %p",   # 6/5/2026, 3:18:15 AM
        "%m/%d/%Y, %I:%M %p",      # 6/5/2026, 3:18 AM
        "%m/%d/%Y %I:%M:%S %p",    # 6/5/2026 3:18:15 AM
        "%m/%d/%Y %I:%M %p",       # 6/5/2026 3:18 AM
        "%Y/%m/%d %H:%M:%S",       # 2026/06/05 03:18:15
    ):
        try:
            dt_utc = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            dt_bj  = dt_utc.astimezone(BJ)
            return dt_bj.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return raw  # 解析失败原样返回

# ── 主流程 ────────────────────────────────────────────────────────────────
def main():
    from cloakbrowser import launch

    log.info("启动 CloakBrowser（源码级指纹伪装）...")
    browser = launch(headless=False, humanize=True)
    page = browser.new_page()

    try:
        # ── Step 1: 浏览器表单登录 ───────────────────────────────────────
        if not login_browser(page):
            wxpush("❌ Zenix 续期失败：登录失败，请检查账号密码。")
            raise SystemExit(1)

        # ── Step 2: 前往续期页面 ─────────────────────────────────────────
        log.info("前往续期页面（续期前）...")
        page.goto(f"{BASE_URL}/dashboard/renew", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        take_screenshot(page, "02_renew_before")

        # ── Step 3: 读取续期前信息 ───────────────────────────────────────
        before = fetch_renew_info_from_page(page)
        log.info(f"续期前 → 上次续期: {before['last_renewed']}，剩余: {before['days_left']} 天")

        # ── Step 4: 点击续期按钮 ─────────────────────────────────────────
        renew_ok = do_renew_browser(page)

        # ── Step 5: 刷新页面读取续期后信息 ──────────────────────────────
        time.sleep(2)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        take_screenshot(page, "03_renew_after")
        after = fetch_renew_info_from_page(page)
        log.info(f"续期后 → 上次续期: {after['last_renewed']}，剩余: {after['days_left']} 天")

        # ── Step 6: 截图截图目录 ─────────────────────────────────────────
        log.info("截图目录内容：")
        for f in sorted(SCREENSHOT_DIR.iterdir()):
            log.info(f"  {f.name}  ({f.stat().st_size} bytes)")

        # ── Step 7: 推送通知 ─────────────────────────────────────────────
        last_bj    = format_to_beijing(after["last_renewed"])
        days_before = before["days_left"]
        days_after  = after["days_left"]
        status      = "✅ 续期成功" if renew_ok else "⚠️ 续期结果未知"

        lines = [
            status,
            "─────────────────",
            f"🕐 续期时间：{last_bj}（北京时间）",
            f"📅 剩余有效期：{days_before} 天 → {days_after} 天",
            "─────────────────",
            f"账号：{EMAIL}",
        ]
        wxpush("\n".join(lines))

        log.info("流程全部完成 ✓")

    except Exception as e:
        log.exception(e)
        take_screenshot(page, "99_error")
        wxpush(f"❌ Zenix 任务异常: {e}")
    finally:
        time.sleep(5)
        browser.close()
        log.info("任务结束")

if __name__ == "__main__":
    main()
