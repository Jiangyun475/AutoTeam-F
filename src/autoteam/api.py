"""AutoTeam HTTP API - 将 CLI 功能暴露为 HTTP 接口"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from autoteam.config import API_KEY
from autoteam.runtime_resources import collect_runtime_resource_snapshot, log_runtime_resource_snapshot
from autoteam.textio import read_text

logger = logging.getLogger(__name__)


def _body_preview(value, limit: int = 500) -> str:
    """Return a short, serializable preview for diagnostic responses."""
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = repr(value)
    return text[:limit]


def _safe_runtime_resource_snapshot() -> dict:
    try:
        return collect_runtime_resource_snapshot()
    except Exception as exc:
        logger.warning("[资源] runtime resource snapshot failed: %s", exc)
        return {"error": "runtime_resource_snapshot_failed"}


def _safe_ipv6_pool_status() -> dict:
    try:
        from autoteam.ipv6_pool import ipv6_pool

        return ipv6_pool.status()
    except Exception as exc:
        logger.warning("[IPv6Pool] status unavailable: %s", exc)
        return {
            "enabled": False,
            "required": False,
            "ok": False,
            "count": 0,
            "unhealthy_count": 0,
            "expired_count": 0,
            "last_error": str(exc),
            "preflight": {"ok": False, "errors": ["status_unavailable"], "warnings": []},
            "entries": [],
        }


def _safe_cliproxy_health(**kwargs) -> dict:
    try:
        from autoteam.cliproxy_health import get_cliproxy_health

        return get_cliproxy_health(**kwargs)
    except Exception as exc:
        logger.warning("[API] CLIProxyAPI health check failed: %s", exc)
        return {
            "ok": False,
            "safe_read_only": True,
            "management_api": {"ok": False, "reason": "health_check_exception"},
            "provider_auth": {
                "ok": False,
                "provider": "codex",
                "model": os.environ.get("CLIPROXY_HEALTH_MODEL") or "gpt-5.5",
                "reason": "health_check_exception",
                "total": 0,
                "available": 0,
                "check_type": "management_metadata",
                "canary_required": True,
            },
            "error": str(exc),
        }


def _safe_multi_master_status() -> dict:
    try:
        from autoteam.multi_master import build_multi_master_status

        return build_multi_master_status()
    except Exception as exc:
        logger.warning("[API] multi-master status failed: %s", exc)
        return {"enabled": False, "owner_count": 0, "owners": [], "error": str(exc)}


def _require_sync_target_configs(action_label: str):
    """Require at least one fully configured enabled remote sync target."""
    from autoteam.sync_targets import get_enabled_sync_targets, get_missing_target_configs

    enabled_targets = get_enabled_sync_targets()
    if not enabled_targets:
        raise HTTPException(
            status_code=400, detail=f"{action_label} 前请先在配置面板启用至少一个远端同步目标（CPA 或 Sub2API）"
        )

    missing = get_missing_target_configs(enabled_targets)
    if missing:
        fields = "、".join(f"{key}（{label}）" for key, label in missing)
        raise HTTPException(status_code=400, detail=f"{action_label} 前请先在配置面板填写：{fields}")


# Round 7 P2.4 — FastAPI 现代 lifespan handler 替代已废弃的 @app.on_event。
# 启动期:修复 auths 认证文件权限 + 启动 _auto_check_loop 后台线程。
# 停止期:发 _auto_check_stop 信号让线程优雅退出。
# 引用的 _auto_check_loop / _auto_check_stop / ensure_auth_file_permissions 都在
# module 后段定义,Python 闭包延迟绑定符合预期(yield 时才会解析名字)。
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    logger.info("[lifespan] starting")
    try:
        from autoteam.auth_storage import ensure_auth_file_permissions

        fixed = ensure_auth_file_permissions()
        if fixed:
            logger.info("[启动] 已修复 %d 个 auths 认证文件权限", fixed)
    except Exception as exc:
        logger.warning("[启动] 修复 auths 认证文件权限失败: %s", exc)

    # Round 9 RT-1 — 启动时跑 1 次 retroactive 重分类,解 "重启后 stale active" 根因。
    # spec/shared/master-subscription-health.md v1.1 §11.3。
    # 默认 ON,设 STARTUP_RETROACTIVE_DISABLE=1 关闭。后台线程跑,失败仅 warning。
    if os.getenv("STARTUP_RETROACTIVE_DISABLE", "").strip().lower() not in ("1", "true", "yes"):
        def _startup_retroactive():
            try:
                from autoteam.master_health import _apply_master_degraded_classification

                retro = _apply_master_degraded_classification()
                if retro and (retro.get("marked_grace") or retro.get("marked_standby") or retro.get("reverted_active")):
                    logger.info(
                        "[启动-retroactive] GRACE %d / STANDBY %d / 撤回 ACTIVE %d",
                        len(retro.get("marked_grace") or []),
                        len(retro.get("marked_standby") or []),
                        len(retro.get("reverted_active") or []),
                    )
                elif retro and retro.get("skipped_reason"):
                    logger.info("[启动-retroactive] skipped: %s", retro["skipped_reason"])
            except Exception as exc:
                logger.warning("[启动-retroactive] 异常(不阻塞启动): %s", exc)

        try:
            threading.Thread(target=_startup_retroactive, daemon=True).start()
        except Exception as exc:
            logger.warning("[启动-retroactive] 后台线程启动失败: %s", exc)

    try:
        from autoteam.accounts import STATUS_ACTIVE, is_account_disabled, load_accounts
        from autoteam.ipv6_pool import ipv6_pool

        active_emails = [
            acc["email"]
            for acc in load_accounts()
            if acc.get("status") == STATUS_ACTIVE
            and acc.get("email")
            and not _is_main_account_email(acc.get("email"))
            and not is_account_disabled(acc)
        ]
        ipv6_pool.start(active_emails=active_emails)
    except Exception as exc:
        logger.warning("[启动] IPv6 代理池启动失败: %s", exc)

    thread = threading.Thread(target=_auto_check_loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        logger.info("[lifespan] stopping")
        _auto_check_stop.set()
        try:
            _pw_executor.stop()
        except Exception as exc:
            logger.warning("[lifespan] stopping Playwright executor failed: %s", exc)
        try:
            from autoteam.ipv6_pool import ipv6_pool

            ipv6_pool.stop_all()
        except Exception as exc:
            logger.warning("[lifespan] stopping IPv6 proxy pool failed: %s", exc)
        try:
            from autoteam.display import stop_virtual_display

            stop_virtual_display()
        except Exception as exc:
            logger.warning("[lifespan] stopping virtual display failed: %s", exc)


app = FastAPI(
    title="AutoTeam API",
    description="ChatGPT Team 账号自动轮转管理 API",
    version="0.1.0",
    lifespan=app_lifespan,
)


# ---------------------------------------------------------------------------
# 版本端点 (SPEC-3 §5) - 不鉴权,纯只读
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    """镜像版本指纹响应 - 来自 Dockerfile build args。"""

    git_sha: str
    build_time: str


@app.get(
    "/api/version",
    response_model=VersionResponse,
    summary="返回镜像构建期注入的 git-sha 与时间戳",
    tags=["meta"],
)
def api_version() -> VersionResponse:
    return VersionResponse(
        git_sha=os.getenv("AUTOTEAM_GIT_SHA", "unknown"),
        build_time=os.getenv("AUTOTEAM_BUILD_TIME", "unknown"),
    )


# ---------------------------------------------------------------------------
# API Key 鉴权中间件
# ---------------------------------------------------------------------------

_AUTH_SKIP_PATHS = {
    "/api/auth/check",
    "/api/setup/status",
    "/api/setup/save",
    "/api/version",
    "/api/setup/dns/check",  # Read-only setup diagnostic; route enforces Bearer once API_KEY exists.
    "/api/mail-provider/probe",  # SPEC-1 §3.5 — 条件鉴权:API_KEY 配置后路由内强制 Bearer
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 不鉴权的路径：非 /api 路径、auth/check 端点
    if not path.startswith("/api/") or path in _AUTH_SKIP_PATHS:
        return await call_next(request)
    # 未配置 API_KEY 则跳过鉴权
    if not API_KEY:
        return await call_next(request)
    # 从 header 或 query param 获取 key
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("key", "")
    if token != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "未授权，请提供有效的 API Key"})
    return await call_next(request)


@app.get("/api/auth/check")
def check_auth(request: Request):
    """验证 API Key 是否有效。未配置 API_KEY 时始终返回成功。"""
    if not API_KEY:
        return {"authenticated": True, "auth_required": False}
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == API_KEY:
        return {"authenticated": True, "auth_required": True}
    return JSONResponse(status_code=401, content={"authenticated": False, "auth_required": True})


# ---------------------------------------------------------------------------
# 初始配置 API（无需鉴权）
# ---------------------------------------------------------------------------


class SetupConfig(BaseModel):
    """`/api/setup/save` 请求体。SPEC-1 §2.1 — 含 cf_temp_email + maillab 两套 mail 字段。"""

    MAIL_PROVIDER: Literal["cf_temp_email", "cloudflare_temp_email", "maillab"] = "cf_temp_email"
    CLOUDMAIL_BASE_URL: str = ""
    CLOUDMAIL_EMAIL: str = ""
    CLOUDMAIL_PASSWORD: str = ""
    CLOUDMAIL_DOMAIN: str = ""
    MAILLAB_API_URL: str = ""
    MAILLAB_USERNAME: str = ""
    MAILLAB_PASSWORD: str = ""
    MAILLAB_DOMAIN: str = ""
    CPA_URL: str = "http://127.0.0.1:8317"
    CPA_KEY: str = ""
    ROTATE_NEW_ACCOUNT_MODE: str = "domain_auto_join_first"
    AUTOTEAM_AUTO_JOIN_DOMAINS: str = "auto"
    ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE: str | bool = "true"
    PLAYWRIGHT_PROXY_URL: str = ""
    PLAYWRIGHT_PROXY_BYPASS: str = ""
    API_KEY: str = ""


class ProbeErrorCode(str, Enum):
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN_DOMAIN = "FORBIDDEN_DOMAIN"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    EMPTY_DOMAIN_LIST = "EMPTY_DOMAIN_LIST"
    UNKNOWN = "UNKNOWN"


class MailProviderProbeRequest(BaseModel):
    """`/api/mail-provider/probe` 请求体。SPEC-1 §2.1。"""

    provider: Literal["cf_temp_email", "maillab"]
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    base_url: str = Field(..., min_length=1, max_length=512)
    admin_password: str = ""
    username: str = ""
    password: str = ""
    domain: str = ""
    bearer_token: str = ""

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        return (v or "").strip().lstrip("@")


class MailProviderProbeResponse(BaseModel):
    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    error_code: ProbeErrorCode | None = None
    message: str | None = None
    hint: str | None = None
    warnings: list[str] = []
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None
    leaked_probe: dict | None = None


class DNSRecordCheck(BaseModel):
    type: Literal["A", "AAAA", "CNAME", "MX", "TXT"]
    name: str = Field(..., min_length=1, max_length=253)
    expected: str = Field(..., min_length=1, max_length=2048)


class DNSDiagnosticRequest(BaseModel):
    domain: str = ""
    mail_host: str = ""
    mail_ip: str = ""
    mx_target: str = ""
    spf_value: str = ""
    openai_domain_verification: str = ""
    records: list[DNSRecordCheck] = Field(default_factory=list)

    @field_validator("domain", "mail_host", "mx_target")
    @classmethod
    def _normalize_dns_name(cls, v: str) -> str:
        return (v or "").strip().lstrip("@").rstrip(".").lower()


class DNSCheckResponse(BaseModel):
    type: Literal["A", "AAAA", "CNAME", "MX", "TXT"]
    name: str
    expected: str
    observed: list[str] = Field(default_factory=list)
    ok: bool
    error: str | None = None


class DNSDiagnosticResponse(BaseModel):
    ok: bool
    domain: str = ""
    checks: list[DNSCheckResponse] = Field(default_factory=list)
    all_ok: bool = False
    safe_read_only: bool = True
    error_code: str | None = None
    message: str | None = None


_CF_MAIL_KEYS = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD", "CLOUDMAIL_DOMAIN"}
_MAILLAB_KEYS = {"MAILLAB_API_URL", "MAILLAB_USERNAME", "MAILLAB_PASSWORD", "MAILLAB_DOMAIN"}
_ROTATE_NEW_ACCOUNT_MODES = {"domain_auto_join_first", "invite_first", "direct_first"}


def _normalize_setup_bool(value, *, field: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "y", "t"}:
        return "true"
    if text in {"0", "false", "no", "off", "disabled", "n", "f"}:
        return "false"
    raise ValueError(f"{field} 必须是 true 或 false")


def _normalize_rotation_setup_values(data: dict) -> None:
    mode = str(data.get("ROTATE_NEW_ACCOUNT_MODE", "") or "").strip().lower()
    if mode:
        if mode not in _ROTATE_NEW_ACCOUNT_MODES:
            raise ValueError("ROTATE_NEW_ACCOUNT_MODE 必须是 domain_auto_join_first、invite_first 或 direct_first")
        data["ROTATE_NEW_ACCOUNT_MODE"] = mode

    domains = str(data.get("AUTOTEAM_AUTO_JOIN_DOMAINS", "") or "").strip()
    if domains:
        data["AUTOTEAM_AUTO_JOIN_DOMAINS"] = ",".join(
            part.strip().lower().lstrip("@").rstrip(".")
            for part in domains.split(",")
            if part.strip()
        ) or "auto"
    else:
        data["AUTOTEAM_AUTO_JOIN_DOMAINS"] = ""

    data["ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE"] = _normalize_setup_bool(
        data.get("ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE", "true"),
        field="ROTATE_DOMAIN_AUTO_JOIN_FALLBACK_INVITE",
    )


@app.get("/api/setup/status")
def get_setup_status():
    """检查配置是否完整 — SPEC-1 §3.6:按 provider 动态标 optional。"""
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _read_env

    env = _read_env()
    provider = (env.get("MAIL_PROVIDER") or os.environ.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    fields = []
    all_ok = True
    for key, prompt, default, optional in REQUIRED_CONFIGS:
        # 不属于当前 provider 的 mail 字段强制 optional
        if provider == "maillab" and key in _CF_MAIL_KEYS:
            optional = True
        elif provider in ("cf_temp_email", "cloudflare_temp_email") and key in _MAILLAB_KEYS:
            optional = True
        val = env.get(key, "") or os.environ.get(key, "")
        ok = bool(val)
        if not ok and not optional:
            all_ok = False
        fields.append({"key": key, "prompt": prompt, "default": default, "optional": optional, "configured": ok})
    return {"configured": all_ok, "fields": fields, "provider": provider}


@app.post("/api/setup/save")
def post_setup_save(config: SetupConfig):
    """保存配置到 .env 并验证连通性"""
    import secrets as _secrets

    from autoteam.setup_wizard import REQUIRED_CONFIGS, _write_env

    data = config.model_dump()
    defaults = {key: default for key, _prompt, default, _optional in REQUIRED_CONFIGS}
    if not data.get("CPA_URL"):
        data["CPA_URL"] = defaults.get("CPA_URL", "http://127.0.0.1:8317")
    if not data.get("API_KEY"):
        data["API_KEY"] = _secrets.token_urlsafe(24)
    try:
        _normalize_rotation_setup_values(data)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"message": str(exc), "api_key": data["API_KEY"]})

    # SPEC-1 §3.6 — 按 provider 互斥写入,避免无关字段污染 .env
    provider = (data.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        skip_keys = set(_MAILLAB_KEYS)
    elif provider == "maillab":
        # CLOUDMAIL_DOMAIN 保留作 maillab 的 fallback;仅跳过 base_url/password
        skip_keys = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD"}
    else:
        skip_keys = set()

    clearable_fields = {"AUTOTEAM_AUTO_JOIN_DOMAINS", "PLAYWRIGHT_PROXY_URL", "PLAYWRIGHT_PROXY_BYPASS"}
    for key, value in data.items():
        if key in skip_keys:
            continue
        if value or key in clearable_fields:
            _write_env(key, value)
            os.environ[key] = value

    # 重新加载模块
    import importlib

    import autoteam.config

    importlib.reload(autoteam.config)
    try:
        import autoteam.cloudmail

        importlib.reload(autoteam.cloudmail)
    except Exception:
        pass

    # 验证连通性
    errors = []
    from autoteam.setup_wizard import _verify_cloudmail, _verify_cpa

    if not _verify_cloudmail():
        errors.append("CloudMail 连接失败")
    if not _verify_cpa():
        errors.append("CPA 连接失败")

    if errors:
        return JSONResponse(status_code=400, content={"message": "、".join(errors), "api_key": data["API_KEY"]})

    # 更新运行时 API_KEY
    global API_KEY
    API_KEY = data["API_KEY"]

    return {"message": "配置保存成功", "api_key": data["API_KEY"], "configured": True}


# ---------------------------------------------------------------------------
# Mail Provider 在线探测 (SPEC-1 §3.5) — 三步分阶段验证
# ---------------------------------------------------------------------------

_probe_rate_buckets: dict[str, list[float]] = {}
_probe_rate_lock = threading.Lock()


def _enforce_probe_rate_limit(request: Request, max_per_min: int = 60):
    """SPEC §NFR-速率限制:setup 阶段 (无 API_KEY) 单 IP 60 req/min,防扫描。"""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _probe_rate_lock:
        bucket = _probe_rate_buckets.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= max_per_min:
            raise HTTPException(status_code=429, detail="probe 请求过频,请稍后再试")
        bucket.append(now)


def _enforce_setup_probe_access(request: Request):
    """Setup diagnostics are open before API_KEY exists and Bearer-gated after setup."""
    from autoteam.config import API_KEY as _key

    if _key:
        auth_header = request.headers.get("authorization", "")
        if not (auth_header.startswith("Bearer ") and auth_header[7:] == _key):
            raise HTTPException(status_code=401, detail="API_KEY 已配置,请提供 Bearer token")
    else:
        _enforce_probe_rate_limit(request)


@app.post("/api/mail-provider/probe", response_model=MailProviderProbeResponse)
def post_mail_provider_probe(req: MailProviderProbeRequest, request: Request):
    """SPEC-1 §3.5 — 三步分阶段:fingerprint → credentials → domain_ownership。

    鉴权策略:
      - API_KEY 未配置(setup 阶段):放行,但走 IP 速率限制
      - API_KEY 已配置:强制 Bearer(_AUTH_SKIP_PATHS 已加白,这里手工二次校验)
    """
    _enforce_setup_probe_access(request)

    from autoteam.mail import probe as mail_probe

    try:
        if req.step == "fingerprint":
            result = mail_probe.probe_fingerprint(req.base_url, req.provider)
        elif req.step == "credentials":
            result = mail_probe.probe_credentials(
                req.base_url,
                req.provider,
                username=req.username,
                password=req.password,
                admin_password=req.admin_password,
            )
        else:  # domain_ownership
            result = mail_probe.probe_domain_ownership(
                req.base_url,
                req.provider,
                bearer_token=req.bearer_token,
                admin_password=req.admin_password,
                domain=req.domain,
                username=req.username,
                password=req.password,
            )
    except mail_probe.ProbeError as exc:
        return MailProviderProbeResponse(
            ok=False,
            step=req.step,
            error_code=ProbeErrorCode(exc.error_code) if exc.error_code in ProbeErrorCode.__members__ else ProbeErrorCode.UNKNOWN,
            message=exc.message,
            hint=exc.hint,
        )
    except Exception as exc:  # noqa: BLE001 — 兜底
        logger.exception("[probe] %s 异常: %s", req.step, exc)
        return MailProviderProbeResponse(
            ok=False,
            step=req.step,
            error_code=ProbeErrorCode.UNKNOWN,
            message=str(exc),
        )

    # ProbeResult → ProbeResponse(忽略内部 bearer_token,前端不持有)
    payload = {k: v for k, v in vars(result).items() if k != "bearer_token"}
    if "error_code" in payload and payload["error_code"]:
        try:
            payload["error_code"] = ProbeErrorCode(payload["error_code"])
        except ValueError:
            payload["error_code"] = ProbeErrorCode.UNKNOWN
    return MailProviderProbeResponse(**payload)


@app.post("/api/setup/dns/check", response_model=DNSDiagnosticResponse)
def post_setup_dns_check(req: DNSDiagnosticRequest, request: Request):
    """Read-only DNS diagnostics for setup/domain verification records."""
    _enforce_setup_probe_access(request)

    from autoteam import dns_diagnostics

    admin_payload = req.model_dump(exclude={"records"})
    admin_payload["email_domain"] = admin_payload.pop("domain", "")
    extra_checks = [record.model_dump() for record in req.records]
    try:
        result = dns_diagnostics.check_admin_dns(admin_payload, extra_checks=extra_checks)
    except dns_diagnostics.DNSDiagnosticError as exc:
        return DNSDiagnosticResponse(ok=False, error_code="INVALID_REQUEST", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - endpoint must not expose stack traces during setup
        logger.exception("[dns] setup DNS check failed: %s", exc)
        return DNSDiagnosticResponse(ok=False, error_code="UNKNOWN", message=str(exc))

    return DNSDiagnosticResponse(ok=bool(result.get("all_ok")), **result)


# ---------------------------------------------------------------------------
# 后台任务管理
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_playwright_lock = threading.Lock()
_current_task_id: str | None = None
_admin_login_api = None
_admin_login_step: str | None = None
_main_codex_flow = None
_main_codex_step: str | None = None
_main_codex_action: str | None = None
_manual_account_flow = None
MAX_TASK_HISTORY = 50
MAX_TASK_PROGRESS_HISTORY = 200


# ---------------------------------------------------------------------------
# Playwright 专用线程执行器（解决跨线程调用问题）
# ---------------------------------------------------------------------------

import queue as _queue

from autoteam._playwright_guard import assert_sync_context


class _PlaywrightExecutor:
    """将 Playwright 操作派发到专用线程执行，避免跨线程错误"""

    def __init__(self):
        self._queue: _queue.Queue = _queue.Queue()
        self._thread: threading.Thread | None = None

    def _worker(self):
        # SPEC-4 §3.2: worker 线程入口必须在普通线程,不允许 asyncio loop
        assert_sync_context()
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, args, kwargs, result_event, result_holder = item
            try:
                result_holder["result"] = func(*args, **kwargs)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()

    def ensure_started(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def run(self, func, *args, **kwargs):
        """在专用线程中执行函数，阻塞等待结果(默认 5 分钟)"""
        return self.run_with_timeout(300, func, *args, **kwargs)

    def run_with_timeout(self, timeout: float, func, *args, **kwargs):
        """
        明确指定超时时间(秒)。适用于批量/长耗时操作。

        注意:超时后 worker 线程仍会继续跑完当前 func(Playwright 操作无法安全中断),
        后续通过 _pw_executor 提交的调用会在队列里等它自然完成。调用方需要自己
        确保不会越过 _playwright_lock 边界并发触发这种情况。
        """
        # SPEC-4 §3.2: 主线程前置检查,拦下 asyncio loop 误入
        assert_sync_context()
        self.ensure_started()
        result_event = threading.Event()
        result_holder: dict = {}
        self._queue.put((func, args, kwargs, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(
                f"Playwright executor timed out after {timeout}s while running {getattr(func, '__name__', repr(func))}"
            )
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None


_pw_executor = _PlaywrightExecutor()


def _current_busy_detail(default_message: str):
    if _admin_login_api:
        return {
            "message": default_message,
            "running_task": {
                "task_id": "admin-login",
                "command": "admin-login",
                "started_at": None,
            },
        }

    if _main_codex_flow:
        command = "main-codex-login" if _main_codex_action == "login" else "main-codex-sync"
        return {
            "message": default_message,
            "running_task": {
                "task_id": command,
                "command": command,
                "started_at": None,
            },
        }

    running = _tasks.get(_current_task_id, {})
    return {
        "message": default_message,
        "running_task": {
            "task_id": _current_task_id,
            "command": running.get("command", "unknown"),
            "started_at": running.get("started_at"),
        },
    }


def _prune_tasks():
    """保留最近 MAX_TASK_HISTORY 个任务"""
    if len(_tasks) <= MAX_TASK_HISTORY:
        return
    sorted_ids = sorted(_tasks, key=lambda k: _tasks[k]["created_at"])
    for tid in sorted_ids[: len(_tasks) - MAX_TASK_HISTORY]:
        if _tasks[tid]["status"] in ("completed", "failed"):
            del _tasks[tid]


_TASK_VALIDATION_COMMANDS = {"auto-fill", "auto-rotate", "rotate", "cleanup", "fill"}
_ROTATION_VALIDATION_COMMANDS = {"auto-fill", "auto-rotate", "rotate", "fill"}
_rotation_validation_cooldown = {
    "next_rotate_after": 0.0,
    "recorded_at": 0.0,
    "severity": "",
    "reason": "",
}


def _auto_check_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _validation_reasons(validation: dict | None) -> list[str]:
    if not isinstance(validation, dict):
        return []
    reasons = validation.get("reasons")
    if isinstance(reasons, list):
        return [str(item) for item in reasons if str(item)]
    reason = validation.get("reason")
    return [str(reason)] if reason else []


def _validation_failure_reason(validation: dict | None) -> str:
    reasons = _validation_reasons(validation)
    return "; ".join(reasons) if reasons else "unknown"


def _validation_is_hard_failure(validation: dict | None) -> bool:
    if not isinstance(validation, dict):
        return False
    if validation.get("severity") == "degraded":
        return False
    return not validation.get("ok", True)


def _record_rotation_validation_decision(command: str, validation: dict | None) -> None:
    if command not in _ROTATION_VALIDATION_COMMANDS or not isinstance(validation, dict):
        return

    severity = str(validation.get("severity") or ("ok" if validation.get("ok", True) else "failed"))
    if severity == "ok":
        _rotation_validation_cooldown.update(
            {
                "next_rotate_after": 0.0,
                "recorded_at": time.time(),
                "severity": "ok",
                "reason": "",
            }
        )
        return

    seconds = _auto_check_int_env(
        "AUTO_CHECK_DEGRADED_COOLDOWN_SECONDS" if severity == "degraded" else "AUTO_CHECK_FAILED_COOLDOWN_SECONDS",
        120 if severity == "degraded" else 300,
    )
    now = time.time()
    next_rotate_after = now + seconds if seconds > 0 else 0.0
    reason = _validation_failure_reason(validation)
    _rotation_validation_cooldown.update(
        {
            "next_rotate_after": next_rotate_after,
            "recorded_at": now,
            "severity": severity,
            "reason": reason,
        }
    )
    followup = validation.setdefault("followup", {})
    followup["cooldown_seconds"] = seconds
    followup["next_eligible_rotation_at"] = next_rotate_after


def _rotation_validation_cooldown_remaining() -> float:
    next_rotate_after = float(_rotation_validation_cooldown.get("next_rotate_after") or 0.0)
    if next_rotate_after <= 0:
        return 0.0
    return max(0.0, next_rotate_after - time.time())


def _log_task_runtime_validation(command: str) -> dict | None:
    """Record post-task runtime truth so completed tasks do not hide broken pool state."""
    if command not in _TASK_VALIDATION_COMMANDS:
        return None

    try:
        from autoteam.accounts import STATUS_ACTIVE, STATUS_AUTH_INVALID, is_account_disabled, load_accounts
        from autoteam.codex_auth import check_codex_quota
        from autoteam.manager import _pool_active_target

        accounts = load_accounts()
        target_seats = _resolve_auto_check_target_seats(_auto_check_config)
        pool_target = _pool_active_target(target_seats)
        team_count = _auto_check_team_member_count(timeout_seconds=20, retries=1)

        active_with_auth = 0
        auth_pending = 0
        auth_file_count = 0
        quota_ok = 0
        quota_exhausted = 0
        quota_auth_error = 0
        quota_unknown = 0
        primary_pcts: list[int] = []

        for acc in accounts:
            if _is_main_account_email(acc.get("email")) or is_account_disabled(acc):
                continue
            status = acc.get("status")
            auth_path = _resolve_status_auth_file(acc)
            if status == STATUS_AUTH_INVALID:
                auth_pending += 1
            if auth_path:
                auth_file_count += 1
            if status != STATUS_ACTIVE or not auth_path:
                continue

            active_with_auth += 1
            try:
                auth_data = json.loads(read_text(Path(auth_path)))
                access_token = auth_data.get("access_token")
                if not access_token:
                    quota_unknown += 1
                    continue
                quota_status, info = check_codex_quota(access_token)
                if quota_status == "ok":
                    quota_ok += 1
                elif quota_status == "exhausted":
                    quota_exhausted += 1
                elif quota_status == "auth_error":
                    quota_auth_error += 1
                else:
                    quota_unknown += 1
                if isinstance(info, dict) and isinstance(info.get("primary_pct"), (int, float)):
                    primary_pcts.append(int(info["primary_pct"]))
            except Exception:
                quota_unknown += 1

        logger.info(
            "[验收] %s 后: team=%s/%s active_with_auth=%d/%d auth_invalid=%d auth_files=%d "
            "quota_ok=%d exhausted=%d auth_error=%d unknown=%d primary_pct=%s",
            command,
            team_count if team_count >= 0 else "unknown",
            target_seats,
            active_with_auth,
            pool_target,
            auth_pending,
            auth_file_count,
            quota_ok,
            quota_exhausted,
            quota_auth_error,
            quota_unknown,
            ",".join(str(v) for v in primary_pcts[:8]) or "-",
        )
        validation = {
            "ok": True,
            "severity": "ok",
            "operation_success": True,
            "pool_health_ok": True,
            "followup_required": False,
            "reasons": [],
            "team_count": team_count,
            "target_seats": target_seats,
            "pool_target": pool_target,
            "active_with_auth": active_with_auth,
            "auth_pending": auth_pending,
            "auth_file_count": auth_file_count,
            "quota_ok": quota_ok,
            "quota_exhausted": quota_exhausted,
            "quota_auth_error": quota_auth_error,
            "quota_unknown": quota_unknown,
            "primary_pct": primary_pcts[:8],
        }
        hard_reasons: list[str] = []
        followup_reasons: list[str] = []

        if team_count >= 0 and team_count != target_seats:
            hard_reasons.append(f"team_count={team_count}/{target_seats}")
        if active_with_auth < pool_target:
            hard_reasons.append(f"active_with_auth={active_with_auth}/{pool_target}")
        if quota_auth_error > 0:
            hard_reasons.append(f"quota_auth_error={quota_auth_error}")
        if quota_ok < pool_target:
            followup_reasons.append(f"quota_ok={quota_ok}/{pool_target}")

        cpa_credential_gate = _collect_cpa_credential_gate()
        if cpa_credential_gate.get("enabled"):
            validation["cpa_credential_gate"] = cpa_credential_gate
            validation["provider_auth_available"] = cpa_credential_gate.get("available")
            if _cpa_provider_auth_below_pool_target(cpa_credential_gate, pool_target):
                available = int(cpa_credential_gate.get("available") or 0)
                followup_reasons.append(f"provider_auth_available={available}/{pool_target}")

        if hard_reasons:
            validation.update(
                {
                    "ok": False,
                    "severity": "failed",
                    "operation_success": False,
                    "pool_health_ok": False,
                    "followup_required": True,
                    "reasons": hard_reasons + followup_reasons,
                    "reason": "; ".join(hard_reasons + followup_reasons),
                }
            )
        elif followup_reasons:
            validation.update(
                {
                    "ok": False,
                    "severity": "degraded",
                    "operation_success": True,
                    "pool_health_ok": False,
                    "followup_required": True,
                    "reasons": followup_reasons,
                    "reason": "; ".join(followup_reasons),
                    "followup": {
                        "recommended_command": "rotate",
                        "reason": "; ".join(followup_reasons),
                    },
                }
            )

        return validation
    except Exception as exc:
        logger.warning("[验收] %s 后运行态核查失败: %s", command, exc)
        return None


def _run_task(task_id: str, func, *args, **kwargs):
    """在后台线程中执行任务"""
    from autoteam import cancel_signal

    global _current_task_id
    task = _tasks[task_id]

    _playwright_lock.acquire()
    # 顺序很关键: 先 reset() 再暴露 _current_task_id。否则 post_task_cancel 在
    # 两行之间读到新 task_id 并 request_cancel(),随后被我们的 reset() 清掉,
    # 用户的取消请求被静默吞掉。
    cancel_signal.reset()
    _current_task_id = task_id
    task["status"] = "running"
    task["started_at"] = time.time()
    task["last_progress_at"] = task["started_at"]
    task["progress"] = "started"
    _record_task_progress(task, "started", task["started_at"])

    try:
        result = func(*args, **kwargs)
        # 任务完成但中途确实收到取消 → 标 cancelled
        task["status"] = "cancelled" if cancel_signal.is_cancelled() else "completed"
        task["result"] = result
    except Exception as e:
        task["status"] = "cancelled" if cancel_signal.is_cancelled() else "failed"
        task["error"] = str(e)
        logger.error("[API] 任务 %s %s: %s", task_id[:8], task["status"], e)
    finally:
        _record_task_progress(task, "runtime_validation")
        validation = _log_task_runtime_validation(task.get("command", ""))
        task["finished_at"] = time.time()
        task["last_progress_at"] = task["finished_at"]
        if validation is not None:
            task["validation"] = validation
            _record_rotation_validation_decision(task.get("command", ""), validation)
            if task["status"] == "completed" and _validation_is_hard_failure(validation):
                task["status"] = "failed"
                task["error"] = f"runtime validation failed: {_validation_failure_reason(validation)}"
        _record_task_progress(task, "finished", task["finished_at"])
        _current_task_id = None
        _playwright_lock.release()


def _record_task_progress(task: dict, stage: str, now: float | None = None) -> None:
    """Record task stage timings for live diagnostics."""
    timestamp = time.time() if now is None else now
    stage_name = str(stage or task.get("progress") or "progress")
    history = task.setdefault("progress_history", [])

    if history and history[-1].get("stage") == stage_name:
        history[-1]["last_at"] = timestamp
        history[-1]["duration_sec"] = round(timestamp - float(history[-1].get("started_at", timestamp)), 3)
        return

    if history:
        previous = history[-1]
        previous["ended_at"] = timestamp
        previous["duration_sec"] = round(timestamp - float(previous.get("started_at", timestamp)), 3)

    history.append({"stage": stage_name, "started_at": timestamp, "last_at": timestamp})
    if len(history) > MAX_TASK_PROGRESS_HISTORY:
        del history[: len(history) - MAX_TASK_PROGRESS_HISTORY]


def bump_task_progress(stage: str = "") -> None:
    """Update the current background task heartbeat and optional stage label."""
    tid = _current_task_id
    if not tid:
        return
    task = _tasks.get(tid)
    if not task:
        return
    now = time.time()
    task["last_progress_at"] = now
    if stage:
        task["progress"] = stage
        _record_task_progress(task, stage, now)


def _start_task(command: str, func, params: dict, *args, **kwargs) -> dict:
    """创建并启动后台任务，返回任务信息"""
    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再试"))
    _playwright_lock.release()

    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "command": command,
        "params": params,
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "last_progress_at": time.time(),
        "progress": "pending",
        "progress_history": [],
    }
    _tasks[task_id] = task
    _prune_tasks()

    thread = threading.Thread(target=_run_task, args=(task_id, func, *args), kwargs=kwargs, daemon=True)
    thread.start()

    return task


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class TaskParams(BaseModel):
    target: int = 3
    leave_workspace: bool = False  # cmd_fill 专用：True 表示生产免费号（注册后退出 Team 走 personal OAuth）


class MultiMasterFillParams(BaseModel):
    target: int = 3
    owner_workers: int | None = None
    direct_parallel: int | None = None
    workspace_ids: list[str] | None = None
    dry_run: bool = False


class CleanupParams(BaseModel):
    max_seats: int | None = None


class AdminEmailParams(BaseModel):
    email: str


class AdminSessionParams(BaseModel):
    email: str
    session_token: str


class AdminPasswordParams(BaseModel):
    password: str


class AdminCodeParams(BaseModel):
    code: str


class AdminWorkspaceParams(BaseModel):
    option_id: str


class ManualAccountCallbackParams(BaseModel):
    redirect_url: str


class TeamMemberRemoveParams(BaseModel):
    email: str
    user_id: str
    type: str


class RegisterDomainParams(BaseModel):
    domain: str
    verify: bool = True  # 默认写入前试探一次 CloudMail 是否接受该域


class PreferredSeatTypeParams(BaseModel):
    """SPEC-2 FR-G — 邀请席位偏好。"default"(优先 PATCH 升级 ChatGPT 完整席位) | "codex"(锁 codex-only)"""
    value: str  # "default" | "codex"


class SyncProbeParams(BaseModel):
    """SPEC-2 FR-E — sync_account_states 被踢探测的并发上限和去重冷却。"""
    concurrency: int | None = None  # 1..16
    cooldown_minutes: int | None = None  # 1..1440


class DeleteBatchParams(BaseModel):
    emails: list[str]
    continue_on_error: bool = True  # 部分失败时继续剩余账号,False 则遇错即停


class AccountDisableParams(BaseModel):
    emails: list[str]


class AuthRepairRecoverParams(BaseModel):
    apply: bool = False
    emails: list[str] = Field(default_factory=list)


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_main_account_email(email: str | None) -> bool:
    from autoteam.admin_state import get_admin_email

    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def _quota_snapshot_status(quota_info: dict | None) -> str:
    if not isinstance(quota_info, dict):
        return ""

    values = []
    for key in ("primary_pct", "weekly_pct"):
        value = quota_info.get(key)
        if isinstance(value, (int, float)):
            values.append(value)

    if not values:
        return ""
    return "exhausted" if any(value >= 100 for value in values) else "active"


def _resolve_status_auth_file(acc: dict) -> str:
    auth_file = (acc.get("auth_file") or "").strip()
    if auth_file and Path(auth_file).exists():
        return auth_file

    if _is_main_account_email(acc.get("email")):
        from autoteam.codex_auth import get_saved_main_auth_file

        saved_auth_file = get_saved_main_auth_file()
        if saved_auth_file and Path(saved_auth_file).exists():
            return saved_auth_file

    return ""


def _display_account_status(acc: dict, quota_snapshot: dict | None = None) -> str:
    from autoteam.accounts import is_account_disabled

    status = acc.get("status", "")
    if not _is_main_account_email(acc.get("email")):
        if is_account_disabled(acc):
            return "disabled"
        return status

    quota_status = _quota_snapshot_status(quota_snapshot) or _quota_snapshot_status(acc.get("last_quota"))
    if quota_status:
        return quota_status

    return "active" if _resolve_status_auth_file(acc) else status


def _quota_int(quota_info: dict | None, key: str) -> int | None:
    if not isinstance(quota_info, dict):
        return None
    value = quota_info.get(key)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _quota_ts(quota_info: dict | None, key: str) -> float | None:
    if not isinstance(quota_info, dict):
        return None
    value = quota_info.get(key)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _format_pool_wait(seconds: float) -> str:
    total_minutes = max(1, int((seconds + 59) // 60))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _quota_blocking_window(
    *,
    primary_remaining_pct: int | None,
    weekly_remaining_pct: int | None,
    primary_resets_at: float | None,
    weekly_resets_at: float | None,
    now: float,
) -> tuple[str, float | None]:
    if primary_remaining_pct is not None and primary_remaining_pct <= 0:
        if not primary_resets_at or primary_resets_at > now:
            return "primary_exhausted", primary_resets_at
    if weekly_remaining_pct is not None and weekly_remaining_pct <= 0:
        if not weekly_resets_at or weekly_resets_at > now:
            return "weekly_exhausted", weekly_resets_at
    return "", None


def _account_standby_since(acc: dict) -> float:
    for key in ("standby_since", "quota_exhausted_at", "last_active_at", "created_at"):
        value = acc.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def _account_cooldown_until(acc: dict) -> float | None:
    for key in ("quota_cooldown_until", "quota_resets_at"):
        value = acc.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _account_pool_schedule(acc: dict, quota_snapshot: dict | None = None) -> dict:
    raw_status = str(acc.get("status") or "")
    now = time.time()
    standby_like = raw_status in ("standby", "exhausted")
    if (
        standby_like
        and isinstance(acc.get("standby_quota_snapshot"), dict)
        and isinstance(acc.get("quota_snapshot_recorded_at"), (int, float))
    ):
        quota_info = acc.get("standby_quota_snapshot")
        quota_scope = "standby_snapshot"
    elif isinstance(quota_snapshot, dict) and not standby_like:
        quota_info = quota_snapshot
        quota_scope = "live"
    else:
        quota_info = None if standby_like else acc.get("last_quota")
        quota_scope = "standby_snapshot" if standby_like else "live"
    primary_pct = _quota_int(quota_info, "primary_pct")
    weekly_pct = _quota_int(quota_info, "weekly_pct")
    primary_remaining_pct = max(0, min(100, 100 - primary_pct)) if primary_pct is not None else None
    weekly_remaining_pct = max(0, min(100, 100 - weekly_pct)) if weekly_pct is not None else None
    primary_resets_at = _quota_ts(quota_info, "primary_resets_at")
    weekly_resets_at = _quota_ts(quota_info, "weekly_resets_at")
    quota_resets_at = _account_cooldown_until(acc)
    auth_retry_after = acc.get("auth_retry_after")
    if not isinstance(auth_retry_after, (int, float)) or auth_retry_after <= 0:
        auth_retry_after = None
    standby_since = _account_standby_since(acc)

    try:
        from autoteam.config import AUTO_CHECK_THRESHOLD

        quota_threshold = int(AUTO_CHECK_THRESHOLD)
    except Exception:
        quota_threshold = 10

    next_usable_at = None
    reason = "unknown"
    detail_parts: list[str] = []
    bucket = 60

    if _is_main_account_email(acc.get("email")):
        bucket = 0
        reason = "main_account"
    elif acc.get("disabled"):
        bucket = 90
        reason = "disabled"
    elif raw_status == "active":
        bucket = 10
        next_usable_at = primary_resets_at
        reason = "active_5h_reset"
    elif standby_like:
        quota_block_reason, quota_block_until = _quota_blocking_window(
            primary_remaining_pct=primary_remaining_pct,
            weekly_remaining_pct=weekly_remaining_pct,
            primary_resets_at=primary_resets_at,
            weekly_resets_at=weekly_resets_at,
            now=now,
        )
        if auth_retry_after and auth_retry_after > now:
            bucket = 45
            next_usable_at = auth_retry_after
            reason = "auth_retry_after"
        elif quota_resets_at and quota_resets_at > now:
            bucket = 40
            next_usable_at = quota_resets_at
            reason = "quota_cooldown"
        elif quota_block_reason == "weekly_exhausted":
            bucket = 55
            next_usable_at = quota_block_until
            reason = quota_block_reason
        else:
            bucket = 20
            next_usable_at = None
            reason = "ready_now"
            if weekly_remaining_pct is not None and weekly_remaining_pct < quota_threshold:
                reason = "ready_low_weekly"
    elif raw_status == "personal":
        bucket = 30
        next_usable_at = primary_resets_at
        reason = "personal"
    elif raw_status in ("pending", "auth_invalid", "orphan"):
        bucket = 70
        if auth_retry_after and auth_retry_after > now:
            next_usable_at = auth_retry_after
            reason = "auth_retry_after"
        else:
            reason = raw_status

    if standby_like:
        if reason == "quota_cooldown" and quota_resets_at and quota_resets_at > now:
            detail_parts.append(f"冷却中 {_format_pool_wait(quota_resets_at - now)}")
        elif reason == "auth_retry_after" and auth_retry_after and auth_retry_after > now:
            detail_parts.append(f"登录重试 {_format_pool_wait(auth_retry_after - now)}")
        elif reason == "weekly_exhausted":
            detail_parts.append("周额度耗尽")
        elif reason == "ready_now":
            detail_parts.append("可复用")
    else:
        if primary_remaining_pct is not None:
            detail_parts.append(f"5h剩余 {primary_remaining_pct}%")
        if weekly_remaining_pct is not None:
            detail_parts.append(f"周剩余 {weekly_remaining_pct}%")
        if reason == "primary_low":
            detail_parts.append(f"5h低于阈值 {quota_threshold}%")
    if reason == "ready_low_weekly":
        detail_parts.append("周额度偏低,有其它号时会靠后")

    return {
        "pool_sort_bucket": bucket,
        "next_usable_at": next_usable_at,
        "next_usable_reason": reason,
        "primary_pct": primary_pct,
        "weekly_pct": weekly_pct,
        "primary_remaining_pct": primary_remaining_pct,
        "weekly_remaining_pct": weekly_remaining_pct,
        "primary_resets_at": primary_resets_at,
        "weekly_resets_at": weekly_resets_at,
        "quota_display_scope": quota_scope,
        "standby_since": standby_since,
        "pool_rank_detail": " · ".join(detail_parts),
    }


def _account_pool_sort_key(acc: dict) -> tuple:
    schedule = acc.get("pool_schedule") or {}
    bucket = schedule.get("pool_sort_bucket")
    if not isinstance(bucket, (int, float)):
        bucket = 60
    next_usable_at = schedule.get("next_usable_at")
    if not isinstance(next_usable_at, (int, float)):
        next_usable_at = 0 if bucket in (0, 10, 20, 30) else 9999999999
    primary_pct = schedule.get("primary_pct")
    if not isinstance(primary_pct, (int, float)):
        primary_pct = 999
    primary_remaining = schedule.get("primary_remaining_pct")
    primary_missing = not isinstance(primary_remaining, (int, float))
    if primary_missing:
        primary_remaining = 0
    weekly_remaining = schedule.get("weekly_remaining_pct")
    weekly_missing = not isinstance(weekly_remaining, (int, float))
    if weekly_missing:
        weekly_remaining = 0
    standby_since = schedule.get("standby_since")
    if not isinstance(standby_since, (int, float)) or standby_since <= 0:
        standby_since = acc.get("created_at") or 0
    if not isinstance(standby_since, (int, float)):
        standby_since = 0
    created_at = acc.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = 0
    return (
        int(bucket),
        float(next_usable_at),
        float(standby_since) if int(bucket) in (20, 40, 45, 55) else 0.0,
        weekly_missing,
        -int(weekly_remaining),
        primary_missing,
        -int(primary_remaining),
        int(primary_pct),
        float(created_at),
        str(acc.get("email") or ""),
    )


def _sanitize_account(acc: dict, quota_snapshot: dict | None = None) -> dict:
    """脱敏账号信息（去掉 password 等敏感字段）"""
    sanitized = {
        k: v
        for k, v in acc.items()
        if k not in ("password", "cloudmail_account_id") and not str(k).startswith("_")
    }
    sanitized["is_main_account"] = _is_main_account_email(acc.get("email"))
    sanitized["raw_status"] = acc.get("status", "")
    sanitized["status"] = _display_account_status(acc, quota_snapshot)
    schedule = _account_pool_schedule(acc, quota_snapshot)
    sanitized["pool_schedule"] = schedule
    sanitized["pool_sort_bucket"] = schedule["pool_sort_bucket"]
    sanitized["next_usable_at"] = schedule["next_usable_at"]
    sanitized["next_usable_reason"] = schedule["next_usable_reason"]
    sanitized["primary_remaining_pct"] = schedule["primary_remaining_pct"]
    sanitized["weekly_remaining_pct"] = schedule["weekly_remaining_pct"]
    sanitized["quota_display_scope"] = schedule["quota_display_scope"]
    sanitized["standby_since"] = schedule["standby_since"]
    sanitized["pool_rank_detail"] = schedule["pool_rank_detail"]
    return sanitized


def _admin_status():
    from autoteam.admin_state import get_admin_state_summary

    status = get_admin_state_summary()
    status["login_step"] = _admin_login_step
    status["login_in_progress"] = _admin_login_api is not None
    if _admin_login_api and _admin_login_step == "workspace_required":
        status["workspace_options"] = getattr(_admin_login_api, "workspace_options_cache", []) or []
    else:
        status["workspace_options"] = []
    return status


def _main_codex_status():
    return {
        "in_progress": _main_codex_flow is not None,
        "step": _main_codex_step,
        "action": _main_codex_action,
    }


def _manual_account_status():
    status = {
        "in_progress": False,
        "status": "idle",
        "state": "",
        "auth_url": "",
        "started_at": None,
        "message": "",
        "error": "",
        "account": None,
        "callback_received": False,
        "callback_source": "",
        "auto_callback_available": False,
        "auto_callback_error": "",
    }
    if _manual_account_flow:
        status.update(_manual_account_flow.status())
    return status


def _finish_admin_login(completed: dict):
    global _admin_login_api, _admin_login_step
    api = _admin_login_api
    info = None
    try:
        info = _pw_executor.run(api.complete_admin_login)
    finally:
        if api:
            try:
                _pw_executor.run(api.stop)
            except Exception:
                pass
        _admin_login_api = None
        _admin_login_step = None
        if info and info.get("session_token") and info.get("account_id"):
            try:
                from autoteam.codex_auth import refresh_main_auth_file

                main_auth = _pw_executor.run(refresh_main_auth_file)
                if main_auth:
                    info["main_auth"] = main_auth
                    logger.info("[API] 管理员登录后已刷新主号认证文件: %s", main_auth.get("auth_file"))
            except Exception as exc:
                info["main_auth_error"] = str(exc)
                logger.warning("[API] 管理员登录完成，但刷新主号认证文件失败: %s", exc)
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"status": "completed", "admin": _admin_status(), "codex": _main_codex_status(), "info": info}


def _set_pending_admin_login(api, step):
    global _admin_login_api, _admin_login_step
    _admin_login_api = api
    _admin_login_step = step
    return {"status": step, "admin": _admin_status()}


def _finish_main_codex_flow():
    global _main_codex_flow, _main_codex_step, _main_codex_action
    flow = _main_codex_flow
    action = _main_codex_action or "sync"
    try:
        info = _pw_executor.run(flow.complete)
    finally:
        if flow:
            try:
                _pw_executor.run(flow.stop)
            except Exception:
                pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        if _playwright_lock.locked():
            _playwright_lock.release()

    if action == "login":
        message = "主号 Codex 已登录"
    else:
        from autoteam.sync_targets import describe_sync_targets, get_enabled_sync_targets

        enabled_targets = get_enabled_sync_targets()
        target_label = describe_sync_targets(enabled_targets)
        message = (
            f"主号 Codex 已同步到 {target_label}"
            if enabled_targets
            else "主号 Codex 已保存，本轮未启用远端同步目标"
        )
    return {
        "status": "completed",
        "message": message,
        "codex": _main_codex_status(),
        "info": info,
    }


def _finish_main_codex_sync():
    return _finish_main_codex_flow()


def _set_pending_main_codex_flow(flow, step, action):
    global _main_codex_flow, _main_codex_step, _main_codex_action
    _main_codex_flow = flow
    _main_codex_step = step
    _main_codex_action = action
    return {"status": step, "codex": _main_codex_status()}


def _set_pending_main_codex_sync(flow, step):
    return _set_pending_main_codex_flow(flow, step, "sync")


def _finish_manual_account_flow(result: dict):
    return {**result, "manual_account": _manual_account_status()}


def _set_pending_manual_account_flow(flow, result):
    global _manual_account_flow
    _manual_account_flow = flow
    return {**result, "manual_account": _manual_account_status()}


# ---------------------------------------------------------------------------
# 同步端点
# ---------------------------------------------------------------------------


@app.get("/api/admin/status")
def get_admin_status():
    """获取管理员登录状态。"""
    return _admin_status()


@app.post("/api/admin/fix-account-id")
def post_admin_fix_account_id():
    """
    基于当前已保存的 session_token,重新从 /backend-api/accounts 拉取真实 workspace 列表,
    覆盖写入 admin_state.account_id / workspace_name。适用于: 之前导入的 session 把
    account_id 误写成了 OAI 缓存的陈旧 UUID,导致所有 admin 接口 401。

    不需要用户手动退出重登 —— 只是重算 account_id。
    """
    from autoteam.admin_state import (
        get_admin_email,
        get_admin_session_token,
        get_chatgpt_account_id,
        update_admin_state,
    )
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    if not get_admin_session_token():
        raise HTTPException(status_code=400, detail="尚未保存 session_token,请先导入")

    def _do():
        api = ChatGPTTeamAPI()
        try:
            api._launch_browser()
            logger.info("[修复 account_id] 打开 chatgpt.com 注入 session...")
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            api._wait_for_cloudflare()
            api._inject_session(get_admin_session_token())
            # 注入 session 后可能触发一次新的 CF 挑战,再等一次避免首个 _api_fetch 碰上 challenge 页
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            api._wait_for_cloudflare()
            api._fetch_access_token()

            team, personal = api._list_real_workspaces()
            admin_roles = ("account-owner", "admin", "org-admin", "workspace-owner")
            chosen = None
            for acc in team:
                if str(acc.get("current_user_role") or "").lower() in admin_roles:
                    chosen = acc
                    break
            if not chosen and team:
                chosen = team[0]
            if not chosen:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"当前 session ({get_admin_email()}) 没有 Team workspace,"
                        f" 只有: {[a.get('structure') for a in personal]}。"
                        f"请确认该账号已被邀请加入 Team。"
                    ),
                )

            new_account_id = str(chosen.get("id") or "")
            new_workspace_name = str(chosen.get("name") or "")

            # 用新 account_id 验证接口是否真能访问
            api.account_id = new_account_id
            verify = api._api_fetch("GET", f"/backend-api/accounts/{new_account_id}/settings")
            if verify.get("status") != 200:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"新 account_id={new_account_id} 仍不可访问 "
                        f"status={verify.get('status')},session_token 可能已过期,请重新导入。"
                    ),
                )

            old_account_id = get_chatgpt_account_id()
            update_admin_state(account_id=new_account_id, workspace_name=new_workspace_name)
            logger.info(
                "[修复 account_id] 已更新: %s -> %s (workspace=%s)",
                old_account_id,
                new_account_id,
                new_workspace_name,
            )
            return {
                "message": "已修复",
                "old_account_id": old_account_id,
                "new_account_id": new_account_id,
                "workspace_name": new_workspace_name,
                "role": chosen.get("current_user_role"),
            }
        finally:
            try:
                api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/admin/diagnose")
def get_admin_diagnose():
    """
    用当前管理员 session_token 探测 Team admin 接口,辅助诊断 401/403。
    返回四个关键接口的状态码 + body 前 200 字:
    - /api/auth/session  → access_token 是否拿到
    - /backend-api/me    → 当前登录用户是谁
    - /backend-api/accounts/<id>/settings  → workspace 是否可读
    - /backend-api/accounts/<id>/users     → admin 权限是否生效(真正的 fill-personal 卡点)
    """
    from autoteam.admin_state import get_admin_email, get_chatgpt_account_id
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    def _do():
        # 只读诊断:必须走手动 launch+inject,不调 api.start()——start() 里的
        # _auto_detect_workspace 会写 admin_state,把诊断弄成副作用操作
        from autoteam.admin_state import get_admin_session_token

        api = ChatGPTTeamAPI()
        try:
            setup_steps = []

            def _record_step(name: str, ok: bool, detail=None):
                setup_steps.append({"step": name, "ok": ok, "detail": _body_preview(detail, 300)})

            def _safe_wait_for_cloudflare(label: str) -> bool:
                try:
                    api._wait_for_cloudflare()
                    _record_step(label, True)
                    return True
                except Exception as exc:
                    logger.warning("[诊断] wait_for_cloudflare failed label=%s: %s", label, exc)
                    _record_step(label, False, exc)
                    return False

            def _safe_page_fetch(path: str) -> dict:
                script = (
                    "async (path) => { const r = await fetch(path); "
                    "return { status: r.status, body: (await r.text()).slice(0, 400) }; }"
                )
                last_error = None
                for attempt in range(2):
                    try:
                        api.page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    try:
                        return api.page.evaluate(script, path)
                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "[诊断] page fetch failed path=%s attempt=%d: %s",
                            path,
                            attempt + 1,
                            exc,
                        )
                        try:
                            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                            time.sleep(1)
                        except Exception as nav_exc:
                            last_error = nav_exc
                            break
                return {"status": None, "error": _body_preview(last_error, 400)}

            def _safe_api_fetch(name: str, path: str) -> dict:
                try:
                    r = api._api_fetch("GET", path)
                    return {"status": r.get("status"), "body": _body_preview(r.get("body") or "", 500)}
                except Exception as exc:
                    logger.warning("[诊断] api fetch failed name=%s path=%s: %s", name, path, exc)
                    return {"status": None, "error": _body_preview(exc, 500)}

            api._launch_browser()
            _record_step("launch_browser", True)
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            _record_step("goto_chatgpt_initial", True)
            time.sleep(3)
            _safe_wait_for_cloudflare("wait_cloudflare_initial")
            session_token = get_admin_session_token()
            if session_token:
                api.account_id = get_chatgpt_account_id() or ""  # 让 _inject_session 把 _account cookie 带上
                api._inject_session(session_token)
                _record_step("inject_session", True)
                api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                _record_step("goto_chatgpt_after_inject", True)
                time.sleep(2)
                _safe_wait_for_cloudflare("wait_cloudflare_after_inject")
            try:
                api._fetch_access_token()
                _record_step("fetch_access_token", bool(api.access_token))
            except Exception as exc:
                logger.warning("[诊断] fetch_access_token failed: %s", exc)
                _record_step("fetch_access_token", False, exc)
            account_id = api.account_id or get_chatgpt_account_id() or ""
            probes = {}

            probes["auth_session"] = _safe_page_fetch("/api/auth/session")

            for name, path in [
                ("backend_me", "/backend-api/me"),
                ("backend_accounts", "/backend-api/accounts"),
                ("workspace_settings", f"/backend-api/accounts/{account_id}/settings"),
                ("workspace_users", f"/backend-api/accounts/{account_id}/users"),
            ]:
                probes[name] = _safe_api_fetch(name, path)

            # Round 8 — SPEC-2 v1.5 §6.2:diagnose 内嵌 master_subscription_state(read-only,5min cache)
            try:
                from autoteam.master_health import is_master_subscription_healthy
                healthy, reason, evidence = is_master_subscription_healthy(api)
                master_state = {
                    "healthy": healthy,
                    "reason": reason,
                    "evidence": evidence,
                }
            except Exception as exc:
                master_state = {
                    "healthy": None,
                    "reason": "probe_exception",
                    "evidence": {"detail": str(exc)[:200]},
                }

            return {
                "admin_email": get_admin_email(),
                "account_id": account_id,
                "access_token_present": bool(api.access_token),
                "access_token_prefix": (api.access_token or "")[:30],
                "setup_steps": setup_steps,
                "probes": probes,
                "master_subscription_state": master_state,
            }
        finally:
            try:
                api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/admin/master-health")
def get_admin_master_health(request: Request):
    """Round 8 — 母号订阅健康度独立端点。

    Round 9 SPEC v1.1 §13 endpoint 守恒:任何场景**永不返回 5xx**,
    全部异常映射到 200 OK + business field(reason='auth_invalid' / 'network_error')。

    查询参数:
        force_refresh=1 → 跳过 5min cache 直接重测
    返回 is_master_subscription_healthy() 的完整 (healthy, reason, evidence) 三元组。
    UI 用于"立即重测"按钮。
    """
    force_refresh = str(request.query_params.get("force_refresh", "")).strip().lower() in (
        "1", "true", "yes",
    )

    def _do():
        from autoteam.chatgpt_api import ChatGPTTeamAPI
        from autoteam.master_health import is_master_subscription_healthy

        api = None
        try:
            try:
                api = ChatGPTTeamAPI()
                api.start()
            except Exception as exc:
                # spec §13.2 — start() 失败映射 auth_invalid 200 OK
                logger.warning("[master-health] ChatGPTTeamAPI start failed: %s", exc)
                return {
                    "healthy": False,
                    "reason": "auth_invalid",
                    "evidence": {
                        "http_status": None,
                        "detail": f"chatgpt_api_start_failed:{type(exc).__name__}",
                        "cache_hit": False,
                        "probed_at": time.time(),
                    },
                    "force_refresh": force_refresh,
                }
            try:
                healthy, reason, evidence = is_master_subscription_healthy(
                    api, force_refresh=force_refresh,
                )
                # Round 12 wire-up (C2) — feed every "force-refresh re-probe"
                # into WorkspacePool so connect 3 unhealthy → auto failover.
                # apply_pool_health_signal swallows internally (M-I1).
                try:
                    from autoteam.master_health import apply_pool_health_signal
                    apply_pool_health_signal(healthy, reason, evidence)
                except Exception as pool_exc:  # pragma: no cover — defensive
                    logger.warning(
                        "[master-health] pool signal failed: %s", pool_exc,
                    )
                return {
                    "healthy": healthy,
                    "reason": reason,
                    "evidence": evidence,
                    "force_refresh": force_refresh,
                }
            except Exception as exc:
                # spec §13.2 — probe 双保险,is_master_subscription_healthy 自身永不抛,
                # 这里兜底把任何意外异常映射 network_error 200 OK
                logger.warning(
                    "[master-health] probe unexpected exception: %s", exc,
                )
                return {
                    "healthy": False,
                    "reason": "network_error",
                    "evidence": {
                        "http_status": None,
                        "detail": f"probe_unexpected_exception:{type(exc).__name__}",
                        "cache_hit": False,
                        "probed_at": time.time(),
                    },
                    "force_refresh": force_refresh,
                }
        finally:
            try:
                if api is not None:
                    api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        try:
            return _pw_executor.run(_do)
        except Exception as exc:
            # spec §13.3 — _pw_executor 调度异常兜底,仍按 endpoint 守恒返回 200
            logger.error("[master-health] _pw_executor.run failed: %s", exc)
            return {
                "healthy": False,
                "reason": "network_error",
                "evidence": {
                    "http_status": None,
                    "detail": f"executor_failed:{type(exc).__name__}",
                    "cache_hit": False,
                    "probed_at": time.time(),
                },
                "force_refresh": force_refresh,
            }
    finally:
        _playwright_lock.release()


@app.post("/api/admin/reconcile")
def post_admin_reconcile(request: Request):
    """对账 Team 实际成员 vs 本地状态,修复残废 / 错位 / 耗尽未抛弃 / ghost。

    与 /api/admin/diagnose 使用同款鉴权模式(auth_middleware 已处理 API_KEY)。
    查询参数:
        dry_run=1 → 只诊断,不 KICK、不改 accounts.json
    返回 _reconcile_team_members 的完整结果 dict。
    """
    from autoteam.manager import cmd_reconcile

    dry_run = str(request.query_params.get("dry_run", "")).strip().lower() in ("1", "true", "yes")

    def _do():
        return cmd_reconcile(dry_run=dry_run)

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/main-codex/status")
def get_main_codex_status():
    """获取主号 Codex 同步状态。"""
    return _main_codex_status()


@app.get("/api/manual-account/status")
def get_manual_account_status():
    """获取手动添加账号状态。"""
    return _manual_account_status()


@app.post("/api/admin/login/start")
def post_admin_login_start(params: AdminEmailParams):
    """开始管理员登录流程。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再进行管理员登录")
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 开始管理员登录: %s", params.email.strip())

        def _do_start(email):
            api = ChatGPTTeamAPI()
            try:
                result = api.begin_admin_login(email)
                return api, result
            except Exception:
                api.stop()
                raise

        api, result = _pw_executor.run(_do_start, params.email.strip())
        step = result["step"]
        logger.info("[API] 管理员登录 start 返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            _admin_login_api = api
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            return _set_pending_admin_login(api, step)
        _pw_executor.run(api.stop)
        _playwright_lock.release()
        raise HTTPException(status_code=400, detail=result.get("detail") or "无法识别管理员登录步骤")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员登录 start 失败")
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/session")
def post_admin_login_session(params: AdminSessionParams):
    """手动导入管理员 session_token。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        post_admin_login_cancel()

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=_current_busy_detail("有任务正在执行，请等待完成后再导入管理员 session_token"),
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 导入管理员 session_token: %s", params.email.strip())

        def _do_import(email, session_token):
            api = ChatGPTTeamAPI()
            try:
                return api.import_admin_session(email, session_token)
            finally:
                api.stop()

        info = _pw_executor.run(_do_import, params.email.strip(), params.session_token.strip())
        if info.get("session_token") and info.get("account_id"):
            try:
                from autoteam.codex_auth import refresh_main_auth_file

                main_auth = _pw_executor.run(refresh_main_auth_file)
                if main_auth:
                    info["main_auth"] = main_auth
                    logger.info("[API] session_token 导入后已刷新主号认证文件: %s", main_auth.get("auth_file"))
            except Exception as exc:
                info["main_auth_error"] = str(exc)
                logger.warning("[API] session_token 导入完成，但刷新主号认证文件失败: %s", exc)
        _admin_login_api = None
        _admin_login_step = None
        return {"status": "completed", "admin": _admin_status(), "info": info}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 导入管理员 session_token 失败")
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if _playwright_lock.locked():
            _playwright_lock.release()


@app.post("/api/admin/login/password")
def post_admin_login_password(params: AdminPasswordParams):
    """提交管理员密码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "password_required":
        raise HTTPException(status_code=409, detail="当前没有等待密码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员密码 | current_step=%s", _admin_login_step)
        result = _pw_executor.run(_admin_login_api.submit_admin_password, params.password)
        step = result["step"]
        logger.info("[API] 管理员密码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员密码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员密码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/code")
def post_admin_login_code(params: AdminCodeParams):
    """提交管理员验证码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "code_required":
        raise HTTPException(status_code=409, detail="当前没有等待验证码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员验证码 | current_step=%s code_len=%d", _admin_login_step, len(params.code.strip()))
        result = _pw_executor.run(_admin_login_api.submit_admin_code, params.code.strip())
        step = result["step"]
        logger.info("[API] 管理员验证码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员验证码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员验证码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/workspace")
def post_admin_login_workspace(params: AdminWorkspaceParams):
    """提交管理员 workspace 选择。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "workspace_required":
        raise HTTPException(status_code=409, detail="当前没有等待组织选择的管理员登录流程")

    try:
        logger.info("[API] 提交管理员 workspace 选择 | option_id=%s", params.option_id)
        result = _pw_executor.run(_admin_login_api.select_workspace_option, params.option_id)
        step = result["step"]
        logger.info("[API] 管理员 workspace 选择返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员组织选择失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员 workspace 选择失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/cancel")
def post_admin_login_cancel():
    """取消管理员登录流程。"""
    global _admin_login_api, _admin_login_step
    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"message": "管理员登录已取消", "admin": _admin_status()}


