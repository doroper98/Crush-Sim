# 해석 결과 3D 뷰어 (생성물 보관)

각 파일은 **자체 완결형 HTML**입니다. 브라우저로 그냥 열면 됩니다 — 서버도,
인터넷도, 별도 설치도 필요 없습니다. 해석 프레임 데이터를 파일 안에 품고
있어서 파일 하나가 3~30 MB입니다.

조작: 드래그 회전 · 휠 확대 · Shift+드래그 이동 (모바일·태블릿은 한 손가락
회전, 두 손가락 확대·이동). 앱 내장 첨부 미리보기는 WebGL을 막는 경우가
있으니 **브라우저에서 직접** 열어 주세요.

> 이 파일들은 `crushsim/ui/viewergen.py` + `crushsim/ui/static/viewer_template.html`이
> `runs/<케이스>/`의 결과에서 생성한 것입니다. 결과가 있으면 언제든
> `csim render` 또는 UI의 '3D 뷰어' 링크로 다시 만들 수 있습니다. 여기 올려둔
> 이유는 `runs/`가 git 제외 대상이라 컨테이너가 회수되면 사라지기 때문입니다.

## 벤트 파단 (각형 셀) — 개발 순서대로

| 파일 | 내용 | 결과 |
|---|---|---|
| `lc6_vent_viewer.html` | v1 자유 캔, 캡에 직접 스코어 | 벽이 1.22 MPa에서 먼저 찢어짐 → 모듈 구속 필요성 확인 |
| `lc6v3_vent_viewer.html` | v3 벽 구속 + 레이저 스코어(테두리) | 2.10 MPa 개방, 단 플랩이 통째로 분리·방출 |
| `lc6v4_vent_viewer.html` | v4 용접 포일 + X자 스코어 | 0.71 MPa, 파단이 X 교차점에서 시작 |
| `lc6v5_vent_viewer.html` | v5 스코어를 실제 기하로 각인 | 0.51 MPa, 개방 단계 파단 100%가 스코어 위 |
| `v5_medium_viewer.html` | **v5 + 스코어 0.5mm 세분 — 실무 기준** | **파단 0.303 / 개방 0.385 MPa** |
| `v5_fine_viewer.html` | v5 + 스코어 0.3mm 세분 (수렴 근거) | 0.287 / 0.366 MPa — medium 대비 5% |
| `lc6_step_vent_viewer.html` | **실형상 STEP 어셈블리** (`can/test.stp` 캔+캡+용접 벤트, 요소별 실측 두께로 S-스코어 각인) | 파단 개시 **0.335 MPa** @ KE/IE 0.5% (개방은 컨테이너 종료로 미기록) |

배경과 판정 기준은 [`../docs/VENT_BURST.md`](../docs/VENT_BURST.md) 참고.

## 압착 해석

| 파일 | 내용 |
|---|---|
| `HondaCanViewer_lc2.html`, `honda_viewer.html` | Honda 캔(CATIA STEP) 측면 압착 |
| `PipeCrushViewer.html` | 파이프(원통 롤러) 압착 |
| `BroadsideCrushViewer.html`, `broadside_viewer.html` | 넓은 면 압착 |
| `CylinderPipeCrush.html` | 원통 캔 파이프 심압입 |
| `lc4_bead_crimp_viewer.html` | 2170 비딩(궤도 롤러) |
| `lc5_swell_viewer.html` | 2170 내압 스웰 10 MPa |

## 검증 벤치마크 B-3 (메쉬 수렴)

| 파일 | 내용 |
|---|---|
| `CrushViewer_b3_coarse.html` | 1.0mm |
| `b3_fine_viewer_artifact.html`, `b3_fine_viewer_download.html` | 0.1mm 불완전성 버전 (프레임 수만 다름) |
| `b3_final_viewer.html` | **0.4mm 최종 — B-3 PASS (+1.94%)** |
