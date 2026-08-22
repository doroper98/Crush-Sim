# Crush-Sim 실행계획서 (Execution Plan)

기준 문서: [`docs/SPEC-v2.1.md`](./SPEC-v2.1.md) · 작성일: 2026-08-22 · 상태: **활성**

이 문서는 SPEC v2.1을 실행 가능한 작업 단위로 분해한 계획서다.
모든 결정은 SPEC의 ADR-01~07을 따르며, 위반하는 작업은 리뷰에서 반려한다.

---

## 1. 목표 재확인

원통형 캔의 **측면 지그 압착(LC-2, 본래 목적)** 과 **축방향 압궤(LC-1)** 를
검증된 explicit 솔버(OpenRadioss)로 해석하고, 애니메이션·리포트까지 자동화하는
배치 파이프라인을 만든다. 구 저장소(can_crush_sim)의 실패 원인(자체 게임물리,
단위 혼재, 접촉 부재, 검증 부재, UI 과투자)을 구조적으로 차단한다.

**성공의 정의는 "시각적 재현"이 아니라 §7 게이트 통과 + §8 벤치마크 오차다.**

---

## 2. 실행 환경 전략 — "어떤 컴퓨터에서도 클론 후 실행"

| 환경 | 가능한 작업 | 비고 |
|---|---|---|
| **Windows 단일 머신** (기본) | 전 과정: CATIA 변환 → 메싱 → 솔버 → 후처리 → 리포트 | OpenRadioss 공식 win64 바이너리. CATIA 변환은 CATIA V5 설치 머신에서만 |
| **Linux / WSL2** | CATIA 변환 제외 전부 | 솔버 배치 실행 확장 옵션 |
| **CI (GitHub Actions)** | 솔버 없는 단위테스트·게이트 로직·덱 생성 검증 | ubuntu + windows 매트릭스 |
| **CATIA 없는 어떤 머신** | STEP부터 시작하는 전 과정 (`examples/step/` 예제 동봉) | 이 저장소의 기본 재현 경로 |

재현 절차는 3단계로 고정한다 (상세는 README):

```
git clone https://github.com/doroper98/Crush-Sim.git && cd Crush-Sim
scripts/bootstrap.sh        # Windows: scripts\bootstrap.ps1  — venv + 의존성 + csim doctor
csim all --config configs/cases/lc2_default.yaml
```

- **`csim doctor`** 가 Phase 0 Day-1 체크리스트를 자동화한다: Python/Gmsh/PyVista
  오프스크린/ffmpeg/솔버 바이너리 존재 여부를 표로 판정. 솔버가 없으면 어디까지
  실행 가능한지 명시한다.
- 솔버 설치는 `scripts/install_openradioss.sh|.ps1` 로 공식 릴리스(태그는
  `configs/solver.yaml`에 고정)를 내려받는다. 저장소에 바이너리를 커밋하지 않는다.
- 레거시 참조가 필요하면 `scripts/fetch_legacy.sh|.ps1` 가 `legacy_ref/`(git 제외)에
  얕은 클론한다. FR-10 수확 결과(YAML 카드)는 이미 커밋되어 있으므로 평상시엔 불필요.

---

## 3. 예제 자산 (커밋 완료)

사용자 지정 소스 저장소에서 확보한 CATIA V5 STEP 내보내기 파일:

| 파일 | 출처 | 용도 |
|---|---|---|
| `examples/step/Honda_Can.stp` (72KB) | doroper98/meshing-tool `raw model/` | 실캔 지오메트리 — FR-02/03, LC-2 대상 |
| `examples/step/Honda_Cap_Assy.stp` (2.3MB) | 〃 | 어셈블리 처리·지그 형상 테스트 |
| `examples/step/side_terminal.stp` (0.9MB) | 〃 | 임의 형상 강체 지그 예제 (구 프로젝트 불가 항목) |
| `configs/materials/harvested/*.yaml` | doroper98/can_crush_sim `MaterialModel.ts` | FR-10 수확 재료 카드, 전량 `verified: false` |

CATProduct 원본은 두 소스 저장소에 없다(모두 STEP 상태). FR-01(CATIA COM 변환기)
검증용 실 CATPart는 Phase 2에서 사내 자산으로 투입한다.

---

## 4. WBS — Phase별 작업 분해

### Phase 0 — 환경·스캐폴드 (이번 세션에서 착수)