@app.post("/api/admin/logout")
def post_admin_logout():
    """清除已保存的管理员登录态。"""
    from autoteam.admin_state import clear_admin_state

    if _admin_login_api:
        post_admin_login_cancel()
    clear_admin_state()
    return {"message": "管理员登录态已清除", "admin": _admin_status()}


def _clear_active_main_codex_flow():
    global _main_codex_flow, _main_codex_step, _main_codex_action

    if _main_codex_flow:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        if _playwright_lock.locked():
            _playwright_lock.release()


def _start_main_codex_flow(action="sync"):
    from autoteam.codex_auth import MainCodexLoginFlow, MainCodexSyncFlow

    flow_cls = MainCodexSyncFlow if action == "sync" else MainCodexLoginFlow

    def _do_start():
        flow = flow_cls()
        try:
            result = flow.start()
        except Exception:
            try:
                flow.stop()
            except Exception:
                pass
            raise
        return flow, result

    flow, result = _pw_executor.run(_do_start)
    step = result["step"]
    if step == "completed":
        _set_pending_main_codex_flow(flow, step, action)
        return step, _finish_main_codex_flow()
    if step in ("password_required", "code_required"):
        return step, _set_pending_main_codex_flow(flow, step, action)

    _pw_executor.run(flow.stop)
    raise RuntimeError(result.get("detail") or "无法识别主号 Codex 登录步骤")


