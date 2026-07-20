"""OpenStreetMap 靜態縮圖與地圖 Tile 服務

提供兩種服務:
1. 取得指定座標的靜態地圖縮圖 (用於照片疊加)
2. 產生 Leaflet 地圖 HTML (用於地圖顯示元件)

使用 OpenStreetMap 免費圖磚，無需 API Key。
"""

from __future__ import annotations

import os
import tempfile as _tempfile
import threading
import time
from collections import OrderedDict
from urllib.parse import urlencode

import requests

from dashcam_player.utils.log import get_logger

log = get_logger()

# OSM 靜態地圖 API 基底
_STATIC_MAP_URL = "https://staticmap.openstreetmap.de/staticmap.php"

# 記憶體快取（LRU）：避免同一 session 內重複 HTTP 請求
_STATIC_MAP_MEMORY_CACHE: OrderedDict[str, bytes] = OrderedDict()
_MAX_MEMORY_CACHE_ENTRIES = 256
_CACHE_LOCK = threading.Lock()  # 執行緒安全：批次截圖與主執行緒並行存取

# OSM Tile 快取目錄 (預設使用系統暫存)
_TILE_CACHE_DIR: str | None = os.path.join(_tempfile.gettempdir(), "dashcam_osm_cache")


_MAX_CACHE_AGE_DAYS = 30
_MAX_CACHE_FILES = 500


def cleanup_cache(force: bool = False) -> int:
    """清理過期 Tile 快取，移除超過 _MAX_CACHE_AGE_DAYS 或超過 _MAX_CACHE_FILES 的舊檔案

    Args:
        force: 若為 True，完全清空快取

    Returns:
        刪除的檔案數量
    """
    if not _TILE_CACHE_DIR or not os.path.isdir(_TILE_CACHE_DIR):
        return 0
    try:
        entries = []
        for fname in os.listdir(_TILE_CACHE_DIR):
            fpath = os.path.join(_TILE_CACHE_DIR, fname)
            if os.path.isfile(fpath):
                entries.append((fpath, os.path.getmtime(fpath)))
        if not entries:
            return 0

        if force:
            for fpath, _ in entries:
                try:
                    os.remove(fpath)
                except OSError:
                    pass
            return len(entries)

        now = time.time()
        cutoff = now - _MAX_CACHE_AGE_DAYS * 86400

        # 第一遍：移除過期檔案
        removed = 0
        remaining = []
        for fpath, mtime in entries:
            if mtime < cutoff:
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError:
                    remaining.append((fpath, mtime))
            else:
                remaining.append((fpath, mtime))

        # 第二遍：若仍超過上限，刪除最舊的
        if len(remaining) > _MAX_CACHE_FILES:
            remaining.sort(key=lambda x: x[1])  # 按修改時間排序
            excess = len(remaining) - _MAX_CACHE_FILES
            for fpath, _ in remaining[:excess]:
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError:
                    pass

        if removed > 0:
            log.info(f"OSM 快取清理: 移除 {removed} 個檔案")
        return removed
    except (OSError, PermissionError) as e:
        log.warning(f"OSM 快取清理失敗: {e}")
        return 0


def set_cache_dir(cache_dir: str) -> None:
    """設定 Tile 快取目錄 (減少重複請求)"""
    global _TILE_CACHE_DIR
    _TILE_CACHE_DIR = cache_dir
    os.makedirs(cache_dir, exist_ok=True)


# ------------------------------------------------------------------
# 靜態縮圖 (用於照片疊加)
# ------------------------------------------------------------------


def get_static_map(
    lat: float,
    lon: float,
    zoom: int = 15,
    width: int = 256,
    height: int = 256,
    maptype: str = "mapnik",
) -> bytes | None:
    """取得指定座標的 OSM 靜態地圖縮圖

    Args:
        lat: 緯度
        lon: 經度
        zoom: 縮放層級 (0-19)
        width: 圖片寬度
        height: 圖片高度
        maptype: 地圖類型 (mapnik, cycle, transport)

    Returns:
        PNG 圖片二進制資料，失敗時回傳 None
    """
    params = {
        "center": f"{lat},{lon}",
        "zoom": str(zoom),
        "size": f"{width}x{height}",
        "maptype": maptype,
    }

    # 檢查記憶體快取（最快）
    cache_key = f"osm_{lat:.4f}_{lon:.4f}_{zoom}_{width}x{height}_{maptype}.png"
    with _CACHE_LOCK:
        if cache_key in _STATIC_MAP_MEMORY_CACHE:
            _STATIC_MAP_MEMORY_CACHE.move_to_end(cache_key)
            return _STATIC_MAP_MEMORY_CACHE[cache_key]

    # 檢查磁碟快取
    cached = _read_cache(cache_key)
    if cached:
        with _CACHE_LOCK:
            _STATIC_MAP_MEMORY_CACHE[cache_key] = cached
            _trim_memory_cache()
        return cached

    try:
        url = f"{_STATIC_MAP_URL}?{urlencode(params)}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "DashcamPlayer/1.0"})
        resp.raise_for_status()

        data = resp.content
        with _CACHE_LOCK:
            _STATIC_MAP_MEMORY_CACHE[cache_key] = data
            _trim_memory_cache()
        _write_cache(cache_key, data)
        return data

    except requests.RequestException as e:
        log.warning(f"OSM 靜態地圖請求失敗 ({lat}, {lon}): {e}")
        return None


# ------------------------------------------------------------------
# Leaflet HTML 生成 (用於地圖顯示元件)
# ------------------------------------------------------------------


