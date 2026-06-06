"""RTSP 실시간 녹화: 카메라 스트림을 지정 시간만큼 받아 mp4로 저장.

- 재인코딩 없이 -c:v copy 로 저장 (CPU 거의 안 씀, 화질 보존, 화면 타임스탬프 그대로)
- 파일명은 녹화 시작 시각(14자리) → _run_single.py 가 이 시각을 기준시각으로 인식
"""
import sys
import subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
from typing import Optional


def build_url(rtsp: dict) -> str:
    """비밀번호의 특수문자(@, ! 등)를 퍼센트 인코딩해 안전한 RTSP URL 생성."""
    user = quote(str(rtsp["username"]), safe="")
    pw = quote(str(rtsp["password"]), safe="")
    host = rtsp["host"]
    port = rtsp.get("port", 554)
    stream = rtsp.get("stream", "stream1")
    return f"rtsp://{user}:{pw}@{host}:{port}/{stream}"


def record(rtsp: dict) -> Optional[Path]:
    """RTSP를 record_seconds 만큼 녹화. 성공 시 저장 경로, 실패 시 None."""
    url = build_url(rtsp)
    duration = int(rtsp.get("record_seconds", 600))
    out_dir = Path(rtsp.get("output_dir", "recordings"))
    out_dir.mkdir(parents=True, exist_ok=True)

    start = datetime.now()
    out_path = out_dir / f"{start.strftime('%Y%m%d%H%M%S')}.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", str(duration),
        "-c:v", "copy",   # 재인코딩 없음
        "-an",            # 오디오 제거
        "-movflags", "+faststart",
        str(out_path),
    ]

    print(f"🎥 녹화 시작: {start:%Y-%m-%d %H:%M:%S} 부터 {duration}초 → {out_path.name}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 60)
    except subprocess.TimeoutExpired:
        print("녹화 타임아웃 (ffmpeg 무응답)")
        return None

    if r.returncode != 0 or not out_path.exists():
        print(f"녹화 실패: {r.stderr[-400:]}")
        return None

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"✅ 녹화 완료: {out_path.name} ({size_mb:.0f}MB)")
    return out_path


if __name__ == "__main__":
    import yaml
    cfg_path = Path(__file__).with_name("config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    path = record(cfg["rtsp"])
    sys.exit(0 if path else 1)
