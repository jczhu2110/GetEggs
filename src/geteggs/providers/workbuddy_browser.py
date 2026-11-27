"""Controlled browser session helper for WorkBuddy (Playwright + Microsoft Edge)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from geteggs.core.config import allow_custom_config, get_data_dir, is_unc_path
from geteggs.core.models import ProviderConfig
from geteggs.core.secure_store import load_secure_json, save_secure_json

LogCallback = Callable[[str, str], None]

DEFAULT_PORTAL_URL = "https://www.codebuddy.cn/profile/usage"
PORTAL_HOSTS = ("codebuddy.cn", "workbuddy.cn", "copilot.tencent.com")


def _is_portal_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower().lstrip(".").rstrip(".")
    if not host:
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in PORTAL_HOSTS)


def _is_portal_origin(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and _is_portal_host(parsed.hostname or "")


def _is_login_url(url: str) -> bool:
    if not url:
        return False
    try:
        path = (urlparse(url).path or "").lower()
    except ValueError:
        return False
    return "/login" in path or "/signin" in path


def _is_portal_url(url: str) -> bool:
    """Whether a URL is a logged-in portal page rather than a sign-in page."""
    if not _is_portal_origin(url):
        return False
    return not _is_login_url(url)


def _matches_active_portal(page_url: str, base_url: str) -> bool:
    if not page_url or _is_login_url(page_url):
        return False
    if _is_portal_url(page_url):
        return True
    if allow_custom_config():
        try:
            page_parsed = urlparse(page_url)
            base_parsed = urlparse(base_url)
        except ValueError:
            return False
        return (
            page_parsed.scheme.lower() in {"http", "https"}
            and bool(page_parsed.hostname)
            and (page_parsed.hostname or "").lower()
            == (base_parsed.hostname or "").lower()
        )
    return False


def get_browser_state_path(config: ProviderConfig) -> Path:
    """Resolve the persistent Playwright storageState path for WorkBuddy."""
    custom = (
        config.extra.get("browser_state_path")
        or os.environ.get("GETEGGS_WORKBUDDY_BROWSER_STATE")
    )
    if custom:
        cleaned = str(custom).strip()
        if cleaned and "\x00" not in cleaned and not is_unc_path(cleaned):
            return Path(cleaned).expanduser().resolve(strict=False)
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


def parse_storage_state_dict(data: Any) -> Optional[dict[str, Any]]:
    """Extract tokens, uid, and scoped portal cookies from an in-memory storageState."""
    if not isinstance(data, dict):
        return None

    now = time.time()
    cookies: dict[str, str] = {}
    cookie_entries: list[dict[str, Any]] = []
    for item in data.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        name = item.get("name")
        value = item.get("value")
        expires = item.get("expires")
        if (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and expires > 0
            and expires < now
        ):
            continue
        if name and isinstance(value, str) and _is_portal_host(domain):
            name_str = str(name)
            cookies[name_str] = value
            cookie_entries.append(
                {
                    "name": name_str,
                    "value": value,
                    "domain": domain,
                    "path": str(item.get("path") or "/"),
                    "secure": bool(item.get("secure", False)),
                }
            )

    auth_info: dict[str, Any] = {}
    has_portal_origin = False
    portal_origin: Optional[str] = None
    for origin in data.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        origin_url = str(origin.get("origin") or "")
        if not _is_portal_origin(origin_url):
            continue
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
        "cookie_entries": cookie_entries,
        "has_session_cookies": bool(cookies),
    }


def parse_storage_state(state_path: Path) -> Optional[dict[str, Any]]:
    """Extract tokens, uid, and domain cookies from a Playwright storageState file."""
    if not state_path.is_file():
        return None
    try:
        data = load_secure_json(state_path)
    except (OSError, ValueError):
        return None
    return parse_storage_state_dict(data)


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
        context: Any = None
        pending_path = state_path.with_suffix(state_path.suffix + ".pending")
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_sec
            saw_auth_step = False
            portal_since: Optional[float] = None
            next_verify_at = 0.0

            while loop.time() < deadline:
                if page.is_closed():
                    return False, "浏览器窗口已关闭，未完成登录授权。"
                current_url = page.url or ""
                if _is_login_url(current_url) or not _is_portal_origin(current_url):
                    saw_auth_step = True
                    portal_since = None
                elif _is_portal_url(current_url):
                    now = loop.time()
                    if portal_since is None:
                        portal_since = now
                    elif now - portal_since >= 2.0 and now >= next_verify_at:
                        await page.wait_for_timeout(1500)
                        if not _is_portal_url(page.url or ""):
                            portal_since = None
                            saw_auth_step = True
                            continue
                        state = await context.storage_state()
                        parsed = parse_storage_state_dict(state)
                        can_verify = bool(parsed and (saw_auth_step or parsed.get("access_token")))
                        if can_verify and await _verify_saved_state(browser, state, login_url):
                            save_secure_json(pending_path, state)
                            os.replace(pending_path, state_path)
                            return True, f"已完成浏览器授权并保存加密会话状态至 {state_path}"
                        next_verify_at = loop.time() + 4.0
                        if saw_auth_step:
                            portal_since = None
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
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
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
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "authenticated": False,
            "reason": "未安装 playwright 依赖，请先执行 `uv sync`。",
        }

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
        context: Any = None
        try:
            context = await browser.new_context(storage_state=state)
            page = await context.new_page()
            if log:
                log("info", f"正在通过受控无头浏览器访问 {target_url} ...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2200)

            if not _matches_active_portal(page.url or "", base_url):
                return {
                    "authenticated": False,
                    "reason": "受控浏览器登录态已过期（重定向至登录页或非门户页面），请重新授权登录。",
                }

            live_state = await context.storage_state()
            parsed = parse_storage_state_dict(live_state) or {}
            token = parsed.get("access_token")
            effective_uid = uid or parsed.get("uid")
            domain_host = (urlparse(base_url).hostname or "").lower()

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
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 15000);
                    try {
                        const resp = await fetch(path, {
                            method: "POST",
                            credentials: "include",
                            headers,
                            body: "{}",
                            signal: controller.signal
                        });
                        let data = null;
                        try { data = await resp.json(); } catch (_) {}
                        return { status: resp.status, data };
                    } catch (_) {
                        return { status: 0, data: null };
                    } finally {
                        clearTimeout(timer);
                    }
                }

                function parseBool(val) {
                    if (typeof val === "boolean") return val;
                    if (typeof val === "number") return val !== 0;
                    if (typeof val === "string") {
                        const t = val.trim().toLowerCase();
                        if (t === "true" || t === "1" || t === "yes" || t === "checked" || t === "done" || t === "signed") return true;
                        if (t === "false" || t === "0" || t === "no" || t === "unchecked" || t === "pending" || t === "unsigned") return false;
                    }
                    return null;
                }

                function extractField(data, keys) {
                    const rootObj = (data && typeof data === "object") ? data : {};
                    const nestedObj = (rootObj.data && typeof rootObj.data === "object") ? rootObj.data : {};
                    for (const src of [nestedObj, rootObj]) {
                        for (const key of keys) {
                            if (Object.prototype.hasOwnProperty.call(src, key) && src[key] !== null && src[key] !== undefined) {
                                return src[key];
                            }
                        }
                    }
                    return undefined;
                }

                const stateKeys = [
                    "checkedIn", "checked_in", "todayCheckedIn", "today_checked_in",
                    "isCheckedIn", "is_checked_in", "signed"
                ];
                const activeKeys = ["active", "isActive", "is_active"];

                let statusRes = { status: 0, data: null };
                let inactiveFallback = null;
                for (const ep of endpoints) {
                    const candidate = await postJson(ep);
                    statusRes = candidate;
                    if (candidate.status === 404) continue;
                    if (
                        candidate.status === 200 &&
                        candidate.data &&
                        typeof candidate.data === "object" &&
                        parseBool(extractField(candidate.data, stateKeys)) !== true &&
                        parseBool(extractField(candidate.data, activeKeys)) === false
                    ) {
                        if (!inactiveFallback) inactiveFallback = candidate;
                        continue;
                    }
                    if (inactiveFallback && (candidate.status === 0 || candidate.status >= 500)) {
                        continue;
                    }
                    inactiveFallback = null;
                    break;
                }
                if (inactiveFallback) {
                    statusRes = inactiveFallback;
                }
                if (statusRes.status !== 200 || dryRun) {
                    return { statusRes, checkinRes: null };
                }

                const already = parseBool(extractField(statusRes.data, stateKeys));
                if (already) {
                    return { statusRes, checkinRes: null };
                }
                if (parseBool(extractField(statusRes.data, activeKeys)) === false) {
                    return { statusRes, checkinRes: null };
                }
                if (already === null) {
                    return { statusRes, checkinRes: null };
                }

                const checkinRes = await postJson(checkinEndpoint);
                return { statusRes, checkinRes };
            }
            """
            result = await page.evaluate(
                js_script,
                {
                    "endpoints": [
                        "/v2/billing/meter/checkin-activity-status",
                        "/v2/billing/meter/checkin-status",
                    ],
                    "checkinEndpoint": "/v2/billing/meter/daily-checkin",
                    "token": token,
                    "uid": effective_uid,
                    "domainHost": domain_host,
                    "dryRun": dry_run,
                },
            )
            # Only persist refreshed sliding cookies after confirming an authenticated response.
            status_code = (result.get("statusRes") or {}).get("status")
            if not dry_run and status_code == 200 and _matches_active_portal(page.url or "", base_url):
                save_secure_json(state_path, await context.storage_state())
            return {"authenticated": True, "active_url": base_url.rstrip("/"), **result}
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
            await browser.close()
