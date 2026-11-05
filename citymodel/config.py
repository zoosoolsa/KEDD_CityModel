"""설정값 (GUI/CLI 공통). JSON 으로 저장·불러오기."""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List

APP_NAME = "KEDD CityModel"
APP_VERSION = "2.1.0"

# 대략적인 시/도 범위 (경도 min, 위도 min, 경도 max, 위도 max)
PRESETS = {
    "서울특별시": (126.68, 37.37, 127.27, 37.76),
    "서울 도심 (종로·중구·용산)": (126.94, 37.52, 127.03, 37.59),
    "강남 (서초·강남·송파)": (126.98, 37.45, 127.15, 37.54),
    "부산광역시": (128.83, 35.03, 129.30, 35.32),
    "대구광역시": (128.45, 35.75, 128.75, 35.99),
    "인천광역시 (육지부)": (126.55, 37.35, 126.80, 37.60),
    "광주광역시": (126.75, 35.07, 127.00, 35.25),
    "대전광역시": (127.28, 36.23, 127.53, 36.49),
    "울산광역시 (도심)": (129.20, 35.48, 129.45, 35.62),
    "세종특별자치시 (도심)": (127.22, 36.46, 127.33, 36.53),
    "수원시": (126.93, 37.22, 127.08, 37.33),
}

# 높이 데이터가 빠진 대표 랜드마크 (범위 안에 있을 때만 사용)
DEFAULT_LANDMARKS = [
    {"name": "롯데월드타워", "lon": 127.1025, "lat": 37.5126, "height": 555, "r0": 36, "r1": 12, "square": 0.6},
    {"name": "N서울타워", "lon": 126.98820, "lat": 37.55118, "height": 236, "r0": 9, "r1": 4, "square": 0.0},
]


@dataclass
class Settings:
    # 범위 (WGS84)
    lon_min: float = 126.68
    lat_min: float = 37.37
    lon_max: float = 127.27
    lat_max: float = 37.76
    # 입력
    building_lib: str = os.path.join(os.path.expanduser("~"), "CityModel", "건물데이터")  # 시도별 GIS건물통합정보 zip 폴더
    building_zips: List[str] = field(default_factory=list)   # (선택) 실행 시 라이브러리로 가져올 zip
    camera_url: str = ""         # 웹 지도 URL (구글/구글어스/네이버/카카오/브이월드) → 카메라
    osm_pbf: str = ""            # 비우면 자동 다운로드 (Geofabrik south-korea)
    dem_files: List[str] = field(default_factory=list)       # 비우면 Copernicus 자동 다운로드
    work_dir: str = os.path.join(os.path.expanduser("~"), "CityModel")
    project_name: str = "seoul"
    blender_path: str = ""       # 비우면 자동 탐지
    # 처리 옵션
    exag: float = 1.6            # 지형 수직 과장
    dem_res: float = 25.0        # 지형 격자 (m, 타일 4km 의 약수)
    osm_max_age_days: int = 90   # OSM 자동 갱신 주기 (일)
    api_heights: bool = True     # 브이월드 API(도로명주소건물 층수)로 높이 보완
    lc_res: float = 2.0          # 지면 텍스처(녹지·물·도로) 해상도 (m)
    min_building_area: float = 15.0
    default_height: float = 7.0
    use_osm_buildings: bool = True
    include_minor_roads: bool = True
    bridge_end_offset: float = 0.15   # 다리 양끝이 지면에 닿는 높이 (m)
    landmarks: list = field(default_factory=lambda: [dict(x) for x in DEFAULT_LANDMARKS])
    # 렌더/색
    col_building: tuple = (0.88, 0.88, 0.88)
    col_ground: tuple = (0.85, 0.85, 0.85)
    col_green: tuple = (0.16, 0.34, 0.12)
    col_water: tuple = (0.05, 0.22, 0.55)
    col_road: tuple = (0.85, 0.50, 0.18)
    ao_distance: float = 40.0
    ao_strength: float = 0.75
    render_w: int = 3000
    render_h: int = 2000
    samples: int = 96
    thumbnails: bool = True          # 카메라 썸네일 (프로젝트/thumbs, .blend 에도 포함)

    @property
    def bbox(self):
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)

    @property
    def project_dir(self):
        return os.path.join(self.work_dir, "projects", self.project_name)

    @property
    def cache_dir(self):
        return os.path.join(self.work_dir, "_cache")

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, tuple(v) if k.startswith("col_") else v)
        return s

    def validate(self):
        errs = []
        if not (self.lon_min < self.lon_max and self.lat_min < self.lat_max):
            errs.append("범위의 최소값이 최대값보다 작아야 합니다.")
        if not (124 <= self.lon_min <= 132 and 33 <= self.lat_min <= 39):
            errs.append("범위가 한국 밖입니다.")
        for p in self.building_zips:
            if not os.path.exists(p):
                errs.append(f"파일이 없습니다: {p}")
        if self.camera_url.strip():
            try:
                from .webcam import parse_url
                parse_url(self.camera_url)
            except Exception as e:
                errs.append(f"카메라 URL 해석 실패: {e}")
        if not self.project_name.strip():
            errs.append("프로젝트 이름을 입력하세요.")
        return errs
