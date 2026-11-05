# KEDD CityModel

위경도 범위만 정하면 **지형 + 건물 + 도로·다리 + 녹지·물**이 들어간 도시 클레이 모델을 Blender 파일(`.blend`)로 만들어 주는 앱입니다. Windows와 macOS에서 실행됩니다.

- 건물: 브이월드 **GIS건물통합정보**를 쓰고, 빠진 건물과 높이는 **OpenStreetMap**으로 보완합니다.
- 지형: **Copernicus DEM 30m**를 자동으로 내려받습니다.
- 도로·다리·녹지·물: **OpenStreetMap**(Geofabrik 한국 데이터)을 자동으로 내려받습니다.
- 렌더: 건물과 땅은 흰색이고 녹지·물·도로에만 색이 들어갑니다. AO(앰비언트 오클루전) 합성과 카메라 배치까지 설정된 상태로 나옵니다.

## 필요한 것

1. **Blender 3.3 이상**. 4.x와 5.x에서도 동작하고, 앱이 설치된 Blender를 자동으로 찾습니다.
2. **브이월드 GIS건물통합정보** zip 파일. 로그인이 필요해서 직접 받아야 합니다.
   - 브이월드 → 공간정보 다운로드 → GIS건물통합정보 → 구분 **"전체데이터"**, 원하는 **시도** 선택 → `AL_D010_xx_날짜.zip`
   - 이름이 `CH_D010_...`인 파일은 하루치 변동분이라 쓸 수 없습니다.
   - 범위가 여러 시도에 걸치면 해당 시도 파일을 모두 추가하세요(예: 서울 + 경기).
3. 처음 실행할 때 인터넷 연결이 필요합니다. OSM 약 300MB와 지형 타일을 받고, 이후에는 캐시를 사용합니다.

## 사용법

1. 앱을 실행하고 **범위**를 고릅니다. 프리셋에서 고르거나 경위도를 직접 입력하세요.
2. **건물 zip**을 추가합니다.
3. **작업 폴더**와 **프로젝트 이름**을 확인합니다. Blender 경로는 자동으로 채워집니다.
4. **생성 시작**을 누릅니다. 서울 전체 기준으로 15~30분 걸립니다.
5. `작업폴더/프로젝트이름/프로젝트이름.blend`가 만들어집니다. Blender에서 열고 **F12**로 렌더하세요.

결과 폴더 구성:

```
작업폴더/
  _cache/                 다운로드한 DEM·OSM, 압축 푼 건물 데이터 (재사용)
  프로젝트이름/
    프로젝트이름.blend    ← 결과
    settings.json         이번 실행 설정
    preview_*.png         지형·녹지 미리보기
    data/                 중간 데이터 (npz, landcover.png, scene.json, blender_scene.py)
```

`data` 폴더만 있으면 Blender로 다시 만들 수 있습니다. Blender 버전을 바꿨을 때 유용합니다.

```
blender -b --factory-startup --python data/blender_scene.py -- --data data --out 새파일.blend
```

## 렌더 팁

- **GPU 렌더**: 환경 설정 → 시스템 → Cycles 렌더 장치에서 Metal(Mac) 또는 CUDA/OptiX(NVIDIA)를 켜세요.
- **AO 진하기**: Compositing 탭 → `AO 강도` Math 노드 값을 바꾸세요. 범위는 월드 속성 → 앰비언트 오클루전 → 거리에서 조정합니다.
- **색**: `Ground` 재질에서 녹지와 물(Mix 노드 두 개)을, `Road` 재질에서 도로를, `Clay_Building` 재질에서 건물 색을 바꿉니다.
- **현재 뷰로 렌더**: 보기 → 정렬 뷰 → 활성 카메라를 뷰에 정렬을 한 뒤 F12를 누르세요.

## 개발 / 빌드

```
pip install -r requirements.txt
python -m citymodel                        # GUI
python -m citymodel --config settings.json # GUI 없이 실행
```

배포판 빌드:

- 로컬: `pip install pyinstaller` → `pyinstaller packaging/citymodel.spec --noconfirm`
- GitHub: 태그를 푸시하면(`git tag v1.0.0 && git push --tags`) Actions가 Windows / macOS(Apple Silicon, Intel)용 zip을 만들어 Release에 올립니다.

### macOS 첫 실행

서명되지 않은 앱이라 처음에는 macOS가 실행을 막을 수 있습니다. **앱을 우클릭 → 열기**로 실행하거나, 시스템 설정 → 개인정보 보호 및 보안에서 "그래도 열기"를 누르세요.

## 데이터 출처·라이선스

- GIS건물통합정보: 국토교통부 / 브이월드 (공공누리)
- OpenStreetMap: © OpenStreetMap contributors, ODbL
- Copernicus DEM GLO-30: © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA

## 알려진 한계

- 높이 정보가 없는 건물(원본·OSM 모두)은 기본 7m로 들어갑니다.
- 지형은 30m 해상도이고 산 정상부가 약간 낮아집니다(건물·수목 제거 필터 때문).
- 도로는 지면 위 1.2m에 떠 있는 판입니다(지형과 겹치지 않게 하려는 처리).
- 터널은 표시하지 않습니다.
