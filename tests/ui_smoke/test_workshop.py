"""UI_002 §3 WP2 - 작업실 브라우저 스모크 테스트 (U01·U02·U07·U12·U16·U23·U40).

이 파일은 진짜 브라우저에서 화면을 여는 유일한 검사다. 실제 솔버는 절대 돌지
않는다: 큐의 command_builder를 잠자는 프로세스로 바꿔 등록까지만 확인한다
(tests/test_ui.py와 같은 방식).

playwright가 없으면 통째로 skip한다. 크로미움은 /opt/pw-browsers에 있고,
설치된 playwright 파이썬 패키지의 기대 빌드 번호와 다를 수 있어 실행 파일을
직접 지정한다(`playwright install`은 이 환경에서 금지).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("playwright")
pytest.importorskip("fastapi")

from playwright.sync_api import sync_playwright  # noqa: E402

from crushsim.ui.server import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CHROMIUM = Path("/opt/pw-browsers/chromium")
SCREENSHOTS = REPO / "docs" / "ui_screens"

#: 큐가 실제로 실행할 명령. 솔버 대신 잠들기만 한다.
_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]

_CASE_YAML = {
    "name": "ui_case",
    "load_case": "LC-1",
    "geometry": {"kind": "parametric_can", "radius": 33.0, "height": 115.0, "thickness": 0.1},
    "material": {"key": "aluminum_3003"},
    "loading": {"tool": "platen", "stroke": 40.0},
    "output": {"dir": "runs/ui_case"},
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def ui_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """test_ui.py의 ui_root와 같되, 작업실이 읽는 것까지 복사한다."""
    root = tmp_path_factory.mktemp("workshop")
    (root / "configs").mkdir()
    shutil.copytree(REPO / "configs" / "materials", root / "configs" / "materials")
    shutil.copytree(REPO / "configs" / "graphs", root / "configs" / "graphs")
    (root / "configs" / "cases").mkdir()
    (root / "configs" / "cases" / "ui_case.yaml").write_text(
        yaml.safe_dump(_CASE_YAML), encoding="utf-8"
    )
    for name in ("solver.yaml",):
        source = REPO / "configs" / name
        if source.is_file():
            shutil.copy(source, root / "configs" / name)
    (root / "examples" / "step").mkdir(parents=True)
    shutil.copy(REPO / "examples" / "step" / "cylin_can.stp", root / "examples" / "step")
    (root / "runs").mkdir()
    return root


@pytest.fixture(scope="module")
def server(ui_root: Path):
    """앱을 uvicorn으로 빈 포트에 띄우고 base URL을 준다."""
    import uvicorn  # noqa: PLC0415

    app = create_app(ui_root)
    app.state.executions.command_builder = lambda exec_id: _SLEEP
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    assert server.started, "uvicorn이 시작되지 않았습니다"
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(timeout=10)
    # 잠자는 가짜 실행이 남지 않게 정리한다.
    for exec_id in list(app.state.executions._processes):  # noqa: SLF001 - 테스트 정리
        try:
            app.state.executions.cancel(exec_id)
        except Exception:  # noqa: BLE001 - 정리 실패가 테스트를 깨뜨리면 안 된다
            pass


@pytest.fixture(scope="module")
def browser():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    with sync_playwright() as pw:
        kwargs = {"args": ["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--no-sandbox"]}
        if CHROMIUM.exists():
            kwargs["executable_path"] = str(CHROMIUM)
        try:
            instance = pw.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 브라우저가 없으면 검사를 건너뛴다
            pytest.skip(f"chromium을 실행할 수 없습니다: {exc}")
        yield instance
        instance.close()


@pytest.fixture()
def page(browser, server):
    base, _app = server
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base + "/")
    page.wait_for_function("() => window.__firstPaintMs !== undefined", timeout=20000)
    yield page
    assert not errors, f"브라우저 콘솔 오류: {errors}"
    context.close()


def _shot(page, name: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS / name))


def _start_parametric(page, kind_label: str = "각형 캔") -> None:
    page.click("#startParametric")
    page.click(f"dialog.modal button:has-text('{kind_label}')")
    page.wait_for_selector("#startArea[hidden]", state="attached")


# ---------------------------------------------------------------- U01


def test_u01_start_area_offers_both_entries(page) -> None:
    """처음 방문: STEP·치수 진입이 보이고 빈 그래프 편집을 요구하지 않는다."""
    assert page.is_visible("#startArea")
    assert page.is_visible("#startParametric")
    assert page.is_visible("#startStep")
    assert page.is_visible("#startOpenGraph")
    assert page.is_visible("#startExample")
    assert "어떤 셀을 검토할까요" in page.inner_text("#startArea")
    # 시작 영역이 그래프 편집을 가리므로 빈 캔버스를 만질 필요가 없다.
    assert page.eval_on_selector("#graphCanvas", "el => el.querySelectorAll('.gnode').length") == 0
    _shot(page, "01_start.png")


# ---------------------------------------------------------------- U02


def test_u02_step_import_shows_units_and_parts(page, server) -> None:
    """지원 STEP을 파일 선택으로 가져오면 서버 경로 입력 없이 확인 카드가 뜬다."""
    _, app = server
    page.click("#startStep")
    page.set_input_files("#stepFile", str(REPO / "examples" / "step" / "cylin_can.stp"))
    try:
        page.wait_for_selector("text=형상 확인 카드", timeout=180_000)
    except Exception:  # noqa: BLE001 - OCP가 없는 환경에서는 가져오기가 실패한다
        status = page.inner_text("#importStatus") if page.query_selector("#importStatus") else ""
        if "CAD" in status or "지원" in status or "읽지" in status:
            pytest.skip(f"이 환경에서는 STEP을 읽을 수 없습니다: {status}")
        raise
    card = page.inner_text("#inspector")
    assert "단위" in card
    assert "외형 치수" in card
    assert "솔리드" in card
    assert "cylin_can.stp" in card
    _shot(page, "02_step_import.png")


# ---------------------------------------------------------------- U07


def test_u07_click_path_from_dimensions_to_queued_execution(page, server) -> None:
    """치수로 시작 → 벤트 개방 → 기본값 → 실행 확인 → 제출 → 실행 행."""
    _, app = server
    _start_parametric(page)
    # 2단계: 목적 카드
    page.click("button.step-btn:has-text('알고 싶은 것')")
    page.wait_for_selector(".purpose-card")
    assert "이번 해석에서 무엇을 알고 싶나요" in page.inner_text("#inspector")
    page.click(".purpose-card:has-text('벤트 개방')")
    # 3단계: 기본값 그대로 두고 필수 확인만 누른다
    page.wait_for_selector("#confirmAll")
    page.click("#confirmAll")
    # 4단계: 사전 검사 결과가 화면에 나온다
    page.click("button.step-btn:has-text('실행 확인')")
    page.wait_for_selector("#runEstimate:has-text('건')", timeout=30_000)
    page.wait_for_function(
        "() => !document.getElementById('btnStart').disabled", timeout=30_000
    )
    assert "사전 검사" in page.inner_text("#inspector")
    _shot(page, "03_review.png")
    page.click("#btnStart")
    page.wait_for_selector(".run-row", timeout=30_000)
    rows = page.inner_text("#runPanel")
    assert "대기" in rows or "해석 중" in rows or "메쉬" in rows
    _shot(page, "04_running.png")
    listed = app.state.executions.list()["items"]
    assert listed, "실행이 큐에 등록되지 않았습니다"


# ---------------------------------------------------------------- U12


def test_u12_incompatible_connection_is_refused_with_a_reason(page) -> None:
    """잘못된 포트 연결은 이유와 함께 거부되고 기존 연결은 남는다."""
    _start_parametric(page, "원통형 캔")
    page.click("#btnToggleSettings")  # 해석 설정 그룹을 펼쳐 메쉬·솔버 노드를 본다
    page.wait_for_selector(".gnode")
    before = page.evaluate("() => App.graph.edges.length")
    # 재료 노드의 출력(mat)을 메쉬 노드의 형상 입력(geom)에 연결해 본다.
    material_id = page.evaluate("() => App.graph.byType('material')[0].id")
    mesh_id = page.evaluate("() => App.graph.byType('mesh')[0].id")
    page.click(f"#gnode-{material_id} .port.out")
    page.click(f"#gnode-{mesh_id} .port.in[data-port='geom']")
    message = page.inner_text("#graphMsg")
    assert "포트 종류가 다릅니다" in message
    assert page.evaluate("() => App.graph.edges.length") == before
    _shot(page, "05_port_refused.png")


# ---------------------------------------------------------------- U16


def test_u16_over_budget_estimate_warns_and_offers_the_preset(page) -> None:
    """추정 상한이 30분을 넘으면 경고와 '30분 목표 설정 보기'가 나온다."""
    _start_parametric(page)
    page.click("button.step-btn:has-text('알고 싶은 것')")
    page.click(".purpose-card:has-text('벤트 개방')")
    page.wait_for_selector("#confirmAll")
    page.click("#confirmAll")
    # 전구간 프리셋(실측 41분)을 고르면 1건 기준 예산을 넘는다.
    page.evaluate(
        """() => {
          App.graph.batch('테스트 프리셋', (m) => {
            m.meta.mesh_preset = 'lc6_full_ramp';
            m.solver().params.mesh_preset = 'lc6_full_ramp';
          });
          UI.renderAll();
        }"""
    )
    page.wait_for_selector("#runNotices .warn-line", timeout=30_000)
    text = page.inner_text("#runNotices")
    assert "30분" in text
    assert page.is_visible("#runNotices button:has-text('30분 목표 설정 보기')")
    page.click("#runNotices button:has-text('30분 목표 설정 보기')")
    page.wait_for_selector("dialog.modal[open]")
    assert "실측" in page.inner_text("dialog.modal")
    _shot(page, "06_over_budget.png")
    page.click("dialog.modal button:has-text('닫기')")


# ---------------------------------------------------------------- U23


def test_u23_status_failure_shows_connection_notice(page) -> None:
    """상태 API가 실패하면 '연결 확인 중'이지 솔버 실패가 아니다."""
    _start_parametric(page)
    page.route("**/api/executions", lambda route: route.abort())
    page.evaluate("() => RunStore.poll(true)")
    page.wait_for_function(
        "() => document.getElementById('runNotices').textContent.includes('연결 확인 중')",
        timeout=15_000,
    )
    notices = page.inner_text("#runNotices")
    assert "연결 확인 중" in notices
    assert "마지막 확인" in notices
    assert "실패" not in notices.replace("저장 실패", "")
    _shot(page, "07_offline.png")
    page.unroute("**/api/executions")


# ---------------------------------------------------------------- U40


def test_u40_keyboard_reaches_submit_and_nothing_overlaps_at_200pct(page) -> None:
    """탭 포커스가 제출 버튼에 닿고, 200% 확대에서 필드·버튼이 겹치지 않는다."""
    _start_parametric(page)
    page.click("button.step-btn:has-text('알고 싶은 것')")
    page.click(".purpose-card:has-text('벤트 개방')")
    page.wait_for_selector("#confirmAll")
    page.click("#confirmAll")
    page.wait_for_function("() => !document.getElementById('btnStart').disabled", timeout=30_000)

    reached = page.evaluate(
        """() => {
          const focusable = [...document.querySelectorAll(
            'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])')]
            .filter(el => el.offsetParent !== null);
          return focusable.includes(document.getElementById('btnStart'));
        }"""
    )
    assert reached, "제출 버튼이 키보드 순서에 없습니다"

    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement.tagName")
    assert focused != "BODY"

    # 200% 확대: 1600 px 창을 200%로 보면 CSS 800 px가 된다(§3.2의 768-1023 분기).
    # 실행 바와 그 위 요소가 겹치지 않아야 한다.
    page.set_viewport_size({"width": 800, "height": 600})
    page.wait_for_timeout(300)
    boxes = page.evaluate(
        """() => {
          const ids = ['btnStart', 'runEstimate', 'btnRunPanel'];
          const out = {};
          for (const id of ids) {
            const r = document.getElementById(id).getBoundingClientRect();
            out[id] = {x: r.x, y: r.y, w: r.width, h: r.height};
          }
          out.docScrollW = document.documentElement.scrollWidth;
          out.clientW = document.documentElement.clientWidth;
          return out;
        }"""
    )
    def overlaps(a, b):
        return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])

    assert boxes["btnStart"]["w"] > 0 and boxes["btnStart"]["h"] >= 40
    assert not overlaps(boxes["btnStart"], boxes["btnRunPanel"])
    assert not overlaps(boxes["btnStart"], boxes["runEstimate"])
    _shot(page, "08_zoom200.png")

    # 768 px 아래에서는 제출을 지원하지 않는다. 버튼을 숨겨 버리는 대신
    # 이어서 할 곳을 알려 준다(§3.2).
    page.set_viewport_size({"width": 640, "height": 800})
    page.wait_for_timeout(300)
    assert page.is_visible("#mobileNotice")
    assert "PC에서 이어서" in page.inner_text("#mobileNotice")
    page.set_viewport_size({"width": 1440, "height": 900})


# ---------------------------------------------------------------- U44


def test_u44_records_first_paint(page) -> None:
    """첫 화면 준비 시간을 기록한다(PR 본문에 적는 값)."""
    paint = page.evaluate("() => window.__firstPaintMs")
    assert paint is not None
    (SCREENSHOTS).mkdir(parents=True, exist_ok=True)
    (SCREENSHOTS / "first_paint.json").write_text(
        json.dumps({"first_paint_ms": round(float(paint), 1)}, ensure_ascii=False), encoding="utf-8"
    )
    assert float(paint) < 5000, f"첫 화면이 너무 느립니다: {paint} ms"


# ---------------------------------------------------------------- U32 (조건 복제)


def test_u32_compare_one_condition_makes_a_second_case(page) -> None:
    """값 하나를 복제하면 두 번째 케이스가 생기고 차이표에 그 한 줄만 나온다."""
    _start_parametric(page)
    page.click("button.step-btn:has-text('알고 싶은 것')")
    page.click(".purpose-card:has-text('벤트 개방')")
    page.wait_for_selector("#confirmAll")
    page.click("#confirmAll")
    page.click("button.step-btn:has-text('실행 확인')")
    page.wait_for_selector("#btnCompareOne")
    page.click("#btnCompareOne")
    page.wait_for_selector("dialog.modal[open] #cmpField")
    label = page.evaluate(
        """() => [...document.querySelectorAll('#cmpField option')]
                 .find(o => o.textContent.includes('스코어 잔여 두께')).textContent"""
    )
    page.select_option("#cmpField", label=label)
    page.fill("#cmpValue", "0.04")
    page.click("dialog.modal button:has-text('비교안 만들기')")
    # 두 번째 케이스가 사전 검사에 나타난다.
    page.wait_for_function(
        "() => App.preflight && (App.preflight.combinations || {}).total === 2", timeout=30_000
    )
    inspector = page.inner_text("#inspector")
    assert "2건" in inspector
    assert "score_thickness" in inspector
    assert "0.04" in inspector
    _shot(page, "09_compare_setup.png")


# ------------------------------------------------------- U32·U33·U34 (A/B 패널)

# 서버 계약(§2.6)의 모양을 그대로 흉내낸 두 결과. 솔버 없이 표시 규칙만 검사한다.
_RESULT_A = {
    "schema_version": 1, "execution_id": "case_a", "state": "completed", "case_name": "case_a",
    "changed_vs_first": {},
    "metrics": [
        {"key": "vent_opening_pressure", "label": "개방 압력", "value": 0.385, "unit": "MPa",
         "kind": "scalar", "definition_id": "vent_open_area_25pct_v1", "definition_version": 1,
         "unavailable_reason": None},
        {"key": "peak_load_N", "label": "최대 반력", "value": 0.0, "unit": "N", "kind": "scalar",
         "definition_id": "peak_load_v1", "definition_version": 1, "unavailable_reason": None},
        {"key": "vent_open_area_curve", "label": "개방 면적", "value": None, "unit": "mm2",
         "kind": "curve", "definition_id": "vent_open_area_v1", "definition_version": 1,
         "unavailable_reason": None, "x_unit": "s", "y_unit": "mm2",
         "points": [[0.0, 0.0], [0.001, 4.0], [0.002, 9.0]]},
        {"key": "pressure_time_curve", "label": "압력", "value": None, "unit": "MPa",
         "kind": "curve", "definition_id": "pressure_ramp_v1", "definition_version": 1,
         "unavailable_reason": None, "x_unit": "s", "y_unit": "MPa",
         "points": [[0.0, 0.0], [0.002, 0.85]]},
    ],
    "target": {"metric_key": "vent_opening_pressure", "lower": 0.3, "upper": 0.5, "unit": "MPa",
               "inclusive": True},
    "target_status": "within",
    "judgement": {"sentence": "개방 압력은 0.385 MPa로, 목표 0.30-0.50 MPa 안에 있습니다.",
                  "status": "within", "caveat": "재료 모델이 검증되지 않아 참고용으로 표시합니다."},
    "validation": {"model_validity": "valid", "scope_status": "unverified",
                   "diagnostics": [{"code": "MATERIAL_NOT_VALIDATED", "severity": "review",
                                    "message": "재료가 검증되지 않았습니다."}],
                   "references": ["lc6_preset_30min 실측 14.4분"]},
    "timing": {"queue_seconds": 0, "execution_seconds": 864},
}

_RESULT_B = json.loads(json.dumps(_RESULT_A))
_RESULT_B.update(execution_id="case_b", case_name="case_b",
                 changed_vs_first={"geometry.vent.score_thickness": [0.03, 0.04]})
_RESULT_B["metrics"][0]["value"] = 0.462
_RESULT_B["metrics"][1]["value"] = 12.0
_RESULT_B["metrics"][2]["points"] = [[0.0, 0.0], [0.001, 2.0], [0.002, 7.0]]
# B의 압력 곡선은 정의 버전이 다르다: 중첩하면 안 된다(U33).
_RESULT_B["metrics"][3]["definition_id"] = "pressure_ramp_v2"


def _completed_run(exec_id: str, finished: str) -> dict:
    return {
        "exec_id": exec_id, "case_name": exec_id, "state": "completed", "stage": None,
        "queue_position": None, "legacy": False,
        "timestamps": {"submitted": None, "started": None, "finished": finished},
        "timing": {"queue_seconds": 0, "execution_seconds": 864, "stage_seconds": {}},
        "artifacts": {"report": None, "viewer": None, "csv": None, "summary": None},
        "snapshot": {"input_hash": "sha256:" + exec_id},
    }


_RUNS = {"items": [_completed_run("case_a", "2026-09-07T10:00:00Z"),
                   _completed_run("case_b", "2026-09-07T10:20:00Z")]}


def test_ab_panel_overlays_only_matching_definitions(page) -> None:
    """A/B: 변경 조건 표, (B−A)/A, 기준 0이면 절대 차이, 정의가 다르면 중첩 금지."""
    def _json(route, payload):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    page.route("**/api/executions", lambda route: _json(route, _RUNS))
    page.route("**/api/executions/case_a/result", lambda route: _json(route, _RESULT_A))
    page.route("**/api/executions/case_b/result", lambda route: _json(route, _RESULT_B))
    _start_parametric(page)
    page.click("#btnRunPanel")
    page.wait_for_selector(".run-row")
    for label in ("case_a", "case_b"):
        page.check(f"input[aria-label='{label} A/B 비교에 넣기']")
    page.wait_for_selector("text=A/B 비교")
    panel = page.inner_text("#runPanel")
    assert "geometry.vent.score_thickness" in panel        # 변경 조건 한 줄
    assert "+20.0 %" in panel                              # (0.462-0.385)/0.385
    assert "절대 12" in panel                              # 기준값 0 -> 절대 차이(U34)
    assert "정의나 단위가 달라 중첩하지 않았습니다" in panel     # U33
    assert page.eval_on_selector_all("canvas.ab-chart", "els => els.length") == 1
    assert "목표 범위 내" in panel                          # 목표 판정과
    assert "일부 조건 미검증" in panel                       # 신뢰 정보는 따로 표시된다
    page.eval_on_selector("canvas.ab-chart", "el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(200)
    _shot(page, "10_ab_compare.png")
    page.unroute("**/api/executions")
    page.unroute("**/api/executions/case_a/result")
    page.unroute("**/api/executions/case_b/result")
