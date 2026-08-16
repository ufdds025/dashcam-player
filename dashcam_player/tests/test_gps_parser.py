from __future__ import annotations

from dashcam_player.utils.gps_parser import GPSParser, GPSPoint

# ------------------------------------------------------------------
# NMEA _parse_nmea_line
# ------------------------------------------------------------------


def test_parse_nmea_line_gga() -> None:
    line = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    point = GPSParser._parse_nmea_line(line)
    assert point is not None
    assert 48.11 < point.lat < 48.12
    assert 11.51 < point.lon < 11.52
    assert point.altitude == 545.4


def test_parse_nmea_line_rmc() -> None:
    line = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W,A*6A"
    point = GPSParser._parse_nmea_line(line)
    assert point is not None
    assert 48.11 < point.lat < 48.12
    assert 11.51 < point.lon < 11.52
    assert point.speed == 22.4


def test_parse_nmea_line_empty_alt() -> None:
    line = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,,M,,M,,*00"
    point = GPSParser._parse_nmea_line(line)
    assert point is not None
    assert point.altitude is None


def test_parse_nmea_line_no_fix() -> None:
    line = "$GPGGA,123519,4807.038,N,01131.000,E,0,00,,,,,,*00"
    point = GPSParser._parse_nmea_line(line)
    assert point is None


def test_parse_nmea_line_rmc_void() -> None:
    line = "$GPRMC,123519,V,4807.038,N,01131.000,E,,,230394,,*00"
    point = GPSParser._parse_nmea_line(line)
    assert point is None


def test_parse_nmea_line_gn_prefix() -> None:
    line = "$GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,100.0,M,,M,,*00"
    point = GPSParser._parse_nmea_line(line)
    assert point is not None
    assert point.altitude == 100.0


def test_parse_nmea_line_short_parts() -> None:
    line = "$GPGGA,123519,4807"
    point = GPSParser._parse_nmea_line(line)
    assert point is None


def test_parse_nmea_line_invalid_coord() -> None:
    line = "$GPGGA,123519,ABCD,N,XXXX,E,1,08,,,,,,*00"
    point = GPSParser._parse_nmea_line(line)
    assert point is None


# ------------------------------------------------------------------
# GPX _parse_gpx
# ------------------------------------------------------------------


GPX_WITH_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg>
      <trkpt lat="25.0330" lon="121.5654">
        <ele>10.0</ele>
        <time>2024-01-15T10:00:00Z</time>
      </trkpt>
      <trkpt lat="25.0340" lon="121.5664">
        <ele>15.0</ele>
        <time>2024-01-15T10:00:10Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def test_parse_gpx_with_time(tmp_path) -> None:
    gpx_path = tmp_path / "test.gpx"
    gpx_path.write_text(GPX_WITH_TIME, encoding="utf-8")
    tracks, time_base = GPSParser._parse_gpx(str(gpx_path))
    assert len(tracks) == 2
    assert 25.0330 <= tracks[0].lat <= 25.0340
    assert tracks[0].altitude == 10.0
    assert tracks[1].altitude == 15.0
    assert tracks[0].time == 0.0  # first point → relative 0
    assert abs(tracks[1].time - 10.0) < 0.01  # second point 10s after base
    assert time_base is not None


GPX_WITHOUT_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg>
      <trkpt lat="25.0330" lon="121.5654">
        <time>2024-01-15T10:00:00Z</time>
      </trkpt>
      <trkpt lat="25.0340" lon="121.5664">
        <!-- no time element -->
      </trkpt>
      <trkpt lat="25.0350" lon="121.5674">
        <time>2024-01-15T10:00:20Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def test_parse_gpx_skip_no_time(tmp_path) -> None:
    gpx_path = tmp_path / "test_no_time.gpx"
    gpx_path.write_text(GPX_WITHOUT_TIME, encoding="utf-8")
    tracks, _ = GPSParser._parse_gpx(str(gpx_path))
    assert len(tracks) == 2  # middle point skipped
    assert tracks[0].lat == 25.0330
    assert tracks[1].lat == 25.0350
    assert abs(tracks[0].time - 0.0) < 0.01
    assert abs(tracks[1].time - 20.0) < 0.01


def test_parse_gpx_empty(tmp_path) -> None:
    xml = '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg/></trk></gpx>'
    gpx_path = tmp_path / "empty.gpx"
    gpx_path.write_text(xml, encoding="utf-8")
    tracks, time_base = GPSParser._parse_gpx(str(gpx_path))
    assert tracks == []
    assert time_base is None


# ------------------------------------------------------------------
# SRT _parse_srt
# ------------------------------------------------------------------


SRT_STANDARD = """1
00:00:01,000 --> 00:00:02,000
GPS: 25.0330, 121.5654

2
00:00:11,000 --> 00:00:12,000
GPS: 25.0340, 121.5664
"""