@app.post("/api/main-codex/start")
def post_main_codex_start():
    """开始主号 Codex 登录并同步到已启用远端。"""
    _clear_active_main_codex_flow()
    _require_sync_target_configs("同步主号 Codex")

    from autoteam.codex_auth import get_saved_main_auth_file
    from autoteam.sync_targets import (
        describe_sync_targets,
        get_enabled_sync_targets,
        sync_main_codex_to_configured_targets,
    )

    saved_auth_file = get_saved_main_auth_file()
    if saved_auth_file:
        sync_main_codex_to_configured_targets(saved_auth_file)
        enabled_targets = get_enabled_sync_targets()
        target_label = describe_sync_targets(enabled_targets)
        return {
            "status": "completed",
            "message": f"主号 Codex 已同步到 {target_label}",
            "codex": _main_codex_status(),
            "info": {"auth_file": saved_auth_file},
        }

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再同步主号 Codex")
    )

    try:
        _step, result = _start_main_codex_flow(action="sync")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/login")
def post_main_codex_login():
    """开始主号 Codex 登录，仅保存本地认证文件。"""
    _clear_active_main_codex_flow()

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再登录主号 Codex")
        )

    try:
        _step, result = _start_main_codex_flow(action="login")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/password")
def post_main_codex_password(params: AdminPasswordParams):
    """提交主号 Codex 登录密码。"""
    global _main_codex_flow, _main_codex_step, _main_codex_action
    if not _main_codex_flow or _main_codex_step != "password_required":
        raise HTTPException(status_code=409, detail="当前没有等待密码的主号 Codex 登录流程")

    try:
        result = _pw_executor.run(_main_codex_flow.submit_password, params.password)
        step = result["step"]
        if step == "completed":
            return _finish_main_codex_sync()
        if step in ("password_required", "code_required"):
            _main_codex_step = step
            return {"status": step, "codex": _main_codex_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "主号 Codex 密码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/code")
