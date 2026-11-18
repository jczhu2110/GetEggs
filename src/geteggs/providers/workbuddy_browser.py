"""Controlled browser session helper for WorkBuddy (Playwright + Microsoft Edge)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from geteggs.core.config import get_data_dir
from geteggs.core.models import ProviderConfig
from geteggs.core.secure_store import load_secure_json, save_secure_json

LogCallback = Callable[[str, str], None]

DEFAULT_PORTAL_URL = "https://www.codebuddy.cn/profile/usage"
PORTAL_HOSTS = ("codebuddy.cn", "workbuddy.cn", "copilot.tencent.com")


def _is_portal_url(url: str) -> bool:
    """Whether a URL is a logged-in portal page rather than a sign-in page."""
    if not url or "/login" in url or "/signin" in url:
        return False
    return any(host in url for host in PORTAL_HOSTS)


def get_browser_state_path(config: ProviderConfig) -> Path:
    """Resolve the persistent Playwright storageState path for WorkBuddy."""
    custom = (
        config.extra.get("browser_state_path")
        or os.environ.get("GETEGGS_WORKBUDDY_BROWSER_STATE")
    )
    if custom:
        return Path(str(custom)).expanduser().resolve(strict=False)
    return (get_data_dir() / "workbuddy_browser_state.json").resolve(strict=False)


def _scan_json_for_auth(obj: Any, depth: int = 0) -> dict[str, Any]:
    """Recursively look for token/uid fields inside JSON/localStorage blobs."""
    found: dict[str, Any] = {}
    if depth > 4:
        return found
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
                return _scan_json_for_auth(parsed, depth + 1)
            except ValueError:
                return found
        return found
    if isinstance(obj, dict):
        for key in ("accessToken", "access_token"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip() and "access_token" not in found:
                found["access_token"] = val.strip()
        for key in ("refreshToken", "refresh_token"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip() and "refresh_token" not in found:
                found["refresh_token"] = val.strip()
        for key in ("uid", "userId", "user_id"):
            val = obj.get(key)
            if val is not None and str(val).strip() and "uid" not in found:
                found["uid"] = str(val).strip()
        for key in ("domain", "baseUrl"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip() and "domain" not in found:
                found["domain"] = val.strip()
        for key in ("expiresAt", "expires_at"):
            val = obj.get(key)
            if val is not None and "expires_at" not in found:
                found["expires_at"] = val
        for val in obj.values():
            if isinstance(val, (dict, list, str)):
                sub = _scan_json_for_auth(val, depth + 1)
                for sk, sv in sub.items():
                    found.setdefault(sk, sv)
    elif isinstance(obj, list):
        for item in obj[:10]:
            sub = _scan_json_for_auth(item, depth + 1)
            for sk, sv in sub.items():
                found.setdefault(sk, sv)
    return found


def parse_storage_state(state_path: Path) -> Optional[dict[str, Any]]:
    """Extract tokens, uid, and domain cookies from a Playwright storageState file."""
    if not state_path.is_file():
        return None
    try:
        data = load_secure_json(state_path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    cookies: dict[str, str] = {}
    for item in data.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        name = item.get("name")
        value = item.get("value")
        if (
            name
            and isinstance(value, str)
            and any(host in domain for host in ("codebuddy.cn", "tencent.com", "workbuddy"))
        ):
            cookies[str(name)] = value

    auth_info: dict[str, Any] = {}
    has_portal_origin = False
    portal_origin: Optional[str] = None
    for origin in data.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        origin_url = str(origin.get("origin") or "")
        if any(host in origin_url for host in ("codebuddy.cn", "copilot.tencent.com", "workbuddy")):
            has_portal_origin = True
            if portal_origin is None:
                portal_origin = origin_url
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            k = str(entry.get("name") or "")
            v = entry.get("value")
            if k in ("accessToken", "access_token") and isinstance(v, str) and v.strip():
                auth_info.setdefault("access_token", v.strip())
            elif k in ("refreshToken", "refresh_token") and isinstance(v, str) and v.strip():
                auth_info.setdefault("refresh_token", v.strip())
            elif isinstance(v, str):
                scanned = _scan_json_for_auth(v)
                for sk, sv in scanned.items():
                    auth_info.setdefault(sk, sv)

    if not auth_info.get("access_token") and not (has_portal_origin and cookies):
        return None
    if not auth_info.get("domain") and portal_origin:
        auth_info["domain"] = portal_origin

    return {
        "access_token": auth_info.get("access_token"),
        "refresh_token": auth_info.get("refresh_token"),
        "uid": auth_info.get("uid"),
        "domain": auth_info.get("domain"),
        "expires_at": auth_info.get("expires_at"),
        "cookies": cookies,
        "has_session_cookies": bool(cookies),
    }


async def _launch_edge(playwright: Any, *, headless: bool) -> Any:
    """Launch installed Microsoft Edge via Playwright, falling back to Playwright Chromium."""
    try:
        return await playwright.chromium.launch(channel="msedge", headless=headless)
    except Exception:  # noqa: BLE001
        return await playwright.chromium.launch(headless=headless)


async def _verify_saved_state(
    browser: Any, state: dict[str, Any], portal_url: str
) -> bool:
    """Replay the saved storageState in a fresh context to confirm login works."""
    verify_context = await browser.new_context(storage_state=state)
    try:
        verify_page = await verify_context.new_page()
        await verify_page.goto(
            portal_url, wait_until="domcontentloaded", timeout=20000
        )
        await verify_page.wait_for_timeout(2000)
        return _is_portal_url(verify_page.url or "")
    except Exception:  # noqa: BLE001
        return False
    finally:
        await verify_context.close()


async def interactive_browser_login(
    state_path: Path,
    *,
    login_url: str = DEFAULT_PORTAL_URL,
    timeout_sec: int = 180,
) -> tuple[bool, str]:
    """Open Edge for one-time manual sign-in and persist Playwright storageState."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "未安装 playwright 依赖，请先执行 `uv sync`。"

    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await _launch_edge(p, headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        pending_path = state_path.with_suffix(state_path.suffix + ".pending")
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_sec
            saw_login = False
            portal_since: Optional[float] = None
            verify_attempted = False

            while loop.time() < deadline:
                if page.is_closed():
                    return False, "浏览器窗口已关闭，未完成登录授权。"
                current_url = page.url or ""
                if "/login" in current_url or "/signin" in current_url:
                    saw_login = True
                    portal_since = None
                elif _is_portal_url(current_url) and saw_login:
                    if portal_since is None:
                        portal_since = loop.time()
                    elif loop.time() - portal_since >= 2.0 and not verify_attempted:
                        await page.wait_for_timeout(1500)
                        if not _is_portal_url(page.url or ""):
                            portal_since = None
                            saw_login = bool(
                                "/login" in (page.url or "")
                                or "/signin" in (page.url or "")
                            )
                            continue
                        state = await context.storage_state()
                        save_secure_json(pending_path, state)
                        parsed = parse_storage_state(pending_path)
                        verify_attempted = True
                        if parsed and await _verify_saved_state(browser, state, login_url):
                            os.replace(pending_path, state_path)
                            return True, f"已完成浏览器授权并保存加密会话状态至 {state_path}"
                        pending_path.unlink(missing_ok=True)
                        return False, (
                            "登录页已跳转回个人中心，但保存的会话仍无法通过校验。"
                            "请确认已在弹出窗口中完成登录（扫码后需在手机上确认），然后重试。"
                        )
                else:
                    portal_since = None
                await asyncio.sleep(1.0)

            return False, f"等待登录超时（>{timeout_sec} 秒），请重试。"
        except Exception as exc:  # noqa: BLE001
            return False, f"浏览器授权过程异常：{exc}"
        finally:
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            await browser.close()


async def browser_checkin_session(
    state_path: Path,
    *,
    base_url: str = "https://www.codebuddy.cn",
    uid: Optional[str] = None,
    dry_run: bool = False,
    log: Optional[LogCallback] = None,
) -> dict[str, Any]:
    """Run check-in API calls inside a headless Edge context backed by saved storageState."""
    from playwright.async_api import async_playwright

    target_url = f"{base_url.rstrip('/')}/profile/usage"
    try:
        state = load_secure_json(state_path)
    except (OSError, ValueError) as exc:
        return {
            "authenticated": False,
            "reason": f"受控浏览器会话文件无法读取或解密，请重新授权登录：{exc}",
        }

    async with async_playwright() as p:
        browser = await _launch_edge(p, headless=True)
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        try:
            if log:
                log("info", f"正在通过受控无头浏览器访问 {target_url} ...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2200)

            if "/login" in (page.url or ""):
                return {
                    "authenticated": False,
                    "reason": "受控浏览器登录态已过期（重定向至登录页），请重新授权登录。",
                }

            # Persist sliding cookies / refreshed localStorage tokens (skip on dry-run).
            if not dry_run:
                save_secure_json(state_path, await context.storage_state())
            parsed = parse_storage_state(state_path) or {}
            token = parsed.get("access_token")
            effective_uid = uid or parsed.get("uid")
            domain_host = base_url.replace("https://", "").replace("http://", "").split("/")[0]

            js_script = """
            async ({ endpoints, checkinEndpoint, token, uid, domainHost, dryRun }) => {
                const headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Domain": domainHost
                };
                if (token) headers["Authorization"] = "Bearer " + token;
                if (uid) headers["X-User-Id"] = uid;

                async function postJson(path) {
                    const resp = await fetch(path, {
                        method: "POST",
                        credentials: "include",
                        headers,
                        body: "{}"
                    });
                    let data = null;
                    try { data = await resp.json(); } catch (_) {}
                    return { status: resp.status, data };
                }

                let statusRes = { status: 0, data: null };
                for (const ep of endpoints) {
                    statusRes = await postJson(ep);
                    if (statusRes.status !== 404) break;
                }
                if (statusRes.status !== 200 || dryRun) {
                    return { statusRes, checkinRes: null, postStatusRes: null };
                }

                const d = (statusRes.data && statusRes.data.data) ? statusRes.data.data : (statusRes.data || {});
                const stateKeys = [
                    "checkedIn", "checked_in", "todayCheckedIn", "today_checked_in",
                    "isCheckedIn", "is_checked_in", "signed"
                ];
                const hasState = stateKeys.some(
                    (key) => Object.prototype.hasOwnProperty.call(d, key)
                );
                if (!hasState) {
                    return { statusRes, checkinRes: null, postStatusRes: null };
                }
                const already = Boolean(
                    d.checkedIn ?? d.checked_in ?? d.todayCheckedIn ?? d.today_checked_in ?? d.isCheckedIn ?? d.signed
                );
                if (already) {
                    return { statusRes, checkinRes: null, postStatusRes: null };
                }

                const checkinRes = await postJson(checkinEndpoint);
                let postStatusRes = null;
                if (checkinRes.status === 200) {
                    for (const ep of endpoints) {
                        postStatusRes = await postJson(ep);
                        if (postStatusRes.status !== 404) break;
                    }
                }
                return { statusRes, checkinRes, postStatusRes };
            }
            """
            result = await page.evaluate(
                js_script,
                {
                    "endpoints": [
                        "/v2/billing/meter/checkin-status",
                        "/v2/billing/meter/checkin-activity-status",
                    ],
                    "checkinEndpoint": "/v2/billing/meter/daily-checkin",
                    "token": token,
                    "uid": effective_uid,
                    "domainHost": domain_host,
                    "dryRun": dry_run,
                },
            )
            return {"authenticated": True, "active_url": base_url.rstrip("/"), **result}
        finally:
            await browser.close()
