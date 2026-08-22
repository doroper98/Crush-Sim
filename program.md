# program — Crush-Sim 운영 문서

## 문서 체계
- `docs/SPEC-v2.1.md` — 요구사항·ADR·게이트·검증 계획. 유일한 기준 문서.
- `docs/EXECUTION_PLAN.md` — Phase별 WBS·완료 판정·리스크. 변경 시 PR 리뷰 필수.
- `GOAL.md` — 정량 성공 기준·비목표.
- `DEVLOG.md` — EXP 단위 실험 기록.

## 작업 절차
1. 작업은 EXP 단위로 정의하고 DEVLOG에 목표를 먼저 적는다.
2. 구현 → `pytest` green → 게이트/벤치마크 판정 → 커밋. 실패 실험은 reset.
3. Phase 진입 게이트(EXECUTION_PLAN §4)를 통과하기 전에 다음 Phase 착수 금지 (ADR-03·06).

## 리뷰 체크리스트 (반려 사유)
- [ ] ADR-01: 자체 물리 적분기·질량스프링·PBD 코드 없음
- [ ] ADR-04: units.py 밖의 단위·게이트 한계 하드코딩 없음
- [ ] ADR-05: 재료 수치가 코드에 없음 (YAML 카드만)
- [ ] ADR-07 / §13.1: legacy_ref 코드·디자인 반입 없음 (데이터·프로세스 문서만)
- [ ] §13.4: TH/ANIM 자체 바이너리 파서 없음 (공식 변환기 래핑만)
- [ ] §13.5: 조용한 빈 결과 없음 (실패는 예외로 크게)