def post_main_codex_code(params: AdminCodeParams):
    """提交主号 Codex 登录验证码。"""
    global _main_codex_flow, _main_codex_step, _main_codex_action
    if not _main_codex_flow or _main_codex_step != "code_required":
        raise HTTPException(status_code=409, detail="当前没有等待验证码的主号 Codex 登录流程")

    try:
        result = _pw_executor.run(_main_codex_flow.submit_code, params.code.strip())
        step = result["step"]
        if step == "completed":
            return _finish_main_codex_sync()
        if step in ("password_required", "code_required"):
            _main_codex_step = step
            return {"status": step, "codex": _main_codex_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "主号 Codex 验证码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/cancel")
def post_main_codex_cancel():
    """取消主号 Codex 登录流程。"""
    global _main_codex_flow, _main_codex_step, _main_codex_action
    if _main_codex_flow:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        _main_codex_action = None
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"message": "主号 Codex 登录已取消", "codex": _main_codex_status()}


def _delete_main_codex_from_enabled_targets():
    from autoteam.sync_targets import (
        delete_main_codex_from_configured_targets,
        describe_sync_targets,
        get_enabled_sync_targets,
    )

    _require_sync_target_configs("删除主号 Codex 远端文件")

    enabled_targets = get_enabled_sync_targets()
    results = delete_main_codex_from_configured_targets()
    deleted: list[str] = []
    total_count = 0

    for target in enabled_targets:
        target_result = results.get(target) or {}
        target_deleted = [str(item) for item in (target_result.get("deleted") or [])]
        deleted.extend(target_deleted)
        try:
            total_count += int(target_result.get("count", len(target_deleted)) or 0)
        except (TypeError, ValueError):
            total_count += len(target_deleted)

    return {
        "message": f"已从 {describe_sync_targets(enabled_targets)} 删除 {total_count} 个主号认证文件",
        "deleted": deleted,
        "results": results,
    }


@app.post("/api/main-codex/delete-remote-files")
def post_main_codex_delete_remote_files():
    """删除已启用远端中已上传的主号 Codex 认证文件。"""
    return _delete_main_codex_from_enabled_targets()


@app.post("/api/main-codex/delete-cpa")
def post_main_codex_delete_cpa():
    """兼容旧接口：删除已启用远端中的主号 Codex 认证文件。"""
    return _delete_main_codex_from_enabled_targets()


@app.post("/api/manual-account/start")
def post_manual_account_start():
    """开始手动添加账号流程，返回 OAuth 链接。"""
    global _manual_account_flow

    if _manual_account_flow:
        try:
            _manual_account_flow.stop()
        except Exception:
            pass
        _manual_account_flow = None

    try:
        from autoteam.manual_account import ManualAccountFlow

        flow = ManualAccountFlow()
        result = flow.start()
        return _set_pending_manual_account_flow(flow, result)
    except HTTPException:
        raise
    except Exception as exc:
        if _manual_account_flow:
            try:
                _manual_account_flow.stop()
            except Exception:
                pass
            _manual_account_flow = None
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/manual-account/callback")
def post_manual_account_callback(params: ManualAccountCallbackParams):
    """提交 OAuth 回调 URL，完成手动添加账号。"""
    global _manual_account_flow
    if not _manual_account_flow:
        raise HTTPException(status_code=409, detail="当前没有等待回调的手动添加账号流程")

    try:
        result = _manual_account_flow.submit_callback(params.redirect_url)
        return _finish_manual_account_flow(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/manual-account/cancel")
def post_manual_account_cancel():
    """取消手动添加账号流程。"""
    global _manual_account_flow
    if _manual_account_flow:
        try:
            _manual_account_flow.stop()
        except Exception:
            pass
        _manual_account_flow = None
    return {"message": "手动添加账号流程已取消", "manual_account": _manual_account_status()}


@app.get("/api/accounts")
def get_accounts():
    """获取所有账号列表"""
    from autoteam.accounts import load_accounts

    accounts = load_accounts()
    sanitized = [_sanitize_account(a) for a in accounts]
    sanitized.sort(key=_account_pool_sort_key)
    return sanitized


@app.get("/api/accounts/{email}/codex-auth")
def get_codex_auth(email: str):
    """导出账号的 Codex CLI 格式认证文件（~/.codex/auth.json）"""
    from autoteam.accounts import find_account, load_accounts
    from autoteam.codex_auth import get_saved_main_auth_file

    email = email.strip().lower()
    auth_file = ""

    if _is_main_account_email(email):
        auth_file = get_saved_main_auth_file()
        if not auth_file or not Path(auth_file).exists():
            raise HTTPException(status_code=404, detail="主号没有可导出的认证文件")
    else:
        acc = find_account(load_accounts(), email)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        auth_file = acc.get("auth_file") or ""
        if not auth_file or not Path(auth_file).exists():
            raise HTTPException(status_code=404, detail="该账号没有认证文件")

    auth_data = json.loads(Path(auth_file).read_text())

    # 转换为 Codex CLI 的 auth.json 格式
    codex_auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": auth_data.get("id_token", ""),
            "access_token": auth_data.get("access_token", ""),
            "refresh_token": auth_data.get("refresh_token", ""),
            "account_id": auth_data.get("account_id", ""),
        },
        "last_refresh": auth_data.get("last_refresh", ""),
    }

    return {
        "email": email,
        "codex_auth": codex_auth,
        "hint": "将内容保存到 ~/.codex/auth.json（Linux/macOS）或 %APPDATA%\\codex\\auth.json（Windows）",
    }


@app.get("/api/accounts/active")
def get_active():
    """获取活跃账号"""
    from autoteam.accounts import get_active_accounts

    sanitized = [_sanitize_account(a) for a in get_active_accounts()]
    sanitized.sort(key=_account_pool_sort_key)
    return sanitized


@app.get("/api/accounts/standby")
def get_standby():
    """获取待命账号"""
    from autoteam.accounts import get_standby_accounts

    accounts = get_standby_accounts()
    sanitized = [_sanitize_account(a) for a in accounts]
    sanitized.sort(key=_account_pool_sort_key)
    return sanitized


def _toggle_account_disabled(email: str, disabled: bool):
    from autoteam.accounts import find_account, load_accounts, update_account

    email = _normalized_email(email)
    if not email:
        raise HTTPException(status_code=400, detail="请提供有效邮箱")
    if _is_main_account_email(email):
        raise HTTPException(status_code=400, detail="主号不允许禁用或启用")

    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    update_account(email, disabled=bool(disabled))
    refreshed = find_account(load_accounts(), email)
    action = "禁用" if disabled else "启用"
    return {
        "message": f"已{action} {email}",
        "email": email,
        "disabled": bool(disabled),
        "account": _sanitize_account(refreshed or {**acc, "disabled": bool(disabled)}),
    }


def _toggle_accounts_disabled(emails: list[str], disabled: bool):
    from autoteam.accounts import load_accounts, save_accounts

    normalized_emails = []
    seen = set()
    for value in emails or []:
        email = _normalized_email(value)
        if not email or email in seen:
            continue
        seen.add(email)
        normalized_emails.append(email)

    if not normalized_emails:
        raise HTTPException(status_code=400, detail="请至少提供一个有效邮箱")

    accounts = load_accounts()
    by_email = {_normalized_email(acc.get("email")): acc for acc in accounts if acc.get("email")}

    updated = []
    unchanged = []
    missing = []
    skipped_main = []

    for email in normalized_emails:
        if _is_main_account_email(email):
            skipped_main.append(email)
            continue
        acc = by_email.get(email)
        if not acc:
            missing.append(email)
            continue
        if bool(acc.get("disabled", False)) == bool(disabled):
            unchanged.append(email)
            continue
        acc["disabled"] = bool(disabled)
        updated.append(email)

    if updated:
        save_accounts(accounts)
        accounts = load_accounts()
        by_email = {_normalized_email(acc.get("email")): acc for acc in accounts if acc.get("email")}

    action = "禁用" if disabled else "启用"
    parts = [f"已{action} {len(updated)} 个账号"]
    if unchanged:
        parts.append(f"{len(unchanged)} 个已是目标状态")
    if skipped_main:
        parts.append(f"跳过主号 {len(skipped_main)} 个")
    if missing:
        parts.append(f"未找到 {len(missing)} 个")

    return {
        "message": "，".join(parts),
        "disabled": bool(disabled),
        "updated_count": len(updated),
        "updated_emails": updated,
        "unchanged_emails": unchanged,
        "skipped_main_accounts": skipped_main,
        "missing_emails": missing,
        "accounts": [_sanitize_account(by_email[email]) for email in updated if email in by_email],
    }


@app.post("/api/accounts/bulk/disable")
def post_bulk_disable_accounts(params: AccountDisableParams):
    """批量禁用账号：保留本地记录，但自动巡检、轮转和 CPA 同步会跳过。"""
    return _toggle_accounts_disabled(params.emails, True)


@app.post("/api/accounts/bulk/enable")
def post_bulk_enable_accounts(params: AccountDisableParams):
    """批量启用账号：恢复参与自动巡检、轮转和 CPA 同步。"""
    return _toggle_accounts_disabled(params.emails, False)


@app.post("/api/accounts/{email}/disable")
def post_disable_account(email: str):
    """禁用账号：保留本地记录，但自动巡检、轮转和 CPA 同步会跳过。"""
    return _toggle_account_disabled(email, True)


@app.post("/api/accounts/{email}/enable")
def post_enable_account(email: str):
    """启用账号：恢复参与自动巡检、轮转和 CPA 同步。"""
    return _toggle_account_disabled(email, False)


@app.delete("/api/accounts/{email}")
def delete_account(email: str):
    """删除本地管理账号及其关联资源。"""
    if not _playwright_lock.acquire(blocking=False):
        running = _tasks.get(_current_task_id, {})
        raise HTTPException(
            status_code=409,
            detail={
                "message": "有任务正在执行，请等待完成后再删除账号",
                "running_task": {
                    "task_id": _current_task_id,
                    "command": running.get("command", "unknown"),
                    "started_at": running.get("started_at"),
                },
            },
        )

    try:
        from autoteam.account_ops import delete_managed_account
        from autoteam.accounts import load_accounts

        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许删除")

        accounts = load_accounts()
        if not any(a["email"].lower() == email.lower() for a in accounts):
            raise HTTPException(status_code=404, detail="账号不存在")

        cleanup = _pw_executor.run(delete_managed_account, email)
        return {
            "message": "账号删除完成",
            "deleted_email": email,
            "cleanup": cleanup,
        }
    finally:
        _playwright_lock.release()


@app.post("/api/accounts/delete-batch")
def delete_accounts_batch(params: DeleteBatchParams):
    """
    批量删除本地管理账号。整批共享一个 chatgpt_api + mail_client,
    Team 成员/邀请状态只拉一次,CPA 在整批结束后同步一次,避免重复开销。
    """
    from autoteam.account_ops import delete_managed_account, fetch_team_state
    from autoteam.accounts import load_accounts
    from autoteam.chatgpt_api import ChatGPTTeamAPI
    from autoteam.cloudmail import CloudMailClient
    from autoteam.sync_targets import sync_to_configured_targets as sync_to_cpa

    raw_emails = [(e or "").strip() for e in (params.emails or [])]
    emails = [e for e in raw_emails if e]
    if not emails:
        raise HTTPException(status_code=400, detail="emails 不能为空")

    # 去重,保留首次出现顺序
    seen = set()
    dedup = []
    for e in emails:
        low = e.lower()
        if low in seen:
            continue
        seen.add(low)
        dedup.append(e)
    emails = dedup

    main_emails = [e for e in emails if _is_main_account_email(e)]
    if main_emails:
        raise HTTPException(status_code=400, detail=f"主号不允许删除: {main_emails}")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再批量删除"))

    def _run():
        from autoteam.accounts import STATUS_AUTH_INVALID, STATUS_PERSONAL

        accounts = load_accounts()
        existing = {(a.get("email") or "").lower(): a for a in accounts}

        # SPEC-2 §3.5.2 + Round 6 PRD-5 FR-P1.4:批量删除若整批都是 personal/auth_invalid,
        # 整批跳过 ChatGPTTeamAPI 启动 — 这两类不需要 remote_state(成员/邀请),纯本地清理。
        # 关键:bool(targets_in_pool) 守卫空 list,避免 all([]) == True 误判;
        # 同时如果传入 emails 全部不存在(existing 没匹配),也不应短路(走原 chatgpt_api 路径
        # 让循环里给每条返回"账号不存在")。
        targets_in_pool = [
            existing[e.lower()]
            for e in emails
            if e.lower() in existing
        ]
        all_local_only = bool(targets_in_pool) and all(
            (a.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))
            for a in targets_in_pool
        )

        chatgpt_api = None
        mail_client = None
        results = []
        try:
            remote_state = None
            if not all_local_only:
                # 至少一个号需要 Team 远端同步 — 启动共享 ChatGPTTeamAPI / mail_client
                chatgpt_api = ChatGPTTeamAPI()
                chatgpt_api.start()
                mail_client = CloudMailClient()
                mail_client.login()
                # 整批共享一次 Team 状态快照,避免每个删除都重查一次
                remote_state = fetch_team_state(chatgpt_api)
            else:
                # 全 personal/auth_invalid 路径:cloudmail 仍可能要清理(personal 有 cloudmail_account_id),
                # 但 mail_client 由 delete_managed_account 内部按需懒加载(own_mail_client 路径),
                # 这里不预启动 ChatGPTTeamAPI。
                logger.info(
                    "[批量删除] 整批 %d 个账号均为 personal/auth_invalid,跳过 ChatGPTTeamAPI 启动(FR-P1.4 短路)",
                    len(targets_in_pool),
                )

            for email in emails:
                if email.lower() not in existing:
                    results.append({"email": email, "ok": False, "error": "账号不存在"})
                    if not params.continue_on_error:
                        break
                    continue
                try:
                    cleanup = delete_managed_account(
                        email,
                        chatgpt_api=chatgpt_api,        # all_local_only=True 时为 None
                        mail_client=mail_client,        # 同上,delete_managed_account 内部懒加载
                        remote_state=remote_state,      # 同上
                        sync_cpa_after=False,            # 整批结束后统一同步
                    )
                    results.append({"email": email, "ok": True, "cleanup": cleanup})
                except Exception as exc:
                    logger.error("[批量删除] %s 失败: %s", email, exc)
                    results.append({"email": email, "ok": False, "error": str(exc)})
                    if not params.continue_on_error:
                        break
        finally:
            if chatgpt_api:
                try:
                    chatgpt_api.stop()
                except Exception as exc:
                    logger.debug("[批量删除] 关闭 chatgpt_api 异常: %s", exc)
            try:
                sync_to_cpa()
            except Exception as exc:
                logger.warning("[批量删除] 结尾 sync_to_cpa 失败: %s", exc)

        ok_count = sum(1 for r in results if r["ok"])
        return {
            "results": results,
            "summary": {
                "total": len(emails),
                "ok": ok_count,
                "failed": len(results) - ok_count,
                "skipped": len(emails) - len(results),
            },
        }

    try:
        # 每个账号平均 30s (拉取 team 状态 + kick + delete cloudmail),再给 120s 兜底余量。
        # 若仍超时会抛 TimeoutError,worker 线程会在后台继续跑完,但锁会释放 → 用户可以再提。
        timeout = max(300, 30 * len(emails) + 120)
        return _pw_executor.run_with_timeout(timeout, _run)
    finally:
        _playwright_lock.release()


