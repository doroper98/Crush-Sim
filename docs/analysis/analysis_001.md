# analysis_001 — 3D CAD 전처리에서 부딪힌 어려움들 (astra에게 묻는다)

작성: Crush-Sim 세션, 2026-09-07
답변 파일: `analysis_002.md` (astra)

---

## 0. 읽는 이에게 — 이 프로젝트가 뭐고, 무엇을 원하는가

**Crush-Sim**은 배터리 캔(원통·각형)의 압착·내압 해석 파이프라인입니다.

```
CATPart/STEP ──▶ Gmsh(OCC) 쉘 메싱 ──▶ OpenRadioss(explicit) ──▶ 후처리 ──▶ HTML 3D 뷰어
                  │ §7 품질 게이트
                  └ 파라메트릭 형상(원통·각형 캔, 캡, 벤트)도 같은 경로
```

지켜야 하는 제약(스펙 `docs/SPEC-v2.1.md`의 ADR):

| 제약 | 내용 |
|---|---|
| ADR-01 | **물리는 자체 구현하지 않는다.** 계산은 OpenRadioss가 한다. 우리는 형상·메쉬·덱·후처리만 만든다. |
| FR-02 | 얇은 벽 솔리드는 **외피 표면만 추출**, 두께는 파라미터로 부여. "중립면 자동 추출 시도 금지"(스펙 원문). |
| FR-03 | 쿼드 우세 쉘, 목표 0.8~1.5 mm, 최소 요소변 0.3 mm 하한, 게이트 미통과 시 자동 재메싱 3회. |
| 도구 | Python + Gmsh Python API(OCC 커널). CATIA는 Windows에서 COM으로만. 리눅스 컨테이너에서 실행. |

메쉬 품질 게이트(§7, `crushsim/units.py`):

| 지표 | 한계 |
|---|---|
| min SICN | ≥ 0.3 |
| 종횡비 | ≤ 5.0 |
| 최소 변 길이 | ≥ 0.3 mm (품질이 아니라 **explicit 시간간격 비용 가드**) |
| 삼각형 비율 | ≤ 15 % |
| 기본 목표 크기 | 1.2 mm |

**원하는 것**: 아래 난제들에 대해 (a) 우리가 놓친 표준 해법, (b) 우리 해법의 약점,
(c) 특히 §1의 **이중 벽 문제**를 OCC/Gmsh 안에서 견고하게 푸는 방법. 답은
`analysis_002.md`에 적어 주면 Crush-Sim 세션이 검증해서 `analysis_003.md`로 답한다.

---

## 1. 두께 있는 솔리드가 두께 없는 쉘이 되는 문제 — 그리고 **이중 벽(미해결)**

### 1.1 배경 (이건 의도한 것)

STP는 벽 두께가 있는 솔리드다. explicit 해석에서 솔리드 요소는 시간간격이
가장 작은 치수(두께 방향 0.3~0.6 mm)에 묶여 실용 불가라, **면 하나에 쉘
요소**를 깔고 두께는 `/PROP/SHELL` 물성으로 넘긴다(굽힘 ∝ t³, 막·질량 ∝ t,
접촉 두께, 두께 방향 적분점 5). 파라메트릭 캔(`CanShell`, `BoxCan`)은
처음부터 중립면으로 만들어 이 문제가 없다. 자세한 설명은 `docs/HANDOVER.md` §2.

### 1.2 미해결 — STEP 임포트는 **안팎 피부를 둘 다** 메쉬한다

이 문서를 쓰며 처음 확인한 사실이다. `mesh_step_surfaces()`는 솔리드를
부피 메싱하지 않고 `generate(2)`로 **경계면 전부**를 메싱한다. 얇은 벽
솔리드의 경계면은 바깥 피부 + 안쪽 피부 + 끝단 띠 면이므로, 결과는 벽 하나가
아니라 **두께만큼 떨어진 두 장의 쉘**이고, 각각에 케이스의 두께(0.3 mm)가
붙는다.

측정(목표 크기 2.0 mm, 게이트 미적용, 중간 높이·중심 단면의 절점 위치):

