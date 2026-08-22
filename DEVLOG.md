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