# ---------------------------------------------------------------------------
# Round 11 — 子号实时探活 + 模型列表(用户 Q3 痛点)
# ---------------------------------------------------------------------------


class ProbeAccountParams(BaseModel):
    """`/api/accounts/{email}/probe` 请求体。"""

    force_codex_smoke: bool = True


@app.post("/api/accounts/{email}/probe")
def post_account_probe(email: str, params: ProbeAccountParams = ProbeAccountParams()):
    """Round 11 AC5 — 子号实时探活:并行 check_codex_quota + cheap_codex_smoke。

    用 access_token 直接探,不进队列,不抢 Playwright 锁(纯 HTTP 请求)。
    落 last_quota_check_at + last_quota,返回 status_before / status_after。
    """
    from autoteam.accounts import find_account, load_accounts, update_account
    from autoteam.codex_auth import cheap_codex_smoke, check_codex_quota

    email = email.strip().lower()
    if _is_main_account_email(email):
        raise HTTPException(status_code=400, detail="主号不属于子号探活对象,请用 /api/admin/master-health")

    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    auth_file = acc.get("auth_file")
    if not auth_file or not Path(auth_file).exists():
        raise HTTPException(status_code=422, detail={
            "error": "auth_file_missing",
            "message": "账号无可用 auth_file,无法探活",
        })

    try:
        auth_data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=422, detail={
            "error": "auth_file_unreadable",
            "message": f"auth_file 解析失败: {type(exc).__name__}",
        })

    access_token = auth_data.get("access_token") or (auth_data.get("tokens") or {}).get("access_token")
    if not access_token:
        raise HTTPException(status_code=422, detail={
            "error": "access_token_missing",
            "message": "auth_file 中缺 access_token,无法探活",
        })

    account_id = (
        auth_data.get("account_id")
        or (auth_data.get("tokens") or {}).get("account_id")
        or acc.get("workspace_account_id")
    )

    status_before = acc.get("status")

    # 并行不强求,顺序也行 — quota 后跑 smoke
    try:
        quota_status, quota_info = check_codex_quota(access_token, account_id=account_id)
    except Exception as exc:
        logger.warning("[probe %s] check_codex_quota 异常: %s", email, exc)
        quota_status, quota_info = "network_error", None

    try:
        smoke_result, smoke_detail = cheap_codex_smoke(
            access_token,
            account_id=account_id,
            force=bool(params.force_codex_smoke),
        )
    except Exception as exc:
        logger.warning("[probe %s] cheap_codex_smoke 异常: %s", email, exc)
        smoke_result, smoke_detail = "uncertain", f"exception:{type(exc).__name__}"

    # 落 last_quota_check_at;quota_info 是 quota 快照才落
    update_fields = {"last_quota_check_at": time.time()}
    if quota_status == "ok" and isinstance(quota_info, dict):
        update_fields["last_quota"] = quota_info
    try:
        update_account(email, **update_fields)
    except Exception as exc:
        logger.warning("[probe %s] update_account 失败: %s", email, exc)

    # 重新读最新状态
    accounts2 = load_accounts()
    acc2 = find_account(accounts2, email) or acc

    return {
        "email": email,
        "status_before": status_before,
        "status_after": acc2.get("status"),
        "quota_status": quota_status,
        "quota_info": quota_info if isinstance(quota_info, dict) else None,
        "smoke_result": smoke_result,
        "smoke_detail": smoke_detail,
        "last_quota_check_at": update_fields["last_quota_check_at"],
    }


@app.get("/api/accounts/{email}/models")
def get_account_models(email: str):
    """Round 11 AC7 — 用 access_token 调 /backend-api/models 拿可用模型列表。

    返回 {email, plan_type, models: [{slug, name, description, ...}, ...]}
    401/403 → 401 + auth_invalid;timeout → 503;其他 → 502。
    """
    import requests

    from autoteam.accounts import find_account, load_accounts

    email = email.strip().lower()
    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    auth_file = acc.get("auth_file")
    if not auth_file or not Path(auth_file).exists():
        raise HTTPException(status_code=422, detail={
            "error": "auth_file_missing",
            "message": "账号无可用 auth_file",
        })

    try:
        auth_data = json.loads(Path(auth_file).read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=422, detail={
            "error": "auth_file_unreadable",
            "message": f"auth_file 解析失败: {type(exc).__name__}",
        })

    access_token = auth_data.get("access_token") or (auth_data.get("tokens") or {}).get("access_token")
    if not access_token:
        raise HTTPException(status_code=422, detail={
            "error": "access_token_missing",
        })

    account_id = (
        auth_data.get("account_id")
        or (auth_data.get("tokens") or {}).get("account_id")
        or acc.get("workspace_account_id")
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    try:
        resp = requests.get(
            "https://chatgpt.com/backend-api/models",
            headers=headers,
            timeout=10.0,
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=503, detail={"error": "timeout"})
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail={
            "error": "network_error",
            "message": f"{type(exc).__name__}",
        })

    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail={
            "error": "auth_invalid",
            "http_status": resp.status_code,
        })
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail={
            "error": f"upstream_status_{resp.status_code}",
            "body_preview": (resp.text or "")[:200],
        })

    try:
        body = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail={
            "error": "json_parse_error",
            "message": f"{type(exc).__name__}",
        })

    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        models = []

    plan_type = None
    if isinstance(body, dict):
        plan_type = body.get("plan_type") or body.get("category") or None

    return {
        "email": email,
        "plan_type": plan_type,
        "models": models,
        "raw_keys": list(body.keys()) if isinstance(body, dict) else [],
    }


@app.post("/api/accounts/{email}/kick")
def post_kick_account(email: str):
    """将账号从 Team 中移出，状态变为 standby"""
    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再操作"))

    try:
        from autoteam.accounts import find_account, load_accounts, update_account
        from autoteam.manager import remove_from_team

        email = email.strip().lower()
        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许移出 Team")
        accounts = load_accounts()
        acc = find_account(accounts, email)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        if acc["status"] != "active":
            raise HTTPException(status_code=400, detail=f"账号状态为 {acc['status']}，不是 active")

        def _do_kick():
            from autoteam.chatgpt_api import ChatGPTTeamAPI

            chatgpt = ChatGPTTeamAPI()
            try:
                chatgpt.start()
                return remove_from_team(chatgpt, email)
            finally:
                chatgpt.stop()

        ok = _pw_executor.run(_do_kick)
        if ok:
            update_account(email, status="standby")
            return {"message": f"已将 {email} 移出 Team", "email": email, "status": "standby"}
        raise HTTPException(status_code=500, detail=f"移出 {email} 失败")
    finally:
        _playwright_lock.release()


class LoginAccountParams(BaseModel):
    email: str


@app.post("/api/accounts/login", status_code=202)
def post_account_login(params: LoginAccountParams):
    """触发单个账号的 Codex 登录（后台执行）"""
    from autoteam.accounts import find_account, load_accounts

    email = params.email.strip().lower()
    if _is_main_account_email(email):
        raise HTTPException(status_code=400, detail="主号不属于账号池登录对象")
    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    def _run():
        from autoteam.accounts import STATUS_ACTIVE, STATUS_PERSONAL, update_account
        from autoteam.cloudmail import CloudMailClient
        from autoteam.codex_auth import (
            check_codex_quota,
            login_codex_via_browser,
            quota_result_quota_info,
            quota_result_resets_at,
            save_auth_file,
        )
        from autoteam.invite import RegisterBlocked
        from autoteam.register_failures import record_failure

        # 账号状态决定登录模式：PERSONAL 走 use_personal=True 补个人号 OAuth；其他走 Team 模式
        use_personal = acc.get("status") == STATUS_PERSONAL

        mail_client = CloudMailClient()
        mail_client.login()
        # SPEC-2 §3.5.3 + Round 6 PRD-5 FR-P1.3:RegisterBlocked 转 409 phone_required / register_blocked
        # 注意:此函数在 _run_task 后台线程中运行,raise HTTPException 由 _run_task 转录到 task["error"];
        # task error 字符串携带 "phone_required" / "register_blocked" 关键字供前端解析(api.ts §SPEC-2 §1)
        try:
            bundle = login_codex_via_browser(
                email,
                acc.get("password", ""),
                mail_client=mail_client,
                use_personal=use_personal,
            )
        except RegisterBlocked as blocked:
            if blocked.is_phone:
                record_failure(
                    email,
                    category="oauth_phone_blocked",
                    reason=f"补登录触发 add-phone (step={blocked.step})",
                    step=blocked.step,
                    stage="api_login",
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "phone_required",
                        "step": blocked.step,
                        "reason": blocked.reason,
                    },
                )
            record_failure(
                email,
                category="exception",
                reason=f"补登录意外 RegisterBlocked: {blocked.reason}",
                step=blocked.step,
                stage="api_login",
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "register_blocked",
                    "step": blocked.step,
                    "reason": blocked.reason,
                },
            )
        if bundle:
            auth_file = save_auth_file(bundle)
            update_account(email, auth_file=auth_file, last_active_at=time.time())
            plan_type = (bundle.get("plan_type") or "").lower()

            if use_personal:
                # personal 补登录：不改状态（保持 PERSONAL），只刷新 auth_file
                update_account(email, status=STATUS_PERSONAL)
            elif plan_type == "team":
                from autoteam.admin_state import get_chatgpt_account_id

                update_account(email, status=STATUS_ACTIVE, workspace_account_id=get_chatgpt_account_id() or None)
                token = bundle.get("access_token")
                if token:
                    st, info = check_codex_quota(token)
                    if st == "ok" and isinstance(info, dict):
                        update_account(email, last_quota=info)
                    elif st == "exhausted":
                        from autoteam.manager import _quota_exhaustion_fields

                        quota_info = quota_result_quota_info(info)
                        update_account(
                            email,
                            status="exhausted",
                            **_quota_exhaustion_fields(
                                quota_info,
                                quota_result_resets_at(info) or int(time.time() + 18000),
                                "api_oauth_quota_exhausted",
                            ),
                        )
            # 同步到 CPA
            from autoteam.sync_targets import sync_to_configured_targets as sync_to_cpa

            sync_to_cpa()
            return {
                "email": email,
                "plan": bundle.get("plan_type"),
                "auth_file": auth_file,
                "mode": "personal" if use_personal else "team",
            }
        raise RuntimeError(f"Codex 登录失败: {email}")

    task = _start_task(f"login:{email}", _run, {"email": email})
    return task


@app.get("/api/status")
def get_status(fast: bool = False):
    """获取所有账号状态 + active 账号实时额度"""
    from autoteam.accounts import (
        STATUS_ACTIVE,
        STATUS_AUTH_INVALID,
        STATUS_EXHAUSTED,
        STATUS_ORPHAN,
        STATUS_PENDING,
        STATUS_PERSONAL,
        STATUS_STANDBY,
        is_account_disabled,
        load_accounts,
    )
    from autoteam.codex_auth import check_codex_quota, quota_result_quota_info

    accounts = load_accounts()
    quota_cache = {}

    if not fast:
        for acc in accounts:
            if not _is_main_account_email(acc.get("email")) and is_account_disabled(acc):
                continue
            if acc["status"] not in (STATUS_ACTIVE, STATUS_PERSONAL) and not _is_main_account_email(acc.get("email")):
                continue

            auth_file = _resolve_status_auth_file(acc)
            if not auth_file:
                continue

            try:
                auth_data = json.loads(read_text(Path(auth_file)))
                access_token = auth_data.get("access_token")
                if access_token:
                    status, info = check_codex_quota(access_token)
                    if status == "ok" and isinstance(info, dict):
                        quota_cache[acc["email"]] = info
                    elif status == "exhausted":
                        quota_info = quota_result_quota_info(info)
                        if quota_info:
                            quota_cache[acc["email"]] = quota_info
            except Exception:
                pass

    sanitized_accounts = [_sanitize_account(a, quota_cache.get(a.get("email"))) for a in accounts]
    sanitized_accounts.sort(key=_account_pool_sort_key)

    summary = {
        "active": sum(1 for a in sanitized_accounts if a["status"] == STATUS_ACTIVE),
        "standby": sum(1 for a in sanitized_accounts if a["status"] == STATUS_STANDBY),
        "exhausted": sum(1 for a in sanitized_accounts if a["status"] == STATUS_EXHAUSTED),
        "pending": sum(1 for a in sanitized_accounts if a["status"] == STATUS_PENDING),
        "personal": sum(1 for a in sanitized_accounts if a["status"] == STATUS_PERSONAL),
        "auth_invalid": sum(1 for a in sanitized_accounts if a["status"] == STATUS_AUTH_INVALID),
        "orphan": sum(1 for a in sanitized_accounts if a["status"] == STATUS_ORPHAN),
        "disabled": sum(1 for a in sanitized_accounts if a["status"] == "disabled"),
        "total": len(sanitized_accounts),
    }

    return {
        "accounts": sanitized_accounts,
        "summary": summary,
        "quota_cache": quota_cache,
        "runtime_resources": _safe_runtime_resource_snapshot(),
        "ipv6_pool": _safe_ipv6_pool_status(),
        "cliproxy": _safe_cliproxy_health(timeout=0.5, cache_ttl=30.0) if fast else _safe_cliproxy_health(),
        "multi_master": _safe_multi_master_status(),
        "status_mode": {
            "fast": fast,
            "quota_budget_seconds": 0 if fast else None,
            "live_quota": not fast,
        },
        "rotation_validation": {
            **_rotation_validation_cooldown,
            "cooldown_remaining_seconds": int(_rotation_validation_cooldown_remaining()),
        },
    }


def _is_auth_repair_misclassification_candidate(acc: dict) -> tuple[bool, str]:
    from autoteam.accounts import STATUS_AUTH_INVALID

    email = str(acc.get("email") or "").strip().lower()
    if not email:
        return False, "missing_email"
    if _is_main_account_email(email):
        return False, "main_account"
    if acc.get("status") != STATUS_AUTH_INVALID:
        return False, "status_not_auth_invalid"
    if str(acc.get("auth_last_error") or "").strip() not in {"exception", "login_failed"}:
        return False, "error_type_not_transient"
    detail = str(acc.get("auth_last_error_detail") or "").lower()
    if "page.screenshot" not in detail:
        return False, "detail_not_screenshot_timeout"
    auth_file = acc.get("auth_file")
    if not auth_file:
        return False, "missing_auth_file"
    if not Path(auth_file).exists():
        return False, "auth_file_not_found"
    return True, "matched"


def _auth_repair_recovery_candidates(accounts: list[dict], *, emails: list[str] | None = None) -> list[dict]:
    selected = {_normalized_email(email) for email in (emails or []) if _normalized_email(email)}
    rows = []
    for acc in accounts:
        email = _normalized_email(acc.get("email"))
        if selected and email not in selected:
            continue
        matched, reason = _is_auth_repair_misclassification_candidate(acc)
        if not matched:
            continue
        rows.append(
            {
                "email": email,
                "status": acc.get("status"),
                "auth_last_error": acc.get("auth_last_error"),
                "auth_last_error_detail": acc.get("auth_last_error_detail"),
                "auth_file": acc.get("auth_file"),
                "reason": reason,
            }
        )
    return rows


@app.get("/api/auth-repair/recovery-candidates")
def get_auth_repair_recovery_candidates():
    """只读列出被 auth_repair 临时异常误标 auth_invalid 的候选账号。"""
    from autoteam.accounts import load_accounts

    candidates = _auth_repair_recovery_candidates(load_accounts())
    return {
        "candidates": candidates,
        "count": len(candidates),
        "criteria": {
            "status": "auth_invalid",
            "auth_last_error": ["exception", "login_failed"],
            "auth_last_error_detail_contains": "Page.screenshot",
            "auth_file_exists": True,
            "exclude_main_account": True,
        },
    }


@app.post("/api/auth-repair/recover-misclassified")
def post_auth_repair_recover_misclassified(params: AuthRepairRecoverParams):
    """恢复精确匹配的误伤账号；默认 dry-run，apply=true 才写回 standby。"""
    from autoteam.accounts import STATUS_STANDBY, load_accounts, update_account

    accounts = load_accounts()
    candidates = _auth_repair_recovery_candidates(accounts, emails=params.emails)
    restored = []
    if params.apply:
        for item in candidates:
            email = item["email"]
            update_account(
                email,
                status=STATUS_STANDBY,
                auth_retry_count=0,
                auth_last_error=None,
                auth_last_error_detail=None,
                auth_last_failed_at=None,
                auth_retry_after=None,
                auth_retry_paused=False,
                _reason="recover_auth_repair_misclassification",
            )
            restored.append(email)

    return {
        "apply": params.apply,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "restored": restored,
        "restored_count": len(restored),
        "next_step": "run read-only quota/auth probe before publishing to CPA" if restored else "dry-run only",
    }


@app.post("/api/sync")
def post_sync():
    """同步认证文件到 CPA"""
    from autoteam.sync_targets import sync_to_configured_targets as sync_to_cpa

    sync_to_cpa()
    return {"message": "同步完成"}


@app.post("/api/sync/from-cpa")
def post_sync_from_cpa():
    """从 CPA 反向同步认证文件到本地。"""
    from autoteam.cpa_sync import sync_from_cpa

    result = sync_from_cpa()
    return {"message": "已从 CPA 同步到本地", "result": result}


@app.get("/api/register-failures")
def get_register_failures_api(limit: int = 50):
    """返回最近的注册/OAuth 失败明细，前端用来展示"为什么账号没生产出来"。"""
    from autoteam.register_failures import count_by_category, list_failures

    return {
        "items": list_failures(limit=max(1, min(limit, 500))),
        "counts": count_by_category(),
    }


def _extract_mail_codes(*parts: str) -> list[str]:
    text = "\n".join(str(part or "") for part in parts)

    # The inbox UI should show only the most trustworthy OTP for a message.
    # Returning every six-digit token makes HTML footers, dates and tracking
    # snippets look like multiple verification codes.
    contextual_patterns = [
        r"(?is)(?:temporary\s+)?(?:openai|chatgpt)\s+(?:login\s+)?code(?:\s+is)?\D{0,80}(\d{6})",
        r"(?is)(?:verification|login|security|one[-\s]?time|auth(?:entication)?)\s+code(?:\s+is)?\D{0,80}(\d{6})",
        r"(?is)(?:验证码|一次性代码|登录代码|安全代码|代码)(?:\s*(?:为|是|:|：))?\D{0,40}(\d{6})",
        r"(?is)\b(\d{6})\b\D{0,60}(?:is\s+your|your)\s+(?:temporary\s+)?(?:openai|chatgpt)?\s*(?:login|verification|security|one[-\s]?time)?\s*code\b",
    ]
    for pattern in contextual_patterns:
        match = re.search(pattern, text)
        if match:
            return [match.group(1)]

    candidates: list[str] = []
    for match in re.findall(r"(?<!\d)(\d{6})(?!\d)", text):
        if match not in candidates:
            candidates.append(match)
    return candidates if len(candidates) == 1 else []