def test_parse_srt_standard(tmp_path) -> None:
    srt_path = tmp_path / "test.srt"
    srt_path.write_text(SRT_STANDARD, encoding="utf-8")
    tracks = GPSParser._parse_srt(str(srt_path))
    assert len(tracks) == 2
    assert tracks[0].lat == 25.0330
    assert tracks[0].lon == 121.5654
    assert abs(tracks[0].time - 1.0) < 0.01
    assert tracks[1].lat == 25.0340
    assert abs(tracks[1].time - 11.0) < 0.01


SRT_CRLF = "1\r\n00:00:01,000 --> 00:00:02,000\r\nGPS: 25.0330, 121.5654\r\n\r\n"


def test_parse_srt_crlf(tmp_path) -> None:
    srt_path = tmp_path / "test_crlf.srt"
    srt_path.write_bytes(SRT_CRLF.encode("utf-8"))
    tracks = GPSParser._parse_srt(str(srt_path))
    assert len(tracks) == 1
    assert tracks[0].lat == 25.0330
    assert abs(tracks[0].time - 1.0) < 0.01


def test_parse_srt_no_gps(tmp_path) -> None:
    content = "1\n00:00:01,000 --> 00:00:02,000\nNo GPS data here\n\n"
    srt_path = tmp_path / "test_no_gps.srt"
    srt_path.write_text(content, encoding="utf-8")
    tracks = GPSParser._parse_srt(str(srt_path))
    assert tracks == []


def test_parse_srt_invalid_coord(tmp_path) -> None:
    content = "1\n00:00:01,000 --> 00:00:02,000\nGPS: 999.0, 999.0\n\n"
    srt_path = tmp_path / "test_invalid.srt"
    srt_path.write_text(content, encoding="utf-8")
    tracks = GPSParser._parse_srt(str(srt_path))
    assert tracks == []  # lat/lon out of valid range → skipped


# ------------------------------------------------------------------
# get_gps_at_time interpolation
# ------------------------------------------------------------------


def test_get_gps_at_time_interpolate() -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = [
        GPSPoint(lat=0.0, lon=0.0, time=0.0),
        GPSPoint(lat=10.0, lon=10.0, time=10.0),
    ]
    parser._has_gps = True

    result = parser.get_gps_at_time(5.0)
    assert result is not None
    assert result == (5.0, 5.0)


def test_get_gps_at_time_before_first() -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = [
        GPSPoint(lat=1.0, lon=1.0, time=1.0),
        GPSPoint(lat=2.0, lon=2.0, time=2.0),
    ]
    parser._has_gps = True
    result = parser.get_gps_at_time(0.0)
    assert result == (1.0, 1.0)


def test_get_gps_at_time_after_last() -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = [
        GPSPoint(lat=1.0, lon=1.0, time=1.0),
        GPSPoint(lat=2.0, lon=2.0, time=2.0),
    ]
    parser._has_gps = True
    result = parser.get_gps_at_time(5.0)
    assert result == (2.0, 2.0)


def test_get_gps_at_time_no_data() -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = []
    parser._has_gps = False
    parser._extraction_attempted = False
    result = parser.get_gps_at_time(5.0)
    assert result is None


def test_get_gps_at_time_single_point() -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = [GPSPoint(lat=42.0, lon=-71.0, time=0.0)]
    parser._has_gps = True
    result = parser.get_gps_at_time(5.0)
    assert result == (42.0, -71.0)


# ------------------------------------------------------------------
# to_gpx (ElementTree)
# ------------------------------------------------------------------


def test_to_gpx_export(tmp_path) -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._tracks = [
        GPSPoint(lat=25.0330, lon=121.5654, time=0.0, altitude=10.0),
        GPSPoint(lat=25.0340, lon=121.5664, time=10.0, altitude=15.0),
    ]
    parser._has_gps = True
    parser._time_base = 1705300000.0

    out = tmp_path / "export.gpx"
    ok = parser.to_gpx(str(out))
    assert ok
    assert out.exists()

    content = out.read_text(encoding="utf-8")
    assert content.startswith("<?xml")
    assert "<gpx" in content
    assert 'lat="25.033"' in content
    assert "<ele>" in content
    assert "<time>" in content


def test_to_gpx_no_gps(tmp_path) -> None:
    parser = GPSParser.__new__(GPSParser)
    parser._has_gps = False
    out = tmp_path / "empty.gpx"
    assert not parser.to_gpx(str(out))


def test_interpolate_gps_bisect_midpoint() -> None:
    from dashcam_player.utils.gps_parser import interpolate_gps

    points = [
        GPSPoint(lat=0.0, lon=0.0, time=0.0),
        GPSPoint(lat=10.0, lon=10.0, time=10.0),
        GPSPoint(lat=20.0, lon=20.0, time=20.0),
    ]
    result = interpolate_gps(5.0, points)
    assert result == (5.0, 5.0)


