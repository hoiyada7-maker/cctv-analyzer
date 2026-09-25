"""전체 동영상 -> 모션 감지 -> 클립 추출 -> Videos/cctv/clip/ 저장.

Gemini 분석 없이 모션 구간만 잘라서 저장합니다.

사용법:
  python classify_clips.py          # Videos/cctv 의 모든 동영상 처리
  python classify_clips.py <경로>   # 지정 파일만 처리
"""

import os
import re
import sys
import shutil
import winsound
import yaml
from pathlib import Path
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from motion_detector import MotionDetector, extract_clip, MotionSegment


def parse_base_time(name: str) -> datetime:
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", name)
    return datetime(*map(int, m.groups())) if m else datetime.now()


def process_video(video_path: Path, clip_dir: Path, cfg: dict) -> int:
    """단일 동영상에서 모션 클립 추출. 저장된 클립 수 반환."""
    base_time = parse_base_time(video_path.name)
    date_str = base_time.strftime("%Y%m%d")
    print(f"\n  파일: {video_path.name}  ({video_path.stat().st_size // 1024 // 1024} MB)")

    # 모션 감지
    m = cfg.get("motion", {})
    detector = MotionDetector(
        min_area_ratio=m.get("min_area_ratio", 0.005),
        min_duration=m.get("min_duration", 2.0),
        merge_gap=m.get("merge_gap", 10.0),
        sample_fps=m.get("sample_fps", 2.0),
    )
    segments = detector.detect(str(video_path))
    if not segments:
        print("  → 모션 없음")
        return 0

    # 인접 구간 병합
    merged = [segments[0]]
    for seg in segments[1:]:
        last = merged[-1]
        if seg.start_sec - last.end_sec <= 15.0:
            merged[-1] = MotionSegment(
                start_sec=last.start_sec,
                end_sec=seg.end_sec,
                peak_intensity=max(last.peak_intensity, seg.peak_intensity),
                avg_intensity=(last.avg_intensity + seg.avg_intensity) / 2,
            )
        else:
            merged.append(seg)
    print(f"  모션 구간: {len(merged)}개")

    saved = 0
    work_dir = BASE / "work" / "clip_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    for idx, seg in enumerate(merged, 1):
        ts_s = (base_time + timedelta(seconds=seg.start_sec)).strftime("%H%M%S")
        ts_e = (base_time + timedelta(seconds=seg.end_sec)).strftime("%H%M%S")
        clip_name = f"{date_str}_{ts_s}_{ts_e}.mp4"
        dest = clip_dir / clip_name

        # 이미 있으면 건너뜀
        if dest.exists():
            print(f"  [{idx:02d}/{len(merged):02d}] {clip_name} 이미 존재 — 건너뜀")
            continue

        tmp_path = work_dir / f"tmp_{idx:03d}.mp4"
        ok = extract_clip(str(video_path), str(tmp_path), seg.start_sec, seg.end_sec,
                          scale="640:360", fps=2)
        if ok and tmp_path.exists():
            shutil.move(str(tmp_path), str(dest))
            saved += 1
            print(f"  [{idx:02d}/{len(merged):02d}] {clip_name}  ({seg.duration:.0f}초)")
        else:
            tmp_path.unlink(missing_ok=True)
            print(f"  [{idx:02d}/{len(merged):02d}] 추출 실패: {clip_name}")

    # 임시 폴더 정리
    try:
        work_dir.rmdir()
    except Exception:
        pass

    return saved


def main():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    cctv_dir = Path(os.path.expandvars(cfg["rtsp"]["output_dir"]))
    clip_dir = cctv_dir / "clip"
    clip_dir.mkdir(parents=True, exist_ok=True)

    # 처리할 동영상 목록 결정
    vid_pattern = re.compile(r"^\d{14}\.mp4$")
    if len(sys.argv) > 1:
        videos = [Path(sys.argv[1])]
    else:
        # cctv 폴더 + 상위 Videos 폴더 모두 탐색
        search_dirs = [cctv_dir.parent, cctv_dir]
        seen = set()
        candidates = []
        for d in search_dirs:
            for p in d.glob("*.mp4"):
                if vid_pattern.match(p.name) and p.name not in seen:
                    seen.add(p.name)
                    candidates.append(p)
        videos = sorted(candidates, key=lambda p: p.name)

    if not videos:
        print("처리할 동영상이 없습니다.")
        sys.exit(1)

    print(f"처리할 동영상: {len(videos)}개")
    print(f"클립 저장 경로: {clip_dir}")

    total_saved = 0
    for i, video in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}]", end="")
        total_saved += process_video(video, clip_dir, cfg)

    print(f"\n===== 완료 =====")
    print(f"저장된 클립: {total_saved}개  →  {clip_dir}")

    for _ in range(3):
        winsound.Beep(1000, 200)
        winsound.Beep(800, 100)


if __name__ == "__main__":
    main()