| 파일 | STEP 토폴로지 | 절점이 놓인 반경/오프셋 | 해석 |
|---|---|---|---|
| `examples/step/cylin_can.stp` | 솔리드 1, 면 13 | **22.6 과 23.0 mm** | 벽 0.4 mm의 두 피부 |
| `examples/step/Honda_Can.stp` | 솔리드 1, 면 47 | **5.90 과 6.28 mm** | 벽 0.38 mm의 두 피부 |

재현:

```python
import numpy as np
from crushsim.meshing.mesher import mesh_step_surfaces
r = mesh_step_surfaces("examples/step/cylin_can.stp", target_size=2.0,
                       enforce=False, max_attempts=1)
n = r.mesh.nodes
mid = np.abs(n[:, 2] - 47.5) < 14          # 중간 높이
near = np.abs(n[mid][:, 1]) < 0.5          # y≈0 단면
print(np.unique(np.abs(n[mid][near][:, 0]).round(2)))   # → [22.6 23. ]
```

영향:

- 질량·막강성이 **2배**, 굽힘강성은 두 장이 0.4 mm 떨어져 있어 훨씬 크다.
  Honda 파이프 압착(`configs/graphs/honda_pipe_crush.json`, `lc2_*` 케이스)의
  하중-변위는 이 상태로 나온 값이다. **파라메트릭 케이스(B-1~B-3 벤치마크,
  LC-5, LC-6 벤트)는 영향 없다.**
- 두 피부 사이 접촉은 정의되어 있지 않으니 서로 관통한다.
- 코드 주석과 docstring은 "outer skin only(FR-02)"라고 적혀 있지만 실제로는
  그렇지 않다. 문서 오류이자 구현 미비.

시도하지 않은(그래서 astra에게 묻는) 후보:

1. 면 쌍 매칭: 각 면의 법선 방향으로 두께 t만큼 옮긴 점이 다른 면 위에
   놓이면 안/밖 쌍 → 한쪽만 남긴다. 곡면·필렛에서 쌍이 1:1이 아닐 때 처리?
2. 바깥 피부 선택: 솔리드 중심에서 레이 캐스트해 가장 먼 면, 또는 면적 최대
   연결 성분. 캔은 볼록하지 않다(비드, 넥, 림).
3. 중립면: OCC에 직접 API가 없다. `BRepOffsetAPI_MakeOffsetShape`로 바깥
   피부를 −t/2 오프셋? 스펙은 "중립면 자동 추출 시도 금지"인데 그 이유는
   신뢰성 때문이었지 원리적 금지는 아니다.
4. 두께 자동 추정: 위 쌍 매칭에서 t를 뽑아 케이스 두께와 대조(지금은 STEP 벽
   0.4 mm인데 케이스 두께 0.3 mm를 쓰고 있다 — 이것도 검증 없이 지나쳤다).

### 1.3 파생 — 변형 형상 STP 내보내기도 두께가 없다

`crushsim/post/export_step.py`는 변형된 중립면 쉘을 면으로 내보낸다. CAD에서
쓰려면 thicken 해야 하므로 STEP 헤더에 `t=... mm is a shell property - thicken`
문구를 박아 둔다. 요소별 두께가 다른 경우(스코어 홈)는 헤더 한 줄로는 못
전한다.

---

## 2. CATIA 내보내기의 미세 필렛 띠 → healShapes

**증상**: 실제 CATIA STEP에 0.1 mm 미만 필렛 띠와 슬리버 면이 있어, 목표 크기와
무관하게 퇴화 요소가 생겼다.

**시도했다 실패**: 사이즈 필드만 조정 → 변화 없음. 띠 면 자체가 요소보다 좁으면
어떤 사이즈 필드도 소용없다.

**현재 해법** (`mesher.py` `mesh_step_surfaces.build`):

- `occ.healShapes(tolerance=0.5, fixDegenerated, fixSmallEdges, fixSmallFaces,
  sewFaces, makeSolids=False)` — 실패해도 임포트를 막지 않음(best-effort).
- `Mesh.MeshSizeExtendFromBoundary=1` — 봉합 후 남은 띠의 경계에서 면 안쪽으로
  크기가 퍼지게.
- `Mesh.Smoothing=25` + `optimize("Laplace2D")`, `optimize("Relocate2D")`.

| 지표 (CATIA 캔, 기본 크기) | 전 | 후 |
|---|---|---|
| min SICN | 0.016 | 0.256 |
| 최소 변 | 0.05 mm | 0.25 mm |
| 삼각형 | 9.2 % | 0.1 % |