def generate_map_html(
    tracks: list[dict],
    center: tuple[float, float] | None = None,
    zoom: int = 15,
    provider: str = "osm",
    google_maps_key: str = "",
) -> str:
    """產生 Leaflet.js 地圖 HTML

    Args:
        tracks: [{'lat': float, 'lon': float, 'time': float}, ...]
        center: (lat, lon) 地圖中心點，預設使用軌跡中心
        zoom: 縮放層級
        provider: 地圖來源 ('osm' 或 'google')
        google_maps_key: Google Maps API Key

    Returns:
        HTML 字串
    """
    if not tracks:
        return _empty_map_html()

    if center is None:
        lats = [t["lat"] for t in tracks]
        lons = [t["lon"] for t in tracks]
        center = ((max(lats) + min(lats)) / 2, (max(lons) + min(lons)) / 2)

    import json

    tracks_json = json.dumps(tracks)
    center_lat, center_lon = center

    # Tile layer URL 與 attribution
    if provider == "google" and google_maps_key:
        tile_url = f"https://mt1.google.com/vt/lyrs=m&x={{x}}&y={{y}}&z={{z}}&key={google_maps_key}"
        attribution = "Google Maps"
    else:
        tile_url = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>GPS 軌跡地圖</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; }}
  html, body, #map {{ width: 100%; height: 100%; }}
</style>
</head>
<body>
<div id="map"></div>
<script>
var tracks = {tracks_json};
var map = L.map('map', {{ zoomControl: true, attributionControl: true }}).setView([{center_lat}, {center_lon}], {zoom});
var tileLayer = L.tileLayer('{tile_url}', {{ attribution: '{attribution}', maxZoom: 19 }}).addTo(map);

// 圖磚載入失敗時記錄到 console，搭配 QTWEBENGINE_REMOTE_DEBUGGING 可直接看到失敗網址與原因
var _tileErrorCount = 0;
tileLayer.on('tileerror', function(e) {{
  _tileErrorCount++;
  console.error('[map] tile 載入失敗 #' + _tileErrorCount + ': ' + (e.tile && e.tile.src));
}});
tileLayer.on('tileload', function() {{
  if (_tileErrorCount === 0) console.log('[map] tile 載入成功');
}});

// 繪製軌跡線
var latlngs = tracks.map(function(t) {{ return [t.lat, t.lon]; }});
var polyline = L.polyline(latlngs, {{ color: '#2196F3', weight: 3, opacity: 0.8 }}).addTo(map);
map.fitBounds(polyline.getBounds().pad(0.1));

// 起點 (綠色)
var start = tracks[0];
L.circleMarker([start.lat, start.lon], {{ color: '#4CAF50', radius: 6 }}).addTo(map)
 .bindPopup('起點');

// 終點 (紅色)
var end = tracks[tracks.length - 1];
L.circleMarker([end.lat, end.lon], {{ color: '#F44336', radius: 6 }}).addTo(map)
 .bindPopup('終點');

// 當前位置標記 (藍色圓點)
var marker = L.circleMarker([start.lat, start.lon], {{
  color: '#1565C0', fillColor: '#2196F3', fillOpacity: 0.9, radius: 8, weight: 3
}}).addTo(map);
marker.bindPopup('當前位置');

// 提供 Python 端呼叫更新接口
function updatePosition(lat, lon) {{
  marker.setLatLng([lat, lon]);
  // A5: 僅當新位置超出目前視圖邊界時才平移，避免播放時地圖持續晃動
  var ll = L.latLng(lat, lon);
  if (!map.getBounds().contains(ll)) {{
    map.panTo([lat, lon], {{ animate: true, duration: 0.3 }});
  }}
}}
</script>
</body>
</html>"""
    return html


def _empty_map_html() -> str:
    """無 GPS 資料時的佔位地圖 HTML"""
    return """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>GPS 軌跡地圖</title>
<style>
  * { margin: 0; padding: 0; }
  html, body, #map { width: 100%; height: 100%; background: #f5f5f5; display: flex; align-items: center; justify-content: center; font-family: sans-serif; color: #999; }
</style>
</head>
<body>
<div id="map">無 GPS 軌跡資料</div>
</body>
</html>"""


# ------------------------------------------------------------------
# 快取
# ------------------------------------------------------------------


def _trim_memory_cache() -> None:
    while len(_STATIC_MAP_MEMORY_CACHE) > _MAX_MEMORY_CACHE_ENTRIES:
        _STATIC_MAP_MEMORY_CACHE.popitem(last=False)


# 呼叫端須已持有 _CACHE_LOCK


def _read_cache(key: str) -> bytes | None:
    """讀取 Tile 快取"""
    if not _TILE_CACHE_DIR:
        return None
    path = os.path.join(_TILE_CACHE_DIR, key)
    if os.path.isfile(path):
        # 快取有效期需與 cleanup_cache 的 _MAX_CACHE_AGE_DAYS 一致
        age = time.time() - os.path.getmtime(path)
        if age < _MAX_CACHE_AGE_DAYS * 86400:
            with open(path, "rb") as f:
                return f.read()
    return None


def _write_cache(key: str, data: bytes) -> None:
    """寫入 Tile 快取"""
    if not _TILE_CACHE_DIR:
        return
    path = os.path.join(_TILE_CACHE_DIR, key)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        log.warning(f"OSM 快取寫入失敗: {e}")


def init_osm_cache() -> None:
    """非同步清理過期的 OSM 快取（啟動時由 main.py 呼叫一次）。"""
    import threading

    threading.Thread(target=cleanup_cache, daemon=True).start()
