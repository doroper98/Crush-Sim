"""UI_002 §2.7 - the single error contract for every UI route.

Every 4xx/5xx body the server produces has the same shape::

    {"code", "message", "severity", "field_path", "node_id", "actions", "detail"}

``code`` is stable and machine-readable, ``message`` is the Korean sentence the
browser shows, ``detail`` carries the raw exception text for the diagnostics
panel. Routes raise :class:`UiError` (or a crushsim exception, which
:func:`from_exception` maps); they never build an ad-hoc
``HTTPException(detail=str)`` again - UI_001 §14.4 forbids showing a bare
English exception string, and a per-route message cannot be translated or
acted on by the front-end.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import (
    ConfigError,
    CrushSimError,
    GateFailure,
    GeometryError,
    MeshingError,
    OptionalDependencyError,
)

Severity = str  # "block" | "review" | "info"

#: crushsim exception -> stable error code (UI_002 §2.7). Order matters:
#: OptionalDependencyError is checked before the geometry/mesh errors because a
#: missing CAD backend is an environment problem, not a bad model.
EXCEPTION_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (OptionalDependencyError, "CAD_BACKEND_MISSING"),
    (ConfigError, "INVALID_VALUE"),
    (GateFailure, "GATE_FAILED"),
    (GeometryError, "ASSET_UNSUPPORTED"),
    (MeshingError, "MESH_FAILED"),
)

#: Default Korean sentence + HTTP status + offered actions per code. A code
#: missing here still works (the message the raiser passes is used); the table
#: exists so the same situation always reads the same way on screen.
CODE_TABLE: dict[str, dict[str, Any]] = {
    "NOT_FOUND": {
        "status": 404,
        "severity": "block",
        "message": "요청한 자원을 찾을 수 없습니다.",
        "actions": [],
    },
    "INVALID_VALUE": {
        "status": 422,
        "severity": "block",
        "message": "입력값이 올바르지 않습니다.",
        "actions": ["fix_value"],
    },
    "GATE_FAILED": {
        "status": 409,
        "severity": "block",
        "message": "품질 게이트를 통과하지 못했습니다.",
        "actions": ["show_diagnostics"],
    },
    "ASSET_UNSUPPORTED": {
        "status": 422,
        "severity": "block",
        "message": "이 형상 파일은 현재 지원하지 않습니다.",
        "actions": ["choose_other_file"],
    },
    "MESH_FAILED": {
        "status": 409,
        "severity": "block",
        "message": "메쉬를 만들지 못했습니다.",
        "actions": ["show_diagnostics"],
    },
    "CAD_BACKEND_MISSING": {
        "status": 501,
        "severity": "block",
        "message": "이 서버에는 STEP 처리 기능(CAD 백엔드)이 설치되어 있지 않습니다.",
        "actions": ["show_install_help"],
    },
    "FILE_TOO_LARGE": {
        "status": 413,
        "severity": "block",
        "message": "파일이 업로드 한도를 넘습니다.",
        "actions": ["choose_other_file"],
    },
    "UNSUPPORTED_FILE_TYPE": {
        "status": 422,
        "severity": "block",
        "message": "STEP 파일(.stp, .step)만 가져올 수 있습니다.",
        "actions": ["choose_other_file"],
    },
    "ASSET_NOT_READY": {
        "status": 409,
        "severity": "block",
        "message": "형상 가져오기가 아직 끝나지 않았습니다.",
        "actions": ["wait_and_retry"],
    },
    "STALE_PREFLIGHT": {
        "status": 409,
        "severity": "block",
        "message": "입력이 바뀌었습니다. 사전 검사를 다시 실행하세요.",
        "actions": ["rerun_preflight"],
    },
    "REVISION_CONFLICT": {
        "status": 409,
        "severity": "block",
        "message": "다른 창에서 수정된 내용이 있습니다.",
        "actions": ["show_latest", "save_as_copy"],
    },
    "PREFLIGHT_NOT_RUNNABLE": {
        "status": 409,
        "severity": "block",
        "message": "차단 항목이 남아 있어 실행할 수 없습니다.",
        "actions": ["show_checks"],
    },
    "GRAPH_INCOMPLETE": {
        "status": 422,
        "severity": "block",
        "message": "솔버 노드에 메쉬·재료·하중이 모두 연결되어야 합니다.",
        "actions": ["show_node"],
    },
    "ALREADY_RUNNING": {
        "status": 409,
        "severity": "block",
        "message": "같은 케이스가 이미 실행 중이거나 대기 중입니다.",
        "actions": ["show_execution"],
    },
    "UNPARSEABLE": {
        "status": 409,
        "severity": "block",
        "message": "파일을 읽을 수 없습니다.",
        "actions": ["show_detail"],
    },
    "INTERNAL": {
        "status": 500,
        "severity": "block",
        "message": "서버에서 처리하지 못했습니다.",
        "actions": ["show_detail"],
    },
}


class UiError(Exception):
    """An error that renders as the UI_002 §2.7 body."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        severity: Severity | None = None,
        field_path: str | None = None,
        node_id: str | None = None,
        actions: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        row = CODE_TABLE.get(code, {})
        self.code = code
        self.message = message or str(row.get("message", code))
        self.status = int(status if status is not None else row.get("status", 422))
        self.severity: Severity = severity or str(row.get("severity", "block"))
        self.field_path = field_path
        self.node_id = node_id
        self.actions = list(actions if actions is not None else row.get("actions", []))
        self.detail = detail
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        """The response body - exactly the seven keys of the contract."""
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "field_path": self.field_path,
            "node_id": self.node_id,
            "actions": self.actions,
            "detail": self.detail,
        }