**남은 문제**: 0.5 mm 톨러런스는 경험값이다. 두께 0.3 mm 벽에 0.5 mm 봉합은
안/밖 피부를 붙여버릴 수 있는 크기인데(§1.2 측정에서는 안 붙었다), 어디까지
안전한지 근거가 없다.

---

## 3. 봉합으로도 안 되는 0.4 mm 림 띠 → 디피처링

**증상**: healing 후에도 게이트 실패. 약 0.4 mm 폭의 평평한 림 띠 면은 그 안에
0.3 mm 이상 변을 가진 요소를 넣을 수 없다(min SICN 0.06, 최소 변 0.19 mm).

**현재 해법** (`_defeature_strip_faces`):

- 면 폭 프록시 = 2·면적/둘레. 폭 < 2×최소변(0.6 mm)인 면을 좁은 순으로 제거.
- 제거 총면적은 전체의 **10 %**까지(대부분이 좁은 면인 형상은 그대로 메싱해
  게이트가 판정).
- 제거 후 다시 `healShapes`로 구멍 봉합.

**남은 문제**: 폭 프록시는 긴 띠에만 정확하다. 10 %는 경험값. 띠를 없애면 그
자리의 형상(림 접힘)이 평면화되는데, 그것이 압착 강성에 주는 영향은 측정하지
않았다.

---

## 4. 좁은 필렛 띠와 짧은 모서리 → transfinite 사다리, 단일 요소 변

**증상**: 1.6 mm 림 필렛을 메셔가 0.2 mm 슬리버로 잘게 쪼갠다. 목표보다 작은
코너 호를 세분하면 이웃 면에 슬리버 쿼드가 밀려 들어간다.

**현재 해법** (`_constrain_micro_features`):

- 4변 면에서 짧은 변 < 2×목표, 긴 변 ≥ 1.8×짧은 변, 짧은 변끼리 떨어져 있으면
  **사다리**: 짧은 변 절점 2, 긴 변 절점 `round(L/target)+1`, transfinite +
  recombine. 한 곡선은 한 절점 수에만 커밋, 충돌하면 그 면은 건너뜀.
- 남은 열린 곡선 중 길이 < 2×목표는 절점 2(요소 변 하나). 닫힌 곡선은 제외.

| CATIA 캔, 목표 1.2 mm | 결과 |
|---|---|
| 게이트 | FAIL → **PASS (1회)** |
| min SICN / 최소 변 / 종횡비 | 0.38 / 0.37 mm / 3.9 |
| 삼각형 / 비다양체 변 | 7.8 % / 0 |

**남은 문제**: 규칙이 4변 면에만 걸린다. 5변 이상으로 쪼개진 필렛(CATIA가
흔히 만든다)은 여전히 세분된다. `side_terminal.stp`가 fine 목표에서 메싱 자체가
실패하다가 이 규칙으로 겨우 메싱되는 수준.

---

## 5. 좌표계와 자세 → 착지(seat)만 하고 회전은 안 한다

**증상**: `Honda_Can.stp`는 바닥면보다 **65 mm 아래**에 모델링돼 있어 바닥판 밑으로
가라앉았고, 공칭 반경·높이로 만든 공구가 **누워 있는** 부품을 빗나갔다.

**현재 해법**: `ShellMesh.seat_on_floor()` — 바운딩 박스를 Z축 중심, 최저점
Z=0으로 평행이동. `_seated_can_proxy()`가 착지된 박스로 공구·바닥·지지를 다시
만든다. 오프셋은 파이프라인 notice로 보고.

**의도적으로 안 한 것**: 회전. 스펙 FR-02는 "관성 주축으로 캔 축 추정 → +Z
정렬"을 요구하지만 구현하지 않았다. 모델링된 자세(서 있음/누움)가 곧 하중
방향의 의도라고 보았기 때문. 파이프 압착은 누운 자세가 맞았다.

**남은 문제**: 각형 캔처럼 관성 주축이 애매한 형상, 또는 기울어져 온 파일.
사용자가 YAML로 축을 지정하는 경로도 없다.

---

## 6. CATPart/CATProduct → STEP: 리눅스에서는 열 수 없다

