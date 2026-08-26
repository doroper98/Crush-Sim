# 폐쇄망(오프라인) 배포

Crush-Sim은 설계상 외부 서비스 의존이 없다: 솔버는 저장소에 핀 고정된 로컬
바이너리(`tools/openradioss/`), UI는 로컬 서버 + 번들 폰트, 뷰어는 자립형
HTML, 텔레메트리 없음. 폐쇄망 반입에 필요한 것은 파이썬 의존성뿐이다.

## 절차

1. **연결된 PC에서** 번들 생성:
   ```bash
   scripts/build_offline_bundle.sh
   ```
   `offline_bundle/wheelhouse/`에 필요한 wheel 전부가 받아진다.
2. 저장소 폴더 전체 + `offline_bundle/`을 매체로 복사해 반입.
3. **폐쇄망 PC에서** (Python >= 3.11만 있으면 됨):
   ```bash
   scripts/install_offline.sh
   . .venv/bin/activate && csim ui
   ```

## 확인 사항

- UI(`csim ui`)와 리포트·뷰어는 네트워크 요청을 하지 않는다. UI 폰트는
  `crushsim/ui/static/fonts/`에 번들되어 있고, 다운로드형 자립 뷰어의 폰트
  링크는 실패 시 시스템 폰트로 자동 대체된다(기능 영향 없음).
- Windows의 CATIA 변환(FR-01)은 로컬 COM이라 폐쇄망과 무관하다.
- OpenRadioss는 오픈소스로 라이선스 서버가 없다.
