"""RTSP 실시간 녹화: 카메라 스트림을 지정 시간만큼 받아 mp4로 저장.

- 재인코딩 없이 -c:v copy 로 저장 (CPU 거의 안 씀, 화질 보존, 화면 타임스탬프 그대로)
- 파일명은 녹화 시작 시각(14자리) → main.py 가 이 시각을 기준시각으로 인식
- RTSP 끊김 시 잔여 시간만큼 최대 3회 재시도, 세그먼트 자동 합치기
"""
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Optional, Tuple, List


MAX_RETRIES = 3
RECONNECT_WAIT = 15    # 재연결 전 대기 시간 (초)
MIN_SEGMENT_SECS = 10  # 이보다 짧은 세그먼트는 실패로 간주


def build_url(rtsp: dict) -> str:
    """비밀번호의 특수문자(@, ! 등)를 퍼센트 인코딩해 안전한 RTSP URL 생성."""
    user = quote(str(rtsp["username"]), safe="")
    pw = quote(str(rtsp["password"]), safe="")
    host = rtsp["host"]
    port = rtsp.get("port", 554)
    stream = rtsp.get("stream", "stream1")
    return f"rtsp://{user}:{pw}@{host}:{port}/{stream}"


def _concat_segments(segments: List[Path], final_path: Path) -> bool:
    """여러 세그먼트를 ffmpeg concat demuxer로 합침. 성공 시 True."""
    filelist = final_path.parent / "_concat_list.txt"
    try:
        filelist.write_text(
            "\n".join(f"file '{s.as_posix()}'" for s in segments),
            encoding="utf-8",
        )
        rc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(filelist), "-c", "copy", str(final_path)],
            capture_output=True, text=True,
        )
        return rc.returncode == 0 and final_path.exists()
    finally:
        filelist.unlink(missing_ok=True)


def record(rtsp: dict) -> Tuple[Optional[Path], List[str]]:
    """RTSP를 record_seconds 만큼 녹화. 끊김 시 잔여 시간 재시도 (최대 3회).

    Returns:
        (path, warnings)
        - path: 녹화 완료 파일 (세그먼트 합산). 전혀 없으면 None.
        - warnings: RTSP 끊김·재시도 경고 메시지 목록.
    """
    url = build_url(rtsp)
    total_duration = int(rtsp.get("record_seconds", 600))
    out_dir = Path(os.path.expandvars(rtsp.get("output_dir", "recordings")))
    out_dir.mkdir(parents=True, exist_ok=True)

    original_start = datetime.now()
    deadline = original_start + timedelta(seconds=total_duration)
    segments: List[Path] = []
    warnings: List[str] = []
    attempt = 0

    while True:
        remaining = int((deadline - datetime.now()).total_seconds())
        if remaining <= MIN_SEGMENT_SECS:
            break

        seg_start = datetime.now()
        out_path = out_dir / f"{seg_start.strftime('%Y%m%d%H%M%S')}.mp4"
        label = f"재시도 {attempt}/{MAX_RETRIES}" if attempt else "시작"
        print(f"[{label}] {seg_start:%H:%M:%S} ~ +{remaining}초 → {out_path.name}")

        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp",
                 "-i", url,
                 "-t", str(remaining),
                 "-c:v", "copy", "-an", "-movflags", "+faststart",
                 str(out_path)],
                capture_output=True, text=True,
                timeout=remaining + 60,
            )
        except subprocess.TimeoutExpired:
            warn = f"[{seg_start:%H:%M}] ffmpeg 무응답"
            print(f"  {warn}")
            warnings.append(warn)
            if attempt >= MAX_RETRIES:
                warnings.append(f"재시도 {MAX_RETRIES}회 소진 — 녹화 조기 종료")
                break
            attempt += 1
            time.sleep(RECONNECT_WAIT)
            continue

        seg_elapsed = (datetime.now() - seg_start).total_seconds()
        if out_path.exists() and out_path.stat().st_size > 0 and seg_elapsed >= MIN_SEGMENT_SECS:
            segments.append(out_path)
            print(f"  세그먼트 {len(segments)}: {out_path.name} ({seg_elapsed:.0f}초/{remaining}초)")
        else:
            out_path.unlink(missing_ok=True)

        # 정상 종료 확인 (deadline에 도달했거나 60초 이내 남음)
        remaining_after = (deadline - datetime.now()).total_seconds()
        if remaining_after <= 60:
            break

        # deadline보다 일찍 끝났으면 RTSP 끊김으로 판단
        warn = f"[{datetime.now():%H:%M}] RTSP 끊김 — 잔여 {int(remaining_after / 60)}분"
        print(f"  {warn}")
        warnings.append(warn)

        if attempt >= MAX_RETRIES:
            warnings.append(f"재시도 {MAX_RETRIES}회 소진 — 녹화 조기 종료")
            break

        attempt += 1
        print(f"  {RECONNECT_WAIT}초 후 재연결 시도 ({attempt}/{MAX_RETRIES})...")
        time.sleep(RECONNECT_WAIT)

    if not segments:
        return None, warnings

    final_path = out_dir / f"{original_start.strftime('%Y%m%d%H%M%S')}.mp4"

    if len(segments) == 1:
        segments[0].rename(final_path)
    else:
        print(f"{len(segments)}개 세그먼트 합치는 중...")
        ok = _concat_segments(segments, final_path)
        if ok:
            for s in segments:
                s.unlink(missing_ok=True)
        else:
            # concat 실패 시 첫 세그먼트로 대체
            warnings.append("세그먼트 합치기 실패 — 첫 세그먼트만 사용")
            for s in segments[1:]:
                s.unlink(missing_ok=True)
            if segments[0].exists():
                segments[0].rename(final_path)
            else:
                return None, warnings

    if not final_path.exists():
        return None, warnings

    size_mb = final_path.stat().st_size / 1024 / 1024
    print(f"녹화 완료: {final_path.name} ({size_mb:.0f}MB)")
    return final_path, warnings


if __name__ == "__main__":
    import yaml
    cfg_path = Path(__file__).with_name("config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    path, warns = record(cfg["rtsp"])
    for w in warns:
        print(f"경고: {w}")
    sys.exit(0 if path else 1)