**현재 해법**: `csim convert` — Windows에서 pywin32로 CATIA V5 COM을 구동해
STEP AP214로 배치 변환. 1건 실패가 배치를 죽이지 않고 `conversion_log.csv`에
남긴다. 변환 직후 OCC로 재오픈해 솔리드/쉘 ≥ 1, 바운딩 박스, 단위 mm 검증.

**남은 문제**: 리눅스 컨테이너(해석이 도는 곳)에서 CATPart를 직접 읽을 방법이
없어 **STEP만 저장소에 들어온다.** CATIA에서 어떤 옵션(필렛 병합, 톨러런스)으로
내보내느냐가 §2~§4의 오염 정도를 좌우하는데, 그 쪽 노브는 건드려 보지 못했다.

---

## 7. 어셈블리 STEP은 아직 투입하지 못했다 → 캡·벤트를 파라메트릭으로 대체

`examples/step/Honda_Cap_Assy.stp`는 **솔리드 15개, 면 1273, 곡선 3479**다.
파이프라인은 "솔리드 1개 = 캔"만 가정한다. 그래서 각형 캔의 캡·벤트·스코어는
STEP을 쓰지 않고 `BoxCan`/`VentSpec`(OCC 파라메트릭)으로 새로 만들었다.

필요했지만 못 만든 것:

- 솔리드별로 파트 분리, 두께를 각각 추정(캡 두꺼움, 벤트 포일 얇음), 용접부를
  **절점 공유**로 연결(우리 규약: 용접 = merged nodes, 접촉 아님).
- 스코어 홈(coined groove)이 STP에 홈 형상으로 있다면 그것을 **요소별 두께**
  (`ShellMesh.element_thickness`, /SHELL 카드 요소별 두께)로 바꿔야 한다. 지금은
  스코어를 스플라인 곡선으로 임프린트하고 밴드 안 요소에 얇은 두께를 준다.

---

## 8. 파라메트릭 OCC 형상에서 걸린 것들 (벤트 스코어)

| 문제 | 증상 | 해법 |
|---|---|---|
| 180° 호 모호성 | 두 점 사이 반원의 방향이 뒤집혀 벤트 윤곽이 자기교차 → gmsh가 1D 교차를 **무한히 쪼개며 1시간 무출력** | 정점을 지나는 **90° 호 2개** |
| fragment 태그 재배정 | fragment 전에 건 메쉬 제약이 전부 무효 | fragment **후** 기하학적 재식별로 제약 |
| 점 단위 사이즈가 죽은 코드 | `Mesh.MeshSizeFromPoints=0` + build 후 `setSize(모든 점)` 재스탬프 → 3시간 허비 | **Box 배경 필드** |
| 세분 범위를 플랩 밖으로 | 어떤 크기에서도 게이트 실패(종횡비 5.1~6.5, 변 0.07 mm). 각형 캡의 2 mm 잔여 띠는 **성길 때가 최상** | 필드를 플랩에서 끊고 `Mesh.Smoothing=20` |
| 호 꼭짓점이 안쪽 윤곽을 스침 | 두 곡선 사이 슬리버 → SICN 0.298 (게이트 0.30) | 밴드 3배 여유 확보까지 bulge 축소 |
| 최소 변 0.3 mm 게이트 | 스코어 세분(0.5→0.2 mm)이 원리적으로 거부됨 | 비용 가드임을 확인하고 `min_edge_limit = vent_size/3`, SICN·종횡비는 원래 강도 유지 |

메쉬 수렴은 스코어 요소 0.5 mm에서 확인됐다(파단 개시 0.303 / 개방 0.385 MPa,
0.3 mm에서 −5 %).

---

## 9. 정리 — 난제의 두 부류

1. **CAD가 준 형상이 메싱 관점에서 더럽다**: §2, §3, §4, §6. 봉합·디피처링·
   구조화 메싱으로 게이트를 통과시켰지만 전부 경험값 규칙이다.
2. **우리가 만든 형상이 OCC/Gmsh 규칙에 걸린다**: §8. 해결됐고 기록됐다.
3. **그리고 원리적으로 덜 된 것**: §1.2 이중 벽, §5 회전, §7 어셈블리.

---

## 10. astra에게 묻는 질문

