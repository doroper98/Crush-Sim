# Crush-Sim

원통형 캔의 압착(지그 압착력)·강도(축방향 압궤) 시뮬레이션 파이프라인.
CATPart → STEP → Gmsh 쉘 메싱 → **OpenRadioss** explicit 해석 → 애니메이션·HTML 리포트 자동화.

- 기준 문서: [`docs/SPEC-v2.1.md`](docs/SPEC-v2.1.md) (요구사항·ADR — 위반 시 리뷰 반려)
- 실행계획: [`docs/EXECUTION_PLAN.md`](docs/EXECUTION_PLAN.md) (Phase별 WBS·게이트)
- Python 패키지 `crushsim` · CLI `csim` · 단위계 **mm·s·tonne·N·MPa** (ADR-04)

> 물리는 자체 구현하지 않는다(ADR-01). 계산은 배치, 시각화는 결과 파일 기반(ADR-02).
> 성공 기준은 시각적 재현이 아니라 **품질 게이트 + 벤치마크 오차**다.

---

## 빠른 시작 — 어떤 컴퓨터에서든 3단계

전제: **Python 3.11+** 와 **git** 만 있으면 된다. (솔버·CATIA는 선택 — 아래 표 참조)

```bash
# 1. 클론
git clone https://github.com/doroper98/Crush-Sim.git
cd Crush-Sim

# 2. 부트스트랩 (venv 생성 + 의존성 설치 + 환경 자가진단)
scripts/bootstrap.sh          # Windows PowerShell: scripts\bootstrap.ps1

# 3. 파이프라인 실행 (파라메트릭 캔, LC-2 측면 압착)
source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
csim all --config configs/cases/lc2_default.yaml
```

`csim doctor` 가 현재 머신에서 무엇이 가능한지 표로 판정한다
(Python·Gmsh·PyVista 오프스크린·ffmpeg·솔버 바이너리). 솔버가 없으면
메싱·덱 생성까지 수행되고, 해석 단계에서 설치 안내와 함께 명확히 중단된다.

### 환경별 가능 범위

| 환경 | 변환(FR-01) | 메싱·덱 | 해석 | 후처리·리포트 |
|---|---|---|---|---|
| Windows + CATIA + 솔버 | ✅ | ✅ | ✅ | ✅ |
| Windows / Linux / WSL2 + 솔버 | — (STEP부터 시작) | ✅ | ✅ | ✅ |
| 솔버 없는 아무 머신 (CI 포함) | — | ✅ | 명확 중단 | 기존 결과 재처리만 |

### 솔버 설치 (OpenRadioss, 공식 릴리스)

```bash
scripts/install_openradioss.sh     # Windows: scripts\install_openradioss.ps1
```

버전 태그는 `configs/solver.yaml`에 고정한다. 설치·실행 플래그는 반드시
[OpenRadioss 공식 README](https://github.com/OpenRadioss/OpenRadioss)를 따른다 (SPEC §9).
설치 후 `csim doctor`로 재확인.

---

## 예제 파일

CATIA 없이도 실형상 STEP으로 파이프라인을 돌릴 수 있도록 예제를 동봉했다.

| 파일 | 출처 | 용도 |
|---|---|---|
| `examples/step/Honda_Can.stp` | [doroper98/meshing-tool](https://github.com/doroper98/meshing-tool) | 실캔 지오메트리 (LC-2 대상) |
| `examples/step/Honda_Cap_Assy.stp` | 〃 | 어셈블리·지그 형상 |
| `examples/step/side_terminal.stp` | 〃 | 임의 형상 강체 지그 |
| `configs/materials/harvested/*.yaml` | [doroper98/can_crush_sim](https://github.com/doroper98/can_crush_sim) `MaterialModel.ts` | FR-10 수확 재료 카드 — **전량 `verified: false`** |

STEP 예제 케이스: `csim all --config configs/cases/lc2_step_example.yaml`

## 주요 CLI

```bash
csim doctor      # 환경 자가진단 (Phase 0 Day-1 게이트)
csim harvest     # FR-10 레거시 재료 수확 (입력: scripts/fetch_legacy.sh 참조)
csim mesh        # FR-03 메싱 + 품질 게이트
csim deck        # FR-04 Radioss 덱 생성
csim run         # FR-05 솔버 실행
csim post        # FR-06/07 후처리·렌더
csim report      # FR-08 HTML 리포트
csim all --config <case.yaml>   # 전 과정 원라인 (변환 제외)
```

## 저장소 구조 (SPEC §11)

```
crushsim/        # 파이프라인 패키지 (units→harvest→converter→geometry→meshing→deck→solver→post→report)
configs/         # 재료 카드(harvested/verified)·케이스 YAML·솔버 설정
examples/step/   # 실형상 STEP 예제
bench/           # 검증 벤치마크 B-1~B-4 (pytest)
tests/           # 단위 테스트
scripts/         # 부트스트랩·솔버 설치·레거시 페치
docs/            # SPEC · 실행계획
webapp/          # Phase 3 전까지 비움 (ADR-03)
runs/            # 실행 출력 (git 제외)
legacy_ref/      # 구 저장소 읽기 전용 참조 (git 제외)
```

## 테스트

```bash
pytest                      # 단위 테스트 (솔버 불필요)
pytest -m bench             # 벤치마크 (솔버 결과 필요 — 없으면 skip)
```

## 개발 규율

- 커밋은 EXP 단위, 실험·교훈은 [`DEVLOG.md`](DEVLOG.md)에 기록 ([`GOAL.md`](GOAL.md) 참조).
- 레거시 코드 반입 금지 — 재료 **데이터**와 프로세스 문서만 화이트리스트 (SPEC §13).
- 게이트 미통과 산출물은 다음 단계로 넘기지 않는다 (ADR-06).

## 라이선스 유의

OpenRadioss는 AGPL v3. 사내 사용은 무방하나 외부 서비스화 전 법무 검토 필요 (SPEC §6).