| # | 작업 | 산출물 | 완료 판정 |
|---|---|---|---|
| 0.1 | 저장소 스캐폴드 (§11 구조) | 패키지·configs·tests·docs | 본 PR |
| 0.2 | `crushsim/units.py` — 단위·게이트 한계 단일 정의 (ADR-04) | units.py | 타 모듈에 수치 하드코딩 0건 |
| 0.3 | FR-10 재료 수확기 + 실행 | `configs/materials/harvested/*.yaml` (~240종) | 전량 unverified 태그, pytest 통과 |
| 0.4 | `csim doctor` 환경 자가진단 | CLI | 솔버 부재 시에도 명확한 판정표 |
| 0.5 | 부트스트랩 스크립트 (win/linux) | `scripts/` | 새 머신 클론→테스트 통과 |
| 0.6 | **[수동/Windows]** OpenRadioss 공식 예제 1건 → anim_to_vtk → PyVista → MP4 관통 | 예제 MP4 | **Phase 1 진입 게이트** (SPEC §9 Day-1 #5) |

### Phase 1 — PoC: 파라메트릭 원통 관통

| # | 작업 | 산출물 | 완료 판정 |
|---|---|---|---|
| 1.1 | 파라메트릭 원통 생성기 (FR-02 일부) | `crushsim/geometry/` | 코드로 캔 형상 정의 |
| 1.2 | Gmsh 쿼드우세 쉘 메싱 + 품질 게이트 + 자동 재메싱≤3회 (FR-03) | `crushsim/meshing/` | §7 메시 게이트 자동 판정 |
| 1.3 | Radioss 덱 생성기: LAW2·SHELL(QEPH)·TYPE7·FLOOR·REF_TOOL·IMPDISP (FR-04) | `crushsim/deck/` | 골든 스냅샷 테스트 + **실 starter 검증(솔버 설치 후)** |
| 1.4 | 솔버 래퍼: 로그 파싱·run_summary.json (FR-05) | `crushsim/solver/` | LC-1/LC-2 완주 |
| 1.5 | 후처리: 공식 변환기 래핑, 곡선·피크·에너지·덴트 (FR-06) | `crushsim/post/` | 자체 바이너리 파서 0건 |
| 1.6 | 렌더 MP4/GIF 3구도 + HTML 리포트 (FR-07/08) | `crushsim/post/`·`report/` | 컬러맵 프레임 고정 |
| 1.7 | `csim all` 원라인 파이프라인 | `crushsim/cli.py` | LC-2 기본 케이스 완주 |
| 1.8 | 벤치마크 B-1(Alexander)·B-2(링 강성)·B-3(수렴) | `bench/` | **Phase 2 진입 게이트**: B-1 ±25%, B-2 ±10%, B-3 ≤5% |

주: 1.1~1.7 골격과 해석해 공식은 이번 세션에서 구현·단위테스트까지 완료.
솔버 실행이 필요한 판정(1.3 starter 검증, 1.4, 1.8)은 솔버 설치 머신에서 수행.

### Phase 2 — 실 CAD 연동

| # | 작업 | 완료 판정 |
|---|---|---|
| 2.1 | FR-01 CATIA COM 배치 변환기 (Windows) — conversion_log.csv, 1건 실패가 배치를 죽이지 않음 | CATPart 10개 중 1 실패 시 9개 정상 (§12) |
| 2.2 | FR-02 완성: 관성 주축 +Z 정렬, 바닥 Z=0, 외피 추출, 지그 `rigid: true` | 실캔 STEP 자동 전처리 |
| 2.3 | 실형상 지그 STEP LC-2 접촉 해석 (`side_terminal.stp` 등) | 구 프로젝트 불가 항목 달성 |
| 2.4 | 실캔 LC-2 HTML 리포트 자동 생성 | **Phase 3 진입 게이트** |

### Phase 3 — 확장·UI (벤치마크 통과 전 착수 금지, ADR-03)

| # | 작업 | 완료 판정 |
|---|---|---|
| 3.1 | FR-09 파라메트릭 스터디 (YAML 매트릭스 → 취합 CSV) | 배치 완주 |
| 3.2 | 높이별 두께 맵, 실측 보정 | B-4 + 실측 대조 |
| 3.3 | FR-11 웹 UI 제로베이스 (§10: 3화면·3단계·성능 예산, ADR-07) | §10.4 수용 기준 |
| 3.4 | Linux 서버/WSL2 솔버 이관 (선택) | 배치 처리량 |

---

## 5. 이번 PR의 범위 (Phase 0 + Phase 1 골격)

**포함**: §11 저장소 구조 전체, units.py, FR-10 수확기+수확 카드, 파라메트릭
지오메트리, Gmsh 메싱+게이트, 덱 생성기, 솔버 래퍼(부재 시 명확 실패), 후처리·
렌더·리포트 골격, `csim` CLI(doctor/all 포함), LC-1/LC-2 기본 케이스 YAML,
B-1/B-2 해석해, pytest(컨테이너에서 green), CI 워크플로, 부트스트랩 스크립트,
예제 STEP 3종, 본 계획서.

**제외(후속)**: 실 솔버 실행·벤치마크 판정(솔버 설치 머신 필요), FR-01 CATIA
변환기 실검증(CATIA 머신 필요), 덱 문법의 실 starter 검증, Phase 3 전체.

**리스크 및 대응**

| 리스크 | 대응 |
|---|---|
| 덱 키워드 문법이 실 OpenRadioss starter에서 거부될 수 있음 | 덱 writer에 경고 주석 명시. Phase 1.3에서 공식 예제 덱과 대조·수정 (게이트) |
| 재료 카드 전량 미검증 | `verified: false` + 리포트 UNVERIFIED 워터마크. 실사용 후보만 문헌 대조 승격 (§4 FR-10) |
| 준정적 조건 미충족 (운동E 초과) | 게이트 자동 판정 → 램프 속도·질량 스케일링 조정 가이드를 리포트에 출력 |
| PyVista 헤드리스 렌더 실패 | `csim doctor`가 선판정. OSMesa/xvfb 안내 |
| AGPL v3 (OpenRadioss) | 사내 사용 무방. 외부 서비스화 전 법무 검토 (SPEC §6) |

---

## 6. 개발 규율 (§1.2 계승)

- EXP 단위 커밋, 실패 시 reset. `DEVLOG.md`에 실험·교훈 기록.
- 메트릭은 **게이트 통과율·벤치마크 오차**. UI 점수 아님.
- 게이트 미통과 산출물은 다음 단계로 넘기지 않는다 (ADR-06).
- 레거시 반입은 화이트리스트 방식: 재료 데이터·프로세스 문서만 (SPEC §13.1).