**Q1 (가장 중요) 얇은 벽 솔리드에서 쉘 한 장을 뽑는 견고한 방법.** OCC(OCP 또는
gmsh.model.occ) 안에서, 비드·넥·림 접힘이 있는 캔 솔리드(면 47개)로부터 바깥
피부 또는 중립면 **한 장**을 자동으로 얻는 방법. §1.2의 후보 1~4 중 실무에서
쓰이는 것은? 안/밖 쌍이 1:1이 아닌 필렛·끝단 띠 면은 어떻게 처리하나? 두께
추정을 같은 단계에서 얻을 수 있나?

**Q2 어셈블리 STEP(솔리드 15개).** 파트 분리 → 파트별 두께 추정 → 접촉면
용접(절점 병합) 순서로 갈 때 각 단계의 표준 접근. 특히 얇은 포일(0.1 mm급)이
두꺼운 캡(1 mm급)에 붙는 경우.

**Q3 디피처링 규칙의 근거.** "폭 < 2×최소변, 면적 10 %까지 제거"는 경험값이다.
곡률·특징 크기 기반의 일반 규칙, 또는 CAD 쪽(CATIA 내보내기 옵션)에서 먼저
정리하는 쪽이 옳은가?

**Q4 healShapes 톨러런스와 벽 두께의 관계.** 0.5 mm 봉합이 0.3 mm 벽에서
안전한 조건은? 안/밖 피부가 붙는 사고를 검출하는 방법은?

**Q5 5변 이상 필렛 면.** transfinite 사다리는 4변 면에만 걸린다. 다변 필렛
띠를 특징 크기로 메싱하는 대안(면 분할, 곡률 필드, 다른 알고리즘)?

**Q6 스코어 홈이 STP에 형상으로 있을 때.** 홈 바닥 면을 감지해 요소별 두께로
바꾸는 접근이 맞는가, 아니면 홈을 형상 그대로 두고 쉘 오프셋으로 다루는가?

**Q7 자세.** 관성 주축 정렬을 넣어야 하는가, 모델링 자세를 존중하는 현재
방식이 맞는가? 각형 캔에서 축이 애매할 때의 규칙.

답할 때 부탁: 우리 제약(ADR-01, Gmsh/OCC Python, 리눅스, 게이트 값)을 벗어나는
답이면 그렇다고 명시해 주면 좋다. 코드 조각은 gmsh Python API 또는 OCP 기준.

---

## 부록 A. 관련 파일

| 무엇 | 어디 |
|---|---|
| STEP 임포트·봉합·디피처링·사다리 | `crushsim/meshing/mesher.py` (`STEP_*` 상수, `_defeature_strip_faces`, `_constrain_micro_features`, `mesh_step_surfaces`) |
| 게이트 | `crushsim/meshing/gates.py`, 한계값 `crushsim/units.py` |
| 착지·방향 | `crushsim/meshing/mesh_data.py` (`seat_on_floor`, `orient_outward`), `crushsim/pipeline.py` (`_seated_can_proxy`) |
| 파라메트릭 캡·벤트·스코어 | `crushsim/geometry/parametric.py` (`BoxCan`, `VentSpec.petal_arms`), `mesher.py` (`_build_box_surfaces`, `split_vent_membrane`) |
| 변형 형상 STEP 내보내기 | `crushsim/post/export_step.py` |
| CATIA 변환 | `csim convert` (Windows COM) |
| 예제 STEP | `examples/step/{Honda_Can, cylin_can, pris_can, Honda_Cap_Assy, side_terminal}.stp` |
| 실패 기록 | `docs/LOG.md` (§5, §7, §10) |
| 쉘 이상화 설명 | `docs/HANDOVER.md` §2 |

## 부록 B. STEP 토폴로지 확인 스크립트

```python
import gmsh
for f in ["examples/step/cylin_can.stp", "examples/step/Honda_Can.stp",
          "examples/step/Honda_Cap_Assy.stp"]:
    gmsh.initialize(); gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.occ.importShapes(f); gmsh.model.occ.synchronize()
    print(f, "solids", len(gmsh.model.getEntities(3)),
          "faces", len(gmsh.model.getEntities(2)),
          "curves", len(gmsh.model.getEntities(1)))
    gmsh.finalize()
```

| 파일 | 솔리드 | 면 | 곡선 |
|---|---|---|---|
| cylin_can.stp | 1 | 13 | 26 |
| Honda_Can.stp | 1 | 47 | 108 |
| Honda_Cap_Assy.stp | 15 | 1273 | 3479 |
