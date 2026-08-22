# DEVLOG — Crush-Sim

실험 단위(EXP-###) 개발 기록. 형식: 날짜 · 목표 · 결과 · 교훈.
메트릭은 게이트 통과율·벤치마크 오차 (UI 점수 아님 — GOAL.md).

---

## EXP-001 (2026-08-22) — 저장소 스캐폴드 + Phase 0/1 골격

**목표**: SPEC v2.1 §11 구조로 신규 저장소 초기화. units.py 단일 정의,
FR-10 수확기 실행, 파라메트릭 지오메트리→메싱→게이트→덱 생성→CLI 골격,
pytest green, 어떤 머신에서든 clone→bootstrap→실행 가능한 재현 경로.

**결과**: 본 저장소 최초 커밋. 상세는 docs/EXECUTION_PLAN.md §5 (이번 PR의 범위).
- FR-10 수확기: 레거시 `MaterialModel.ts`에서 **재료 카드 243종** 생성, 전량 `verified: false`.
- pytest **248 passed, 2 skipped** (skip 2건은 솔버 결과가 필요한 B-1/B-2 대조 — 해석해 공식 자체는 19개 테스트로 상시 검증).
- Gmsh 파라메트릭 캔 1.2mm → 8,473 요소, 쿼드 100%, min SICN 0.391, 메시 게이트 통과. `Honda_Can.stp` STEP 메싱 동작.
- `csim all --skip-solver` 로 LC-1/LC-2 덱 생성 + UNVERIFIED 워터마크 HTML 리포트까지 관통. `csim doctor` 코어 전항 PASS(오프스크린 렌더 포함).
- 주의: 덱 키워드 문법은 실 starter 미검증 (writer·덱·리포트에 경고 명시, Phase 1.3 게이트).

**교훈(레거시 부검에서, 코드 반입 없이)**:
1. 물리는 검증된 솔버에 위임 — 안정화 계수는 재료 물성이 아니다.
2. 단위계는 한 파일에서만 정의한다.
3. 오프스크린 렌더 검증을 가장 먼저 한다 — 실패 시 이후 전부 헛수고.
4. 코어가 게이트를 통과하기 전에 표면(UI)을 다듬지 않는다.

**미결(다음 EXP)**:
- 솔버 설치 머신에서 OpenRadioss 공식 예제 1건 → MP4 관통 (Phase 0 완료 게이트)
- 덱 문법을 실 starter로 검증·수정 (Phase 1.3)

---

## EXP-002 (2026-08-22) — 실 솔버 검증: 덱 문법·엔진·후처리 전 구간 관통

**목표**: EXP-001의 덱 생성기를 실제 OpenRadioss(latest-20260728)로 검증하고
`csim all` LC-2를 끝까지 관통시킨다 (Phase 1.3~1.7 게이트).

**결과**: `csim all --config configs/cases/lc2_default.yaml` **전 과정 완주** —
starter 오류 0, 엔진 완주(9.1만 사이클), 공식 변환기(TH→CSV·ANIM→VTK),
MP4 3구도+GIF 렌더, UNVERIFIED 워터마크 HTML 리포트. 에너지 오차 1.2% (≤5% 통과),
아워글래스 0%, 부가질량 0%. 캔이 지그-V블록 사이에서 실제 압착됨
(전면 19.96mm / 배면 0mm), IE 22.6mJ ≈ 박판 링 압착 이론 스케일.

**수정 항목(전부 실 starter/엔진/포맷 정의로 검증)**: /SHELL 제목 제거 ·
/TH 객체 ID화 · LAW2/RBODY/TYPE7 누락 카드 · /PROP 필드 정렬 · /BEGIN 단위
20자 필드 · /ANIM TENS/STRESS 제거(엔진 세그폴트) · gmsh가 풀어놓는
RLIMIT_STACK 복원(자식 즉사 원인) · 절대경로화(install_root/RAD_CFG_PATH/변환기) ·
IMPDISP→IMPVEL(외부일 폭주) · Inacti=6 · 강체 두께 0.5mm+이격 · V블록 지지대
신설(캔 밀림 방지) · 접촉 인터페이스 3분할 + /TH/INTER(FN/FT) 반력 기록 ·
준정적 게이트를 변형체(CAN) KE 기준으로.

**교훈**:
1. cfg 포맷 정의(hm_cfg_files)와 QA 덱이 최고의 문법 레퍼런스다. 추측 금지.
2. 라이브러리(gmsh)가 부모 프로세스 상태(rlimit)를 바꿔 자식 솔버를 죽일 수 있다.
3. LC-2는 반대편 지지대 없이는 물리적으로 성립하지 않는다(캔이 밀려 도망감).
4. 구동 속도는 관성 지배 여부를 결정한다: 2 m/s에서 캔 KE/IE 52% → 0.5 m/s로 조정.

**미결(다음 EXP)**:
- **반력 채널 이상**: /TH/INTER FNX(≈0.019N)가 에너지 기반 평균 하중(≈1.1N)과
  ~60배 불일치. /TH/RBODY FX도 동일하게 작음. B-2(링 강성 해석해 ±10%)로
  힘 채널을 정량 교정할 것 — 접촉 강성(Istf/Stfac) 또는 TH 정의 재검토.
- 벤치마크 B-1/B-3 실판정, lc1_default 엔진 완주 확인.