@app.get("/api/mail/inbox")
def get_mail_inbox(address: str, limit: int = 10):
    """Read recent messages for a configured temporary-mail address."""
    normalized = (address or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise HTTPException(status_code=400, detail="请填写有效邮箱地址")
    safe_limit = max(1, min(50, int(limit or 10)))

    try:
        from autoteam.mail import get_mail_client

        client = get_mail_client()
        login = getattr(client, "login", None)
        if callable(login):
            login()
        search = getattr(client, "search_emails_by_recipient", None)
        if not callable(search):
            raise RuntimeError("当前邮箱后端不支持按收件人查询邮件")
        mails = search(normalized, size=safe_limit)
    except Exception as exc:
        logger.exception("[MailInbox] 查询 %s 失败", normalized)
        raise HTTPException(status_code=400, detail=f"查询邮件失败: {exc}") from exc

    from autoteam.mail.base import html_to_visible_text

    items = []
    for mail in mails or []:
        subject = mail.get("subject") or ""
        text = mail.get("text") or ""
        content = mail.get("content") or ""
        raw = mail.get("raw") or ""
        code = None
        extractor = getattr(client, "extract_verification_code", None)
        if callable(extractor):
            try:
                code = extractor(mail)
            except Exception as exc:
                logger.debug("[MailInbox] 统一验证码提取失败,回退正则: %s", exc)
        codes = [code] if code else _extract_mail_codes(subject, text, content)
        if not codes:
            codes = _extract_mail_codes(raw)
        body = text or html_to_visible_text(content) or raw or ""
        body_truncated = len(body) > 20000
        if body_truncated:
            body = body[:20000]
        items.append(
            {
                "id": mail.get("emailId") or mail.get("id"),
                "created_at": mail.get("createTime") or mail.get("created_at"),
                "from": mail.get("sendEmail") or mail.get("sender") or mail.get("source"),
                "to": mail.get("receiveEmail") or mail.get("accountEmail") or normalized,
                "subject": subject,
                "codes": codes,
                "body": body,
                "body_truncated": body_truncated,
                "preview": body[:600],
            }
        )

    return {"address": normalized, "count": len(items), "items": items}


@app.get("/api/config/register-domain")
def get_register_domain_api():
    """读取当前子号注册使用的 CloudMail 域名。"""
    from autoteam.config import CLOUDMAIL_DOMAIN
    from autoteam.runtime_config import get, get_register_domain

    override = (get("register_domain") or "").strip()
    return {
        "domain": get_register_domain(),
        "override": override,
        "env_default": (CLOUDMAIL_DOMAIN or "").lstrip("@").strip(),
    }


@app.put("/api/config/register-domain")
def put_register_domain_api(params: RegisterDomainParams):
    """
    更新子号注册域名。verify=True（默认）会试探性调用 CloudMail new_address 验证服务端是否接受此域，
    成功则立即删除探测地址再保存；失败把 CloudMail 原始错误透传给前端。

    SPEC-1 §FR-005 — 与 `/api/mail-provider/probe?step=domain_ownership` 共享回收语义:
    本路径走 `CloudMailClient` 抽象(已对齐 maillab/cf_temp_email),
    `probe.probe_domain_ownership` 走无状态 HTTP 直连,二者最终行为一致(创建 + 立即 DELETE,
    回收失败 leaked_probe 透传)。改 probe 时需同步检查本函数。
    """
    from autoteam.cloudmail import CloudMailClient
    from autoteam.runtime_config import set_register_domain

    cleaned = (params.domain or "").strip().lstrip("@").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="域名不能为空")

    leaked_probe = None
    if params.verify:
        probe_prefix = f"probe{int(time.time())}"
        acct_id = None
        probe_email = None
        try:
            client = CloudMailClient()
            client.login()
            acct_id, probe_email = client.create_temp_email(prefix=probe_prefix, domain=cleaned)
        except Exception as exc:
            # CloudMail 返回 "Invalid domain" 等错误直接透传
            raise HTTPException(status_code=400, detail=f"域名验证失败: {exc}") from exc
        # 探测地址用完立即回收;删除失败也要让前端看到,否则 CloudMail 会积压僵尸地址
        try:
            if acct_id is not None:
                client.delete_account(acct_id)
        except Exception as exc:
            logger.warning("[config] 删除域名探测邮箱失败 (%s, id=%s): %s", probe_email, acct_id, exc)
            leaked_probe = {"email": probe_email, "acct_id": acct_id, "error": str(exc)}

    set_register_domain(cleaned)
    logger.info("[config] register_domain 已切换为 @%s", cleaned)
    resp = {"message": f"注册域名已切换为 @{cleaned}", "domain": cleaned}
    if leaked_probe:
        resp["warning"] = (
            f"域名已保存,但探测邮箱 {leaked_probe['email']} 回收失败,请手动在 CloudMail 删除"
            f" (id={leaked_probe['acct_id']}): {leaked_probe['error']}"
        )
        resp["leaked_probe"] = leaked_probe
    return resp


@app.get("/api/config/preferred-seat-type")
def get_preferred_seat_type_api():
    """SPEC-2 FR-G — 读取邀请席位偏好。"""
    from autoteam.runtime_config import get_preferred_seat_type
    return {"value": get_preferred_seat_type()}


@app.put("/api/config/preferred-seat-type")
def put_preferred_seat_type_api(params: PreferredSeatTypeParams):
    """SPEC-2 FR-G — 切换邀请席位偏好(default | codex)。"""
    from autoteam.runtime_config import set_preferred_seat_type
    val = (params.value or "").strip().lower()
    if val not in ("default", "codex"):
        raise HTTPException(status_code=400, detail="value 必须为 'default' 或 'codex'")
    saved = set_preferred_seat_type(val)
    logger.info("[config] preferred_seat_type 已切换为 %s", saved)
    return {"value": saved, "message": f"邀请席位偏好已设为 {saved}"}


@app.get("/api/config/sync-probe")
def get_sync_probe_api():
    """SPEC-2 FR-E — 读取 sync_account_states 被踢探测的并发/冷却配置。"""
    from autoteam.runtime_config import get_sync_probe_concurrency, get_sync_probe_cooldown_minutes
    return {
        "concurrency": get_sync_probe_concurrency(),
        "cooldown_minutes": get_sync_probe_cooldown_minutes(),
    }


@app.put("/api/config/sync-probe")
def put_sync_probe_api(params: SyncProbeParams):
    """SPEC-2 FR-E — 更新 sync_account_states 被踢探测的并发/冷却(任一非空字段都生效)。"""
    from autoteam.runtime_config import (
        get_sync_probe_concurrency,
        get_sync_probe_cooldown_minutes,
        set_sync_probe_concurrency,
        set_sync_probe_cooldown_minutes,
    )
    if params.concurrency is not None:
        set_sync_probe_concurrency(params.concurrency)
    if params.cooldown_minutes is not None:
        set_sync_probe_cooldown_minutes(params.cooldown_minutes)
    return {
        "concurrency": get_sync_probe_concurrency(),
        "cooldown_minutes": get_sync_probe_cooldown_minutes(),
        "message": "sync 探测配置已更新",
    }


@app.post("/api/sync/accounts")
def post_sync_accounts():
    """从 auths 目录和 Team 成员同步账号到 accounts.json"""
    from autoteam.manager import sync_account_states

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再同步"))

    try:
        result = _pw_executor.run(sync_account_states)
    finally:
        _playwright_lock.release()
    if isinstance(result, dict) and result.get("ok") is False:
        raise HTTPException(status_code=502, detail=result)

    from autoteam.accounts import load_accounts

    accounts = load_accounts()
    return {"message": f"同步完成，共 {len(accounts)} 个账号", "total": len(accounts), "result": result}


@app.get("/api/team/members")
def get_team_members():
    """获取 Team 全部成员（包括手动添加的外部成员）"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id

    if not get_admin_session_token() or not get_chatgpt_account_id():
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再查询"))

    try:

        def _fetch_team_members():
            from autoteam.account_ops import (
                fetch_team_state,
                team_invite_email,
                team_member_email,
                team_member_role,
                team_member_user_id,
            )
            from autoteam.accounts import load_accounts
            from autoteam.chatgpt_api import ChatGPTTeamAPI
            from autoteam.manager import _chatgpt_user_id_for_email

            chatgpt = ChatGPTTeamAPI()
            try:
                chatgpt.start()
                members, invites = fetch_team_state(chatgpt)
                local_accounts = load_accounts()
                local_emails = {a["email"].lower() for a in local_accounts}
                local_by_user_id = {
                    user_id: a
                    for a in local_accounts
                    for user_id in [_chatgpt_user_id_for_email(a.get("email"))]
                    if user_id
                }

                result = []
                for m in members:
                    email = team_member_email(m)
                    user_id = team_member_user_id(m) or ""
                    matched_email = ""
                    if not email and user_id in local_by_user_id:
                        matched_email = (local_by_user_id[user_id].get("email") or "").lower()
                        email = matched_email
                    result.append(
                        {
                            "email": email,
                            "role": team_member_role(m) or "",
                            "user_id": user_id,
                            "is_local": email in local_emails,
                            "matched_by_user_id": bool(matched_email),
                            "type": "member",
                        }
                    )
                for inv in invites:
                    email = team_invite_email(inv)
                    result.append(
                        {
                            "email": email,
                            "role": inv.get("role", ""),
                            "user_id": inv.get("id", ""),
                            "is_local": email in local_emails,
                            "type": "invite",
                        }
                    )
                return {"members": result, "total": len(members), "invites": len(invites)}
            finally:
                chatgpt.stop()

        try:
            return _pw_executor.run(_fetch_team_members)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[Team成员] 查询失败: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={"error": "team_members_fetch_failed", "message": _body_preview(exc, 800)},
            ) from exc
    finally:
        _playwright_lock.release()


@app.post("/api/team/members/remove")
def post_team_member_remove(params: TeamMemberRemoveParams):
    """移出 Team 成员或取消邀请。"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id

    if not get_admin_session_token() or not get_chatgpt_account_id():
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再操作"))

    try:
        from autoteam.accounts import find_account, load_accounts, update_account

        email = params.email.strip().lower()
        user_id = params.user_id.strip()
        member_type = params.type.strip().lower()

        if not email or not user_id:
            raise HTTPException(status_code=400, detail="缺少必要参数")
        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许从 Team 成员页移出")
        if member_type not in ("member", "invite"):
            raise HTTPException(status_code=400, detail="无效的成员类型")

        account_id = get_chatgpt_account_id()

        def _do_remove_team_member():
            from autoteam.account_ops import delete_team_invite, fetch_team_state, team_invite_email
            from autoteam.chatgpt_api import ChatGPTTeamAPI

            chatgpt = ChatGPTTeamAPI()
            try:
                chatgpt.start()
                if member_type == "invite":
                    result = delete_team_invite(chatgpt, account_id, invite_id=user_id, email=email)
                    action_text = "取消邀请"
                    if result.get("status") in (200, 204):
                        deadline = time.time() + 20
                        while time.time() < deadline:
                            try:
                                _members, invites = fetch_team_state(chatgpt)
                                if not any(team_invite_email(inv) == email for inv in invites):
                                    break
                            except Exception:
                                pass
                            time.sleep(2)
                        else:
                            return {"status": 409, "body": "invite still present after delete"}, action_text
                else:
                    path = f"/backend-api/accounts/{account_id}/users/{user_id}"
                    result = chatgpt._api_fetch("DELETE", path)
                    action_text = "移出 Team"
                return result, action_text
            finally:
                chatgpt.stop()

        result, action_text = _pw_executor.run(_do_remove_team_member)
        if result["status"] not in (200, 204):
            raise HTTPException(status_code=500, detail=f"{action_text}失败: HTTP {result['status']}")

        accounts = load_accounts()
        acc = find_account(accounts, email)
        if acc:
            update_account(email, status="standby")

        return {
            "message": f"已{action_text}: {email}",
            "email": email,
            "type": member_type,
        }
    finally:
        _playwright_lock.release()


# ---------------------------------------------------------------------------
# 日志收集
# ---------------------------------------------------------------------------

_log_buffer: list[dict] = []
_LOG_BUFFER_MAX = 500


class _LogCollector(logging.Handler):
    """收集日志到内存 buffer，供前端查询"""

    def emit(self, record):
        entry = {
            "time": record.created,
            "level": record.levelname,
            "message": self.format(record),
        }
        _log_buffer.append(entry)
        if len(_log_buffer) > _LOG_BUFFER_MAX:
            del _log_buffer[: len(_log_buffer) - _LOG_BUFFER_MAX]


_log_collector = _LogCollector()
_log_collector.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_collector)


@app.get("/api/logs")
def get_logs(limit: int = 100, since: float = 0):
    """获取最近的日志"""
    if since > 0:
        entries = [e for e in _log_buffer if e["time"] > since]
    else:
        entries = _log_buffer[-limit:]
    return {"logs": entries, "total": len(_log_buffer)}


@app.post("/api/sync/main-codex")
def post_sync_main_codex():
    """兼容旧接口：开始主号 Codex 登录并同步到已启用远端目标。"""
    return post_main_codex_start()


@app.get("/api/cpa/files")
def get_cpa_files():
    """获取 CPA 中的认证文件列表"""
    from autoteam.cpa_sync import list_cpa_files

    return list_cpa_files()


# ---------------------------------------------------------------------------
# 后台任务端点
# ---------------------------------------------------------------------------


class CheckParams(BaseModel):
    include_standby: bool = False  # True 时额外探测 standby 池(限速+24h 去重)


@app.post("/api/tasks/check", status_code=202)
def post_check(params: CheckParams = CheckParams()):
    """检查所有 active 账号额度（后台执行）。include_standby=True 时追加探测 standby 池。"""
    from autoteam.manager import cmd_check

    include_standby = bool(params.include_standby)

    def _run():
        exhausted = cmd_check(include_standby=include_standby)
        return {"exhausted": [a["email"] for a in exhausted]}

    task = _start_task("check", _run, {"include_standby": include_standby})
    return task


@app.post("/api/tasks/rotate", status_code=202)
def post_rotate(params: TaskParams = TaskParams()):
    """智能轮转（后台执行）"""
    from autoteam.manager import cmd_rotate

    task = _start_task(
        "rotate",
        lambda target: cmd_rotate(target, force_auth_repair=False, background_post_sync=True),
        {"target": params.target},
        params.target,
    )
    return task


class ReplaceParams(BaseModel):
    email: str
    reason: str = "manual"


@app.post("/api/tasks/replace", status_code=202)
def post_replace(params: ReplaceParams):
    """定点替换一个 Team 子号:kick + 补一个(标准行为:优先 standby 复用,否则新号)。

    失效一个立即轮换一个的手动触发入口,也可由 auto-check 自动调用 cmd_replace_batch。
    """
    from autoteam.manager import cmd_replace_one

    email = (params.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email 不能为空")
    task = _start_task(
        "replace",
        cmd_replace_one,
        {"email": email, "reason": params.reason},
        email,
        params.reason,
    )
    return task


@app.post("/api/tasks/add", status_code=202)
def post_add():
    """添加新账号（后台执行）"""
    from autoteam.manager import cmd_add

    task = _start_task("add", cmd_add, {})
    return task


@app.post("/api/tasks/fill", status_code=202)
def post_fill(params: TaskParams = TaskParams()):
    """补满 Team 成员（后台执行）。leave_workspace=True 时切换为"生产免费号"模式

    fill-personal 模式下额外做一次轻量预检:Team 子号已满 TEAM_SUB_ACCOUNT_HARD_CAP
    则直接返回 409,不启动后台任务(队列化拒绝,Solution C)。本地状态足够用,无需启动
    Playwright 远程查询,避免给前端按错按钮带来额外开销。
    """
    from autoteam.manager import (
        TEAM_SUB_ACCOUNT_HARD_CAP,
        _count_local_team_seat_accounts,
        _schedule_post_task_sync,
        cmd_fill,
    )

    if params.leave_workspace:
        from autoteam.accounts import load_accounts

        in_team_local = _count_local_team_seat_accounts(load_accounts())
        if in_team_local >= TEAM_SUB_ACCOUNT_HARD_CAP:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Team 子号已满 {in_team_local}/{TEAM_SUB_ACCOUNT_HARD_CAP},"
                    "fill-personal 拒绝执行。请先等子号自然 exhausted 或手动腾位置后再试"
                ),
            )

    # Round 8 M-T3 + Round 9 AC-B4 — fill 任务起点统一 master probe,Team / Personal 都 fail-fast。
    # cancel 状态下整批 fill 必拿 plan_type=team,直接 503 fail-fast 不启动 task。
    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI
        from autoteam.master_health import is_master_subscription_healthy

        def _probe_master():
            api = ChatGPTTeamAPI()
            try:
                api.start()
                return is_master_subscription_healthy(api)
            finally:
                try:
                    api.stop()
                except Exception:
                    pass

        if _playwright_lock.acquire(blocking=False):
            try:
                healthy, reason, evidence = _pw_executor.run(_probe_master)
            finally:
                _playwright_lock.release()
            if not healthy and reason == "subscription_cancelled":
                msg = (
                    "母号 ChatGPT Team 订阅已 cancel(eligible_for_auto_reactivation=true),"
                    "fill 必拿 plan_type=team / free。请先续订或更换母号"
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "master_subscription_degraded",
                        "reason": reason,
                        "evidence": evidence,
                        "message": msg,
                        "leave_workspace": params.leave_workspace,
                    },
                )
        else:
            # playwright 锁拿不到 → 别阻塞 fill,放行让任务自身的 M-T1 / M-T2 兜底
            pass
    except HTTPException:
        raise
    except Exception:
        # probe 异常按 spec §6.1 不阻塞 — M-T1 / M-T2 在 _run_post_register_oauth 兜底
        pass

    def _cmd_fill_api_task(target: int, *, leave_workspace: bool = False):
        if leave_workspace:
            return cmd_fill(target, leave_workspace=True)
        result = cmd_fill(target, leave_workspace=False, post_sync=False, print_status=False)
        _schedule_post_task_sync("[填充]")
        return result

    command = "fill-personal" if params.leave_workspace else "fill"
    task = _start_task(
        command,
        _cmd_fill_api_task,
        {"target": params.target, "leave_workspace": params.leave_workspace},
        params.target,
        leave_workspace=params.leave_workspace,
    )
    return task


@app.post("/api/tasks/multi-master/fill", status_code=202)
def post_multi_master_fill(params: MultiMasterFillParams = MultiMasterFillParams()):
    """多母号并行补齐 Team 子号。"""
    from autoteam.multi_master import run_multi_master_fill

    payload = params.model_dump()

    def _run():
        return run_multi_master_fill(
            target_seats=params.target,
            owner_workers=params.owner_workers,
            direct_parallel=params.direct_parallel,
            workspace_ids=params.workspace_ids,
            dry_run=params.dry_run,
        )

    if params.dry_run:
        return {
            "task_id": None,
            "command": "multi-master-fill",
            "status": "completed",
            "params": payload,
            "result": _run(),
        }
    task = _start_task(
        "multi-master-fill",
        _run,
        payload,
    )
    return task


@app.post("/api/tasks/cleanup", status_code=202)
def post_cleanup(params: CleanupParams = CleanupParams()):
    """清理多余成员（后台执行）"""
    from autoteam.manager import cmd_cleanup

    task = _start_task("cleanup", cmd_cleanup, {"max_seats": params.max_seats}, params.max_seats)
    return task


@app.post("/api/tasks/reset-quota", status_code=202)
def post_reset_quota():
    """清空本地额度恢复记录，并恢复 exhausted 账号为可检查状态（后台执行）"""
    from autoteam.manager import cmd_reset_quota_recovery

    task = _start_task("reset-quota", cmd_reset_quota_recovery, {})
    return task