def test_interpolate_gps_bisect_large_gap() -> None:
    from dashcam_player.utils.gps_parser import interpolate_gps

    points = [
        GPSPoint(lat=1.0, lon=2.0, time=0.0),
        GPSPoint(lat=3.0, lon=4.0, time=100.0),
    ]
    result = interpolate_gps(50.0, points)
    assert result is not None
    assert 1.5 < result[0] < 2.5


# ------------------------------------------------------------------
# A4 重構：GPSDetector / GPSTrackExtractor 策略拆分
# ------------------------------------------------------------------

from dashcam_player.utils.gps_parser import GPSDetector, GPSTrackExtractor


def test_detector_uses_external_file(tmp_path) -> None:
    # 建立同名 .gpx 外部檔案，detector 應偵測到
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"dummy")
    gpx = tmp_path / "clip.gpx"
    gpx.write_text(
        "<gpx><trk><trkseg>"
        '<trkpt lat="24.0" lon="120.0"><time>2024-01-01T00:00:00Z</time></trkpt>'
        '<trkpt lat="24.1" lon="120.1"><time>2024-01-01T00:00:01Z</time></trkpt>'
        "</trkseg></trk></gpx>"
    )
    parser = GPSParser(str(video))
    detector = GPSDetector(str(video), parser, skip_ocr=True)
    assert detector.detect() is True


def test_detector_skips_ocr_when_requested() -> None:
    # 無外部/內嵌檔，skip_ocr=True 時不觸發 OCR（應快速回傳 False）
    import os
    import tempfile

    d = tempfile.mkdtemp()
    vp = os.path.join(d, "nocsv.mp4")
    open(vp, "wb").write(b"x")
    parser = GPSParser(vp)
    detector = GPSDetector(vp, parser, skip_ocr=True)
    # 無 ffprobe 相關 metadata，detector 不應長時間阻塞
    assert detector.detect() is False


def test_extractor_fallback_chain(tmp_path) -> None:
    # external 優先；無 external 時走 embedded（此處皆無 → 空 list）
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    parser = GPSParser(str(video))
    extractor = GPSTrackExtractor(str(video), parser)
    # 無任何來源，提取結果為空（不拋例外）
    assert extractor.extract() == []


# ------------------------------------------------------------------
# GPS 飄移過濾 (filter_gps_drift)
# ------------------------------------------------------------------

from dashcam_player.utils.gps_parser import (
    _haversine_m,
    filter_gps_drift,
)


def _pts(coords: list[tuple[float, float, float]]) -> list[GPSPoint]:
    return [GPSPoint(lat=lat, lon=lon, time=t) for lat, lon, t in coords]


def test_haversine_m_known_distance() -> None:
    # 緯度 25° 處 1 度經度 ≈ 111.32 * cos(25°) ≈ 100.9 km
    d = _haversine_m(25.0, 121.0, 25.0, 122.0)
    assert 100000 < d < 102000


def test_haversine_m_same_point() -> None:
    assert _haversine_m(25.0, 121.0, 25.0, 121.0) == 0.0


def test_filter_gps_drift_removes_spike() -> None:
    # 中間點 (24.44, 120.46) 與前後差距約 11 km，應被丟棄
    points = _pts([
        (24.3385, 120.4559, 0.0),
        (24.4385, 120.4559, 1.0),  # 飄移點
        (24.3395, 120.4569, 2.0),
    ])
    result = filter_gps_drift(points, max_distance_m=2000.0)
    assert len(result) == 2
    assert result[0].time == 0.0
    assert result[1].time == 2.0


def test_filter_gps_drift_keeps_movement() -> None:
    # 連續大跳躍（真實快速移動）應全部保留
    points = _pts([
        (24.0, 120.0, 0.0),
        (24.5, 120.0, 1.0),
        (25.0, 120.0, 2.0),
    ])
    result = filter_gps_drift(points, max_distance_m=2000.0)
    assert len(result) == 3


def test_filter_gps_drift_keeps_endpoints() -> None:
    # 端點即使遠離相鄰點也保留
    points = _pts([
        (24.9, 120.9, 0.0),  # 端點飄移
        (24.3, 120.4, 1.0),
        (24.3395, 120.4569, 2.0),
    ])
    result = filter_gps_drift(points, max_distance_m=2000.0)
    assert len(result) == 3


def test_filter_gps_drift_disabled_threshold() -> None:
    points = _pts([
        (24.3385, 120.4559, 0.0),
        (24.4385, 120.4559, 1.0),
        (24.3395, 120.4569, 2.0),
    ])
    # max_distance <= 0 → 不濾除
    assert len(filter_gps_drift(points, max_distance_m=0.0)) == 3


def test_filter_gps_drift_short_track() -> None:
    assert len(filter_gps_drift(_pts([(1.0, 1.0, 0.0)]), 100.0)) == 1
    assert len(filter_gps_drift(_pts([(1.0, 1.0, 0.0), (1.1, 1.1, 1.0)]), 100.0)) == 2