def code_for_exception(exc: BaseException) -> str:
    """The stable code for a crushsim exception (UI_002 §2.7 mapping)."""
    for kind, code in EXCEPTION_CODES:
        if isinstance(exc, kind):
            return code
    return "INTERNAL"


def from_exception(
    exc: BaseException,
    *,
    message: str | None = None,
    field_path: str | None = None,
    node_id: str | None = None,
    status: int | None = None,
) -> UiError:
    """Wrap a crushsim exception in the contract, keeping its text as ``detail``."""
    return UiError(
        code_for_exception(exc),
        message,
        status=status,
        field_path=field_path,
        node_id=node_id,
        detail=str(exc),
    )


def not_found(message: str, *, detail: str | None = None) -> UiError:
    """A 404 in the contract's shape."""
    return UiError("NOT_FOUND", message, detail=detail)


def install_error_handlers(app: FastAPI) -> None:
    """Route every error - ours, FastAPI's, and the unexpected - through the contract."""

    @app.exception_handler(UiError)
    async def _ui_error(_request: Any, exc: UiError) -> JSONResponse:  # noqa: RUF029
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(CrushSimError)
    async def _crushsim_error(_request: Any, exc: CrushSimError) -> JSONResponse:  # noqa: RUF029
        wrapped = from_exception(exc)
        return JSONResponse(status_code=wrapped.status, content=wrapped.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:  # noqa: RUF029
        first = (exc.errors() or [{}])[0]
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        wrapped = UiError(
            "INVALID_VALUE",
            "요청 형식이 올바르지 않습니다.",
            field_path=loc or None,
            detail=str(first.get("msg", "")) or str(exc),
        )
        return JSONResponse(status_code=422, content=wrapped.body())

    @app.exception_handler(Exception)
    async def _unexpected(_request: Any, exc: Exception) -> JSONResponse:  # noqa: RUF029
        # Without this, an unexpected error escapes as Starlette's plain-text
        # 500 and the browser gets no code, no severity and no Korean
        # sentence - the one shape the front-end is built to read.
        wrapped = UiError("INTERNAL", detail=f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content=wrapped.body())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Any, exc: StarletteHTTPException) -> JSONResponse:  # noqa: RUF029
        # FastAPI's own 404/405 (and any legacy raise) still have to speak the
        # contract, so they are mapped by status instead of leaking {"detail"}.
        by_status = {
            404: "NOT_FOUND",
            405: "NOT_FOUND",
            409: "UNPARSEABLE",
            413: "FILE_TOO_LARGE",
            422: "INVALID_VALUE",
        }
        code = by_status.get(exc.status_code, "INTERNAL")
        detail = exc.detail if isinstance(exc.detail, str) else None
        wrapped = UiError(code, status=exc.status_code, detail=detail)
        return JSONResponse(status_code=exc.status_code, content=wrapped.body())


__all__ = [
    "CODE_TABLE",
    "EXCEPTION_CODES",
    "UiError",
    "code_for_exception",
    "from_exception",
    "install_error_handlers",
    "not_found",
]