@app.get("/api/tasks")
def get_tasks():
    """查看所有任务"""
    sorted_tasks = sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return sorted_tasks


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """查看任务状态"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/tasks/cancel", status_code=202)
def post_task_cancel():
    """
    请求当前正在运行的任务在下一个安全点退出。
    协作式:后台 worker 在每个批次/账号边界检查 cancel_signal.is_cancelled(),
    调用这里后等 10-30s 让当前步骤跑完,任务状态会在 task["status"] 里显示为 "cancelled"。
    """
    from autoteam import cancel_signal

    if not _current_task_id:
        raise HTTPException(status_code=404, detail="当前没有正在运行的任务")
    task = _tasks.get(_current_task_id) or {}
    if task.get("status") not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"任务当前状态 {task.get('status')} 无法取消")
    cancel_signal.request_cancel(f"手动停止 task={_current_task_id[:8]}")
    task["cancel_requested"] = True
    return {
        "message": "已请求中止,等待当前步骤安全退出",
        "task_id": _current_task_id,
        "command": task.get("command"),
    }


# ---------------------------------------------------------------------------
# Round 12 F2 — SSE: rotate 实时进度推送
# ---------------------------------------------------------------------------
# 订阅 S1 commit ef1637c 的事件总线 (default_machine.subscribe),把每次状态
# 转移序列化为 SSE event 行 (data: {...}\n\n)。心跳 15s 一次 (": heartbeat\n\n")
# 维持 EventSource 连接、绕过 proxy idle timeout。
#
# 设计要点:
# - subscribe 回调在调用线程上同步执行 (S1 实现);用线程安全 SimpleQueue 把事件
#   中转给 sync generator,避免 generator 阻塞而 callback 被持续触发导致 OOM。
# - 客户端断开 (浏览器 close / refresh / 进程退出) 时 FastAPI 关闭 generator,
#   try/finally 块负责 unsubscribe 防泄漏。
# - generator 是 sync 而非 async,FastAPI 用 anyio threadpool 跑,uvicorn 不会被阻塞。
# - 每条 event payload schema (与 account_state.Transition.to_jsonl 对齐):
#     {"email":..., "from":..., "to":..., "reason":..., "ts":..., "extra":{...}}
# - 没有 manager.py 改动 (本任务范围外);S3/S4 任务把 transition 调用塞进
#   manager.py 的 rotate 路径,本端点会自动看到事件。

import queue as _sse_queue


def _build_sse_event_stream(machine, *, heartbeat_seconds: float = 15.0):
    """Return a sync generator yielding SSE-formatted bytes from machine subscriber.

    Pulled out of the route so unit tests can drive it without spinning up
    Starlette/uvicorn — see ``tests/unit/test_round12_rotate_sse_stream.py``.
    """
    q: _sse_queue.Queue = _sse_queue.Queue(maxsize=1024)
    _SENTINEL = object()

    def _on_transition(transition):
        try:
            payload = {
                "email": transition.email,
                "from": transition.from_state.value if transition.from_state else None,
                "to": transition.to_state.value,
                "reason": transition.reason or "",
                "ts": transition.timestamp,
                "extra": dict(transition.extra or {}),
            }
            # Round 12 wire-up (minor m7) — bounded queue. If the slow SSE
            # client lets the buffer fill, drop oldest to make room rather
            # than growing unbounded memory.
            try:
                q.put_nowait(payload)
            except _sse_queue.Full:
                try:
                    q.get_nowait()  # drop oldest
                except _sse_queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except _sse_queue.Full:
                    logger.warning("[sse] queue still full after drop, event lost")
        except Exception:
            logger.exception("[sse] failed to enqueue transition %r", transition)

    machine.subscribe(_on_transition)

    def _generator():
        try:
            # 立刻发一次 retry 提示 + 心跳,让前端尽早确认连接成功
            yield b"retry: 5000\n\n"
            yield b": connected\n\n"
            while True:
                try:
                    payload = q.get(timeout=heartbeat_seconds)
                except _sse_queue.Empty:
                    yield b": heartbeat\n\n"
                    continue
                if payload is _SENTINEL:
                    break
                line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                yield line.encode("utf-8")
        finally:
            machine.unsubscribe(_on_transition)

    return _generator(), q, _on_transition


@app.get("/api/rotate/stream")
def get_rotate_stream(request: Request):
    """Server-Sent Events: 推送账号状态转移事件到前端 (rotate 实时进度面板)。

    Content-Type: text/event-stream
    Cache-Control: no-cache (代理不要缓存)
    X-Accel-Buffering: no (Nginx 不要缓冲,确保 chunk 及时落地浏览器)
    """
    from autoteam.account_state import default_machine

    generator, _q, _cb = _build_sse_event_stream(default_machine)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# 后台自动巡检
# ---------------------------------------------------------------------------

from autoteam.config import (
    AUTO_CHECK_INTERVAL as _DEFAULT_INTERVAL,
)
from autoteam.config import (
    AUTO_CHECK_MIN_LOW as _DEFAULT_MIN_LOW,
)
from autoteam.config import (
    AUTO_CHECK_TARGET_SEATS as _DEFAULT_TARGET_SEATS,
)
from autoteam.config import (
    AUTO_CHECK_THRESHOLD as _DEFAULT_THRESHOLD,
)
from autoteam.config import (
    AUTO_CHECK_BURN_GUARD_COOLDOWN_SECONDS as _DEFAULT_BURN_GUARD_COOLDOWN_SECONDS,
)
from autoteam.config import (
    AUTO_CHECK_BURN_GUARD_ENABLED as _DEFAULT_BURN_GUARD_ENABLED,
)
from autoteam.config import (
    AUTO_CHECK_BURN_GUARD_MAX_EXHAUSTED as _DEFAULT_BURN_GUARD_MAX_EXHAUSTED,
)
from autoteam.config import (
    AUTO_CHECK_BURN_GUARD_WINDOW_SECONDS as _DEFAULT_BURN_GUARD_WINDOW_SECONDS,
)
from autoteam.config import (
    AUTO_CHECK_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS as _DEFAULT_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS,
)
from autoteam.config import (
    AUTO_CHECK_AUTH_REPAIR_GUARD_ENABLED as _DEFAULT_AUTH_REPAIR_GUARD_ENABLED,
)
from autoteam.config import (
    AUTO_CHECK_AUTH_REPAIR_GUARD_MAX_INVALID as _DEFAULT_AUTH_REPAIR_GUARD_MAX_INVALID,
)
from autoteam.config import (
    AUTO_CHECK_AUTH_REPAIR_GUARD_WINDOW_SECONDS as _DEFAULT_AUTH_REPAIR_GUARD_WINDOW_SECONDS,
)

# 运行时可修改的巡检配置
_auto_check_config = {
    "interval": _DEFAULT_INTERVAL,
    "target_seats": _DEFAULT_TARGET_SEATS,
    "threshold": _DEFAULT_THRESHOLD,
    "min_low": _DEFAULT_MIN_LOW,
}
_auto_check_stop = threading.Event()
_auto_check_restart = threading.Event()  # 配置变更时通知线程重启


def _resolve_auto_check_target_seats(cfg: dict[str, int | bool]) -> int:
    try:
        return max(1, min(3, int(cfg.get("target_seats", 3))))
    except Exception:
        return 3

# auto-fill watchdog 冷却:防止反复触发 cmd_rotate 导致 OpenAI 对短时间内
# 多次 invite/kick 的子号批量 revoke token。30 分钟内只触发一次,给 OpenAI
# 风控系统冷却时间。0 表示从未触发过。
_auto_fill_last_trigger_ts = 0.0
_AUTO_FILL_COOLDOWN_SECONDS = 1800  # 30 min


def _collect_recent_quota_exhaustions(accounts: list[dict], *, now: float, lookback_seconds: int) -> list[dict]:
    cutoff = now - max(1, int(lookback_seconds))
    events: list[dict] = []

    try:
        from autoteam.account_state import DEFAULT_LOG_PATH

        path = Path(DEFAULT_LOG_PATH)
        if path.exists():
            for line in read_text(path).splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("from_state") != "active" or row.get("to_state") != "exhausted":
                    continue
                email = str(row.get("email") or "")
                if not email or _is_main_account_email(email):
                    continue
                try:
                    ts = float(row.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if ts >= cutoff:
                    events.append({"email": email, "timestamp": ts, "source": "state_log"})
    except Exception as exc:
        logger.warning("[巡检] 烧号熔断读取 state_log 失败: %s", exc)

    if events:
        return sorted(events, key=lambda item: item["timestamp"])

    # 降级兜底:如果 state_log 不可读/为空,用 accounts.json 当前字段判断。
    for acc in accounts:
        email = str(acc.get("email") or "")
        if not email or _is_main_account_email(email):
            continue
        try:
            ts = float(acc.get("quota_exhausted_at") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            events.append({"email": email, "timestamp": ts, "source": "accounts"})
    return sorted(events, key=lambda item: item["timestamp"])


def _auto_check_burn_guard_status(accounts: list[dict], *, now: float | None = None) -> dict:
    if not _DEFAULT_BURN_GUARD_ENABLED:
        return {"blocked": False, "enabled": False}

    now_ts = time.time() if now is None else float(now)
    window = int(_DEFAULT_BURN_GUARD_WINDOW_SECONDS)
    max_exhausted = int(_DEFAULT_BURN_GUARD_MAX_EXHAUSTED)
    cooldown = int(_DEFAULT_BURN_GUARD_COOLDOWN_SECONDS)
    lookback = max(window, cooldown)
    events = _collect_recent_quota_exhaustions(accounts, now=now_ts, lookback_seconds=lookback)
    recent_window_emails = sorted(
        {
            str(item["email"])
            for item in events
            if float(item["timestamp"]) >= now_ts - window
        }
    )

    trip_at = 0.0
    trip_emails: list[str] = []
    for idx, event in enumerate(events):
        event_ts = float(event["timestamp"])
        cluster_start = event_ts - window
        cluster = [
            item for item in events[: idx + 1]
            if cluster_start <= float(item["timestamp"]) <= event_ts
        ]
        unique_emails = sorted({str(item["email"]) for item in cluster})
        if len(unique_emails) >= max_exhausted:
            trip_at = event_ts
            trip_emails = unique_emails

    blocked_until = trip_at + cooldown if trip_at else 0.0
    blocked = bool(blocked_until and blocked_until > now_ts)
    return {
        "blocked": blocked,
        "enabled": True,
        "count": len(trip_emails),
        "emails": trip_emails,
        "recent_window_count": len(recent_window_emails),
        "recent_window_emails": recent_window_emails,
        "tripped_at": trip_at or None,
        "blocked_until": blocked_until if blocked else None,
        "remaining_seconds": max(0, int(blocked_until - now_ts)) if blocked else 0,
        "window_seconds": window,
        "max_exhausted": max_exhausted,
        "cooldown_seconds": cooldown,
    }


def _collect_recent_auth_repair_invalidations(*, now: float, lookback_seconds: int) -> list[dict]:
    cutoff = now - lookback_seconds
    events: list[dict] = []
    try:
        from autoteam.account_state import DEFAULT_LOG_PATH

        path = Path(DEFAULT_LOG_PATH)
        if path.exists():
            for line in read_text(path).splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("from_state") != "active" or row.get("to_state") != "auth_invalid":
                    continue
                reason = str(row.get("reason") or "")
                if not reason.startswith("auth_repair:"):
                    continue
                error_type = reason.split(":", 1)[1].strip()
                if error_type not in {"exception", "login_failed"}:
                    continue
                email = str(row.get("email") or "")
                if not email or _is_main_account_email(email):
                    continue
                try:
                    ts = float(row.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if ts >= cutoff:
                    events.append(
                        {
                            "email": email,
                            "timestamp": ts,
                            "reason": reason,
                            "source": "state_log",
                        }
                    )
    except Exception as exc:
        logger.warning("[巡检] 认证修复熔断读取 state_log 失败: %s", exc)
    return sorted(events, key=lambda item: item["timestamp"])


def _auto_check_auth_repair_guard_status(*, now: float | None = None) -> dict:
    if not _DEFAULT_AUTH_REPAIR_GUARD_ENABLED:
        return {"blocked": False, "enabled": False}

    now_ts = time.time() if now is None else float(now)
    window = int(_DEFAULT_AUTH_REPAIR_GUARD_WINDOW_SECONDS)
    max_invalid = int(_DEFAULT_AUTH_REPAIR_GUARD_MAX_INVALID)
    cooldown = int(_DEFAULT_AUTH_REPAIR_GUARD_COOLDOWN_SECONDS)
    lookback = max(window, cooldown)
    events = _collect_recent_auth_repair_invalidations(now=now_ts, lookback_seconds=lookback)
    recent_window_emails = sorted(
        {
            str(item["email"])
            for item in events
            if float(item["timestamp"]) >= now_ts - window
        }
    )

    trip_at = 0.0
    trip_emails: list[str] = []
    for idx, event in enumerate(events):
        event_ts = float(event["timestamp"])
        cluster_start = event_ts - window
        cluster = [
            item for item in events[: idx + 1]
            if cluster_start <= float(item["timestamp"]) <= event_ts
        ]
        unique_emails = sorted({str(item["email"]) for item in cluster})
        if len(unique_emails) >= max_invalid:
            trip_at = event_ts
            trip_emails = unique_emails

    blocked_until = trip_at + cooldown if trip_at else 0.0
    blocked = bool(blocked_until and blocked_until > now_ts)
    return {
        "blocked": blocked,
        "enabled": True,
        "count": len(trip_emails),
        "emails": trip_emails,
        "recent_window_count": len(recent_window_emails),
        "recent_window_emails": recent_window_emails,
        "tripped_at": trip_at or None,
        "blocked_until": blocked_until if blocked else None,
        "remaining_seconds": max(0, int(blocked_until - now_ts)) if blocked else 0,
        "window_seconds": window,
        "max_invalid": max_invalid,
        "cooldown_seconds": cooldown,
    }


def _playwright_probe_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "autoteam.playwright_probe", *args]


def _kill_subprocess_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _parse_playwright_probe_stdout(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _run_playwright_probe(*args: str, timeout_seconds: float = 30) -> dict:
    cmd = _playwright_probe_command(*args)
    env = os.environ.copy()
    env["AUTOTEAM_PROBE_MODE"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=max(1.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        _kill_subprocess_group(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        raise TimeoutError(f"Playwright probe timeout: {' '.join(args)}") from exc

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if proc.returncode != 0:
        detail = stderr or stdout or f"exit={proc.returncode}"
        raise RuntimeError(detail)

    return _parse_playwright_probe_stdout(stdout) if stdout else {}


class _TeamMemberCount(int):
    def __new__(cls, count: int, *, invites: int = 0, occupancy: int | None = None):
        obj = int.__new__(cls, count)
        obj.invites = max(0, int(invites or 0))
        obj.occupancy = int(occupancy) if occupancy is not None else int(count) + obj.invites
        return obj


def _team_member_invite_count(team_count) -> int:
    try:
        return max(0, int(getattr(team_count, "invites", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _team_member_occupancy(team_count) -> int:
    try:
        count = int(team_count)
    except (TypeError, ValueError):
        return -1
    if count < 0:
        return -1
    try:
        occupancy = int(team_count.occupancy)
    except (AttributeError, TypeError, ValueError):
        occupancy = count + _team_member_invite_count(team_count)
    return max(count, occupancy)


def _auto_check_team_member_count(timeout_seconds: float = 30, retries: int = 2) -> int:
    """Return Team member count via a killable subprocess probe; -1 means unknown."""

    for attempt in range(1, max(1, retries) + 1):
        try:
            result = _run_playwright_probe("team-member-count", timeout_seconds=timeout_seconds)
        except TimeoutError:
            if attempt < retries:
                logger.warning(
                    "[巡检] Team 人数探针超时（>%ss），准备重试第 %d/%d 次",
                    timeout_seconds,
                    attempt + 1,
                    retries,
                )
                continue
            logger.warning("[巡检] Team 人数探针超时（>%ss，已重试 %d 次），本轮视为未知", timeout_seconds, retries)
            return -1
        except Exception as exc:
            logger.warning("[巡检] Team 人数探针失败: %s", exc)
            return -1

        try:
            count = int(result.get("count", -1))
            invites = int(result.get("invites", 0) or 0)
            occupancy = int(result.get("occupancy", count + invites if count >= 0 else -1))
            return _TeamMemberCount(count, invites=invites, occupancy=occupancy)
        except Exception:
            return -1


def _collect_cpa_credential_gate() -> dict:
    """Read-only CPA provider-auth gate for auto-check decisions."""
    try:
        from autoteam.sync_targets import SYNC_TARGET_CPA, is_sync_target_enabled

        enabled = is_sync_target_enabled(SYNC_TARGET_CPA)
    except Exception as exc:
        logger.warning("[巡检] CPA 可用凭证 gate 配置检查失败: %s", exc)
        return {
            "enabled": False,
            "safe_read_only": True,
            "management_ok": False,
            "available": None,
            "total": None,
            "reason": "config_check_failed",
            "zero_available": False,
            "error": str(exc),
        }

    if not enabled:
        return {
            "enabled": False,
            "safe_read_only": True,
            "management_ok": False,
            "available": None,
            "total": None,
            "reason": "cpa_sync_disabled",
            "zero_available": False,
        }

    try:
        from autoteam.cliproxy_health import get_cliproxy_health

        health = get_cliproxy_health(cache_ttl=0.0, force_refresh=True)
    except Exception as exc:
        logger.warning("[巡检] CPA 可用凭证 gate 只读检查失败: %s", exc)
        return {
            "enabled": True,
            "safe_read_only": True,
            "management_ok": False,
            "available": None,
            "total": None,
            "reason": "health_check_exception",
            "zero_available": False,
            "error": str(exc),
        }

    management_api = health.get("management_api") if isinstance(health, dict) else {}
    provider_auth = health.get("provider_auth") if isinstance(health, dict) else {}
    management_ok = bool(isinstance(management_api, dict) and management_api.get("ok"))
    safe_read_only = bool(isinstance(health, dict) and health.get("safe_read_only", True))
    if not isinstance(provider_auth, dict):
        provider_auth = {}

    try:
        available = int(provider_auth.get("available") or 0)
    except (TypeError, ValueError):
        available = 0
    try:
        total = int(provider_auth.get("total") or 0)
    except (TypeError, ValueError):
        total = 0

    reason = str(
        provider_auth.get("reason")
        or (management_api.get("reason") if isinstance(management_api, dict) else "")
        or "unknown"
    )
    return {
        "enabled": True,
        "safe_read_only": safe_read_only,
        "management_ok": management_ok,
        "available": available,
        "total": total,
        "reason": reason,
        "zero_available": safe_read_only and management_ok and available <= 0,
    }


def _cpa_provider_auth_below_pool_target(gate: dict | None, pool_target: int) -> bool:
    if not isinstance(gate, dict):
        return False
    if not gate.get("enabled") or not gate.get("safe_read_only") or not gate.get("management_ok"):
        return False
    try:
        available = int(gate.get("available"))
        target = max(1, int(pool_target))
    except (TypeError, ValueError):
        return False
    return available < target


def _auto_check_loop():
    """后台巡检线程：定期检查额度，多个账号低于阈值时自动轮转"""
    from autoteam.accounts import STATUS_ACTIVE, is_account_disabled, load_accounts
    from autoteam.codex_auth import check_codex_quota, quota_result_quota_info

    while not _auto_check_stop.is_set():
        cfg = _auto_check_config
        logger.info(
            "[巡检] 等待 %d 分钟后执行下一轮检查（阈值: %d%%, 模式: 任意失效立即 1v1 替换）",
            cfg["interval"] // 60,
            cfg["threshold"],
        )

        # 等待 interval 秒，期间可被 restart 或 stop 唤醒
        _auto_check_restart.clear()
        if _auto_check_stop.wait(cfg["interval"]):
            break
        if _auto_check_restart.is_set():
            continue  # 配置变更，跳到下一轮重新读取配置

        if _playwright_lock.locked() or _current_task_id:
            logger.info("[巡检] 有任务正在执行，跳过本轮自动巡检昂贵探测")
            continue

        try:
            cfg = _auto_check_config  # 重新读取
            target_seats = _resolve_auto_check_target_seats(cfg)
            sub_account_target = max(0, target_seats - 1)
            log_runtime_resource_snapshot(logger, label="auto-check")
            accounts = load_accounts()
            active = [
                a
                for a in accounts
                if a["status"] == STATUS_ACTIVE
                and not _is_main_account_email(a.get("email"))
                and not is_account_disabled(a)
                and a.get("auth_file")
                and Path(a["auth_file"]).exists()
            ]
            try:
                from autoteam.manager import _replaceable_pool_blocker_reason

                replaceable_blockers = [
                    {
                        "email": a.get("email"),
                        "reason": reason,
                    }
                    for a in accounts
                    if not _is_main_account_email(a.get("email"))
                    and not is_account_disabled(a)
                    and (reason := _replaceable_pool_blocker_reason(a))
                ]
            except Exception as exc:
                logger.warning("[巡检] 本地占席 blocker 分类失败: %s", exc)
                replaceable_blockers = []
            cpa_credential_gate = _collect_cpa_credential_gate()
            if cpa_credential_gate.get("enabled"):
                logger.info(
                    "[巡检] CPA 可用凭证 gate: management_ok=%s available=%s/%s reason=%s safe_read_only=%s",
                    cpa_credential_gate.get("management_ok"),
                    cpa_credential_gate.get("available"),
                    cpa_credential_gate.get("total"),
                    cpa_credential_gate.get("reason"),
                    cpa_credential_gate.get("safe_read_only"),
                )

            provider_auth_below_target = _cpa_provider_auth_below_pool_target(cpa_credential_gate, sub_account_target)
            actual_team_count = _auto_check_team_member_count(timeout_seconds=30, retries=2)
            actual_invite_count = _team_member_invite_count(actual_team_count)
            actual_team_occupancy = _team_member_occupancy(actual_team_count)
            burn_guard = _auto_check_burn_guard_status(accounts)
            auth_repair_guard = _auto_check_auth_repair_guard_status()
            if burn_guard.get("blocked"):
                logger.warning(
                    "[巡检] 烧号熔断已生效: 最近 %d 秒内 %d 个子号耗尽(%s)，"
                    "暂停自动补位/替换 %d 秒；手动操作不受影响。"
                    "请检查任务并发、reasoning effort 和 service_tier/fast 设置",
                    burn_guard.get("window_seconds"),
                    burn_guard.get("count"),
                    ", ".join(burn_guard.get("emails") or []),
                    burn_guard.get("remaining_seconds"),
                )
            if auth_repair_guard.get("blocked"):
                logger.warning(
                    "[巡检] 认证修复熔断已生效: 最近 %d 秒内 %d 个子号因 auth_repair 临时异常失效(%s)，"
                    "暂停自动补位/替换 %d 秒；手动操作不受影响。请检查代理、Playwright/Xvfb、OpenAI 页面状态",
                    auth_repair_guard.get("window_seconds"),
                    auth_repair_guard.get("count"),
                    ", ".join(auth_repair_guard.get("emails") or []),
                    auth_repair_guard.get("remaining_seconds"),
                )

            if actual_team_occupancy > target_seats:
                if not _playwright_lock.acquire(blocking=False):
                    logger.info("[巡检] Team 远端占用超出目标，但有任务在跑，本轮跳过自动清理")
                    continue
                _playwright_lock.release()

                logger.warning(
                    "[巡检] Team 远端占用超出目标: members=%d invites=%d total=%d/%d，触发自动清理",
                    int(actual_team_count),
                    actual_invite_count,
                    actual_team_occupancy,
                    target_seats,
                )
                from autoteam.manager import cmd_cleanup

                try:
                    _start_task(
                        "auto-cleanup",
                        cmd_cleanup,
                        {
                            "max_seats": target_seats,
                            "trigger": "auto-check",
                            "team_count": int(actual_team_count),
                            "invite_count": actual_invite_count,
                            "team_occupancy": actual_team_occupancy,
                        },
                        target_seats,
                    )
                except Exception as e:
                    logger.error("[巡检] 自动清理失败: %s", e)
                continue

            # Watchdog:active 账号数 < 子号目标时自动补位。
            # 之前的 `if not active: continue` 在 active 全 kick 进 standby
            # 之后会让 Team 永远萎缩。但触发频率必须节制 —— OpenAI 对短时间内反复
            # invite/kick 同一批子号会 revoke token(token_revoked 错误),所以加
            # 30 分钟冷却,避免巡检每 5 分钟无脑触发 cmd_rotate 把账号全洗成废号。
            global _auto_fill_last_trigger_ts
            if len(active) < sub_account_target:
                now_ts = time.time()
                should_start_auto_fill = False
                cooldown_remaining = (_auto_fill_last_trigger_ts + _AUTO_FILL_COOLDOWN_SECONDS) - now_ts
                if cooldown_remaining > 0:
                    logger.info(
                        "[巡检] active=%d < %d,但 auto-fill 冷却中(还剩 %d 分钟)",
                        len(active),
                        sub_account_target,
                        int(cooldown_remaining / 60),
                    )
                    # 冷却期内仍然继续做"低额度替换"(下面的 low_accounts 逻辑),
                    # 只是不触发全量 cmd_rotate。例外:Team 真实人数不足时必须继续补位,
                    # 否则 cooldown 会把实际席位缺口拖到下一轮甚至 4h backoff 之后。
                    if actual_team_count >= target_seats:
                        if replaceable_blockers:
                            logger.warning(
                                "[巡检] auto-fill 冷却中但 Team 已满且存在 %d 个不可用占席子号，继续触发轮转: %s",
                                len(replaceable_blockers),
                                ", ".join(
                                    f"{item['email']}({item['reason']})" for item in replaceable_blockers[:5]
                                ),
                            )
                            should_start_auto_fill = True
                        elif cpa_credential_gate.get("zero_available"):
                            logger.warning(
                                "[巡检] auto-fill 冷却中但 Team 已满、本地 active=%d/%d，且 CPA 可用凭证为 0/%s，继续补位",
                                len(active),
                                sub_account_target,
                                cpa_credential_gate.get("total"),
                            )
                            should_start_auto_fill = True
                        elif provider_auth_below_target:
                            logger.warning(
                                "[巡检] auto-fill 冷却中但 CPA/CLIProxy provider-auth 可用凭证低水位: "
                                "available=%s/%d local_active=%d/%d，继续补位/同步",
                                cpa_credential_gate.get("available"),
                                sub_account_target,
                                len(active),
                                sub_account_target,
                            )
                            should_start_auto_fill = True
                        else:
                            logger.info(
                                "[巡检] auto-fill 冷却中且 Team 实际成员数=%d 已满；本轮只保留低额度替换检查",
                                actual_team_count,
                            )
                    elif actual_team_count >= 0:
                        logger.info(
                            "[巡检] auto-fill 冷却中但 Team 实际成员不足（%d/%d），继续补位",
                            actual_team_count,
                            target_seats,
                        )
                        should_start_auto_fill = True
                    else:
                        logger.info("[巡检] auto-fill 冷却中且 Team 实际成员数未知；本轮不触发全量补位")
                else:
                    # Round 11 — OAuth 连续失败 backoff:
                    # 最近 2 小时内 master workspace 累积 ≥3 个 auth_invalid 账号 → fill 已稳定失败,
                    # 延长有效冷却到 4 小时,避免每 30 分钟无脑循环浪费 cloudmail 邮箱 + 累积僵尸账号。
                    # 触发条件用 status=auth_invalid + workspace_account_id=master 简单可靠,
                    # 不依赖具体 OAuth 失败原因(根因可能是 ChatGPT consent 页面变化或 master 订阅问题)。
                    backoff_triggered = False
                    try:
                        from autoteam.accounts import STATUS_AUTH_INVALID
                        from autoteam.admin_state import get_chatgpt_account_id

                        master_aid = get_chatgpt_account_id() or ""
                        recent_window = 2 * 3600  # 2 小时
                        recent_failures = [
                            a for a in accounts
                            if a.get("status") == STATUS_AUTH_INVALID
                            and (a.get("workspace_account_id") or "") == master_aid
                            and (a.get("created_at") or 0) >= now_ts - recent_window
                        ]
                        if len(recent_failures) >= 3:
                            # 强制延长冷却,记 last_trigger_ts 让下次巡检也走 cooldown 分支
                            _auto_fill_last_trigger_ts = now_ts - _AUTO_FILL_COOLDOWN_SECONDS + 4 * 3600
                            logger.warning(
                                "[巡检] active=%d < %d 但近 2h 累积 %d 个 OAuth 失败账号 → "
                                "backoff 生效,延长冷却到 4h(避免无谓循环)。"
                                "请检查 codex_auth consent 页面或 master 订阅",
                                len(active),
                                sub_account_target,
                                len(recent_failures),
                            )
                            backoff_triggered = True
                    except Exception as exc:
                        logger.warning("[巡检] OAuth backoff 检查异常: %s,按原逻辑继续", exc)

                    if backoff_triggered:
                        continue

                    if actual_team_count >= target_seats:
                        if replaceable_blockers:
                            logger.warning(
                                "[巡检] Team 已满但本地 active=%d/%d 且存在不可用占席子号，触发 auto-fill 修复: %s",
                                len(active),
                                sub_account_target,
                                ", ".join(
                                    f"{item['email']}({item['reason']})" for item in replaceable_blockers[:5]
                                ),
                            )
                        elif cpa_credential_gate.get("zero_available"):
                            logger.warning(
                                "[巡检] Team 已满但本地 active=%d/%d，且 CPA 可用凭证为 0/%s，触发 auto-fill 补位",
                                len(active),
                                sub_account_target,
                                cpa_credential_gate.get("total"),
                            )
                        elif provider_auth_below_target:
                            logger.warning(
                                "[巡检] Team 已满且 CPA/CLIProxy provider-auth 可用凭证低水位: "
                                "available=%s/%d local_active=%d/%d，触发 auto-fill 补位/同步",
                                cpa_credential_gate.get("available"),
                                sub_account_target,
                                len(active),
                                sub_account_target,
                            )
                        else:
                            logger.info(
                                "[巡检] 本地 active=%d < %d,但 Team 实际成员数=%d 已满；先跳过 auto-fill,等待同步/对账稳定",
                                len(active),
                                sub_account_target,
                                actual_team_count,
                            )
                            continue
                    should_start_auto_fill = True

                if should_start_auto_fill:
                    if auth_repair_guard.get("blocked"):
                        logger.warning(
                            "[巡检] active=%d < %d 但认证修复熔断中，本轮跳过 auto-fill",
                            len(active),
                            sub_account_target,
                        )
                        continue
                    if burn_guard.get("blocked"):
                        logger.warning(
                            "[巡检] active=%d < %d 但烧号熔断中，本轮跳过 auto-fill",
                            len(active),
                            sub_account_target,
                        )
                        continue
                    if not _playwright_lock.acquire(blocking=False):
                        logger.info(
                            "[巡检] active=%d < %d 但有任务在跑,本轮先跳过自动补位",
                            len(active),
                            sub_account_target,
                        )
                        continue
                    _playwright_lock.release()
                    logger.warning(
                        "[巡检] active 账号 %d < %d,触发 auto-fill(cmd_rotate 全流程补位)",
                        len(active),
                        sub_account_target,
                    )
                    from autoteam.manager import cmd_rotate

                    try:
                        task_params = {"target_seats": target_seats}
                        if cpa_credential_gate.get("enabled"):
                            try:
                                provider_available = int(cpa_credential_gate.get("available") or 0)
                            except (TypeError, ValueError):
                                provider_available = 0
                            task_params.update(
                                {
                                    "trigger": "auto-check",
                                    "shortage": max(
                                        0,
                                        sub_account_target - min(len(active), provider_available),
                                    ),
                                    "cpa_credential_gate": cpa_credential_gate,
                                    "provider_auth_below_target": provider_auth_below_target,
                                    "provider_auth_available": cpa_credential_gate.get("available"),
                                    "provider_auth_target": sub_account_target,
                                }
                            )
                        _start_task(
                            "auto-fill",
                            cmd_rotate,
                            task_params,
                            target_seats,
                            background_post_sync=True,
                        )
                        _auto_fill_last_trigger_ts = now_ts
                    except Exception as e:
                        logger.error("[巡检] auto-fill 启动失败: %s", e)
                    # 触发后本轮不再做"低额度替换",免得跟 cmd_rotate 抢锁
                    continue

            if not active:
                continue

            low_accounts = []
            for acc in active:
                try:
                    auth_data = json.loads(read_text(Path(acc["auth_file"])))
                    access_token = auth_data.get("access_token")
                    if not access_token:
                        continue
                    status, info = check_codex_quota(access_token)
                    if status == "ok" and isinstance(info, dict):
                        remaining = 100 - info.get("primary_pct", 0)
                        if remaining < cfg["threshold"]:
                            low_accounts.append((acc["email"], remaining, info))
                    elif status == "exhausted":
                        quota_info = quota_result_quota_info(info) if isinstance(info, dict) else None
                        low_accounts.append((acc["email"], 0, quota_info or {}))
                except Exception:
                    pass

            if low_accounts:
                logger.info(
                    "[巡检] %d 个账号额度不足: %s",
                    len(low_accounts),
                    ", ".join(f"{e}({r}%)" for e, r, _quota in low_accounts),
                )

                # 有任务在跑则本轮跳过(下轮再替换,避免重复 kick)
                if not _playwright_lock.acquire(blocking=False):
                    logger.info("[巡检] 有任务正在执行，本轮跳过即时替换")
                    continue
                _playwright_lock.release()

                # 先标记 exhausted,cmd_check 入口的对账在此之后再看到就会补 kick(双保险)。
                # 必须同时写 quota_resets_at —— 否则 get_standby_accounts() 看到 None 就默认
                # _quota_recovered=True,导致后续 rotate/replace 立刻把这个 0% 账号当可复用号
                # 反复 reinvite 进 Team,席位来回洗同一批耗尽账号永远不换新鲜的。
                # 阈值默认 5h(18000s),与 check_codex_quota 无返回 resets_at 时的 fallback 一致。
                from autoteam.accounts import STATUS_EXHAUSTED, update_account
                from autoteam.manager import _quota_exhaustion_fields

                now_ts = time.time()
                emails_to_replace = []
                for email, remaining, quota_info in low_accounts:
                    logger.info("[巡检] %s 剩余 %d%%，立即替换", email, remaining)
                    resets_at = None
                    if isinstance(quota_info, dict):
                        resets_at = quota_info.get("primary_resets_at")
                    update_account(
                        email,
                        status=STATUS_EXHAUSTED,
                        **_quota_exhaustion_fields(
                            quota_info,
                            resets_at or now_ts + 18000,
                            "auto_check_low_quota",
                            now_ts=now_ts,
                        ),
                    )
                    emails_to_replace.append(email)

                # 失效一个立即轮换一个:逐个 kick+补一个,不等凑 min_low 也不走全量 cmd_rotate。
                # min_low 字段保留作兼容(当前不参与判断),前端可继续配置但无语义效果。
                logger.info("[巡检] 准备即时替换 (%d 个)...", len(emails_to_replace))
                recent_burn_emails = set(burn_guard.get("recent_window_emails") or [])
                next_burn_emails = recent_burn_emails | set(emails_to_replace)
                if auth_repair_guard.get("blocked"):
                    logger.warning(
                        "[巡检] 认证修复熔断阻止 auto-replace: 本轮只标记耗尽，不自动补新号"
                    )
                    continue
                if burn_guard.get("blocked") or len(next_burn_emails) >= int(_DEFAULT_BURN_GUARD_MAX_EXHAUSTED):
                    logger.warning(
                        "[巡检] 烧号熔断阻止 auto-replace: 最近窗口内将达到 %d/%d 个耗尽子号(%s)，"
                        "本轮只标记耗尽，不自动补新号",
                        len(next_burn_emails),
                        int(_DEFAULT_BURN_GUARD_MAX_EXHAUSTED),
                        ", ".join(sorted(next_burn_emails)),
                    )
                    continue
                from autoteam.manager import cmd_replace_batch

                try:
                    _start_task(
                        "auto-replace",
                        cmd_replace_batch,
                        {"emails": emails_to_replace, "trigger": "auto-check"},
                        emails_to_replace,
                        "auto-check",
                    )
                except Exception as e:
                    logger.error("[巡检] 即时替换启动失败: %s", e)
            elif provider_auth_below_target and actual_team_count >= target_seats:
                try:
                    provider_available = int(cpa_credential_gate.get("available") or 0)
                except (TypeError, ValueError):
                    provider_available = 0
                shortage = max(1, sub_account_target - min(len(active), provider_available))

                if not _playwright_lock.acquire(blocking=False):
                    logger.info("[巡检] CPA/CLIProxy provider-auth 低水位，但有任务在跑，本轮跳过预防性轮转")
                    continue
                _playwright_lock.release()
                if auth_repair_guard.get("blocked"):
                    logger.warning("[巡检] CPA/CLIProxy provider-auth 低水位，但认证修复熔断中，本轮跳过 auto-rotate")
                    continue
                if burn_guard.get("blocked"):
                    logger.warning("[巡检] CPA/CLIProxy provider-auth 低水位，但烧号熔断中，本轮跳过 auto-rotate")
                    continue

                logger.warning(
                    "[巡检] CPA/CLIProxy provider-auth 可用凭证低水位: "
                    "available=%d/%d local_active=%d/%d; trigger preventive auto-rotate/sync",
                    provider_available,
                    sub_account_target,
                    len(active),
                    sub_account_target,
                )
                from autoteam.manager import cmd_rotate

                try:
                    _start_task(
                        "auto-rotate",
                        cmd_rotate,
                        {
                            "target": target_seats,
                            "trigger": "auto-check",
                            "shortage": shortage,
                            "low_accounts": 0,
                            "cpa_credential_gate": cpa_credential_gate,
                            "provider_auth_below_target": True,
                            "provider_auth_available": cpa_credential_gate.get("available"),
                            "provider_auth_target": sub_account_target,
                        },
                        target_seats,
                        background_post_sync=True,
                    )
                except Exception as e:
                    logger.error("[巡检] provider-auth 低水位自动轮转失败: %s", e)
            elif provider_auth_below_target:
                logger.info(
                    "[巡检] CPA/CLIProxy provider-auth 低水位，但 Team 实际成员数=%s 未满或未知，本轮不做预防性轮转",
                    actual_team_count if actual_team_count >= 0 else "unknown",
                )
            else:
                logger.info("[巡检] 额度正常，无需替换")

        except Exception as e:
            logger.error("[巡检] 巡检异常: %s", e)

        # Round 9 RT-2 — 后台巡检每 interval 跑一次 retroactive(走 5min cache,失败 warning)。
        # spec/shared/master-subscription-health.md v1.1 §11.3。
        try:
            from autoteam.master_health import _apply_master_degraded_classification

            retro = _apply_master_degraded_classification()
            if retro and (retro.get("marked_grace") or retro.get("marked_standby") or retro.get("reverted_active")):
                logger.info(
                    "[巡检] retroactive: GRACE %d / STANDBY %d / 撤回 ACTIVE %d",
                    len(retro.get("marked_grace") or []),
                    len(retro.get("marked_standby") or []),
                    len(retro.get("reverted_active") or []),
                )
        except Exception as exc:
            logger.warning("[巡检] retroactive helper 异常: %s", exc)


class AutoCheckConfig(BaseModel):
    interval: int = 300  # 巡检间隔（秒）
    target_seats: int = 3  # 自动巡检目标 Team seat 数，最多 1 母 + 2 子
    threshold: int = 10  # 额度阈值（%）
    min_low: int = 2  # 触发轮转的最少账号数


@app.get("/api/config/auto-check")
def get_auto_check_config():
    """获取巡检配置"""
    return _auto_check_config.copy()


@app.put("/api/config/auto-check")
def set_auto_check_config(cfg: AutoCheckConfig):
    """修改巡检配置（运行时生效）"""
    _auto_check_config["interval"] = max(60, cfg.interval)  # 最少 1 分钟
    _auto_check_config["target_seats"] = max(1, min(3, cfg.target_seats))
    _auto_check_config["threshold"] = max(1, min(100, cfg.threshold))
    _auto_check_config["min_low"] = max(1, cfg.min_low)
    _auto_check_restart.set()  # 唤醒巡检线程，立即应用新配置
    logger.info(
        "[巡检] 配置已更新: 间隔=%ds 目标 seat=%d 阈值=%d%%（min_low 已废弃,任意失效立即 1v1 替换）",
        _auto_check_config["interval"],
        _auto_check_config["target_seats"],
        _auto_check_config["threshold"],
    )
    return _auto_check_config.copy()


# Round 7 P2.4 — startup/shutdown 已迁移到顶部 app_lifespan,这里不再重复挂 handler。


# ---------------------------------------------------------------------------
# 前端静态文件
# ---------------------------------------------------------------------------

DIST_DIR = Path(__file__).parent / "web" / "dist"

if DIST_DIR.exists():
    # Vite 构建的 assets 目录
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        """兜底路由：serve 前端 SPA"""
        file = DIST_DIR / path
        if file.is_file() and ".." not in path:
            return FileResponse(str(file))
        return FileResponse(str(DIST_DIR / "index.html"))


class _QuietAccessLog(logging.Filter):
    """过滤前端轮询产生的高频访问日志"""

    _quiet_paths = (
        "/api/status",
        "/api/tasks",
        "/api/config/auto-check",
        "/api/admin/status",
        "/api/main-codex/status",
        "/api/manual-account/status",
        "/api/auth/check",
        "/api/setup/status",
    )

    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self._quiet_paths)


def start_server(host: str = "0.0.0.0", port: int = 8787):
    """启动 API 服务器"""
    import uvicorn

    # 过滤轮询日志，避免刷屏
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())
    # 首次启动检查配置
    from autoteam.setup_wizard import check_and_setup

    check_and_setup(interactive=True)

    # 重新读取 API_KEY（可能刚刚被向导写入）
    global API_KEY
    from autoteam.config import API_KEY as _fresh_key

    API_KEY = _fresh_key or os.environ.get("API_KEY", "")
    if API_KEY:
        logger.info("[API] API Key 鉴权已启用")
    else:
        logger.warning("[API] 未设置 API_KEY，所有接口无需认证")
    logger.info("[API] 启动 AutoTeam API 服务器 http://%s:%d", host, port)
    if DIST_DIR.exists():
        logger.info("[API] 前端面板 http://%s:%d", host, port)
    logger.info("[API] API 文档 http://%s:%d/docs", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
