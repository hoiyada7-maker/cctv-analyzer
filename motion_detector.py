"""
움직임 감지 모듈
- OpenCV로 영상에서 움직임 있는 구간만 추출
- 연속된 움직임은 하나의 클립으로 그룹화
- 노이즈/조명 변화는 무시
"""

import cv2
import logging
import subprocess
from pathlib import Path
from typing import List, Tuple
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class MotionSegment:
    """움직임 구간"""
    start_sec: float
    end_sec: float
    peak_intensity: float  # 가장 큰 움직임 정도 (0~1)
    avg_intensity: float

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec


class MotionDetector:
    """
    백그라운드 서브트랙션 기반 움직임 감지.
    MOG2 알고리즘 사용 - 조명 변화에 강하고 그림자 무시 가능.
    """

    def __init__(
        self,
        min_area_ratio: float = 0.005,  # 화면의 0.5% 이상 변화해야 움직임으로 인정
        min_duration: float = 2.0,       # 2초 이상 지속된 움직임만
        merge_gap: float = 10.0,         # 10초 이내의 움직임은 묶음
        sample_fps: float = 2.0,         # 감지 속도 (초당 2프레임 분석)
        warmup_frames: int = 30,         # 초기 학습 프레임
    ):
        self.min_area_ratio = min_area_ratio
        self.min_duration = min_duration
        self.merge_gap = merge_gap
        self.sample_fps = sample_fps
        self.warmup_frames = warmup_frames

    def detect(self, video_path: str) -> List[MotionSegment]:
        """
        영상 분석 후 움직임 구간 리스트 반환.
        리턴: [MotionSegment, ...]
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            log.error(f"영상 열기 실패: {video_path}")
            return []

        original_fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_area = width * height

        # sample_fps에 맞춰 프레임 건너뛰기
        skip = max(1, int(original_fps / self.sample_fps))

        log.info(
            f"  감지 시작: {Path(video_path).name} "
            f"({width}x{height}, {original_fps:.1f}fps, "
            f"{total_frames/original_fps:.0f}초)"
        )

        # MOG2: 그림자 감지, 가우시안 5개, 임계값 25
        bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=25, detectShadows=True
        )

        motion_records = []  # [(time_sec, intensity), ...]
        frame_idx = 0
        analyzed = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # 프레임 스킵
            if frame_idx % skip != 0:
                frame_idx += 1
                continue

            time_sec = frame_idx / original_fps

            # 다운스케일로 연산량 절감
            small = cv2.resize(frame, (480, 270))

            # 그레이스케일 + 가우시안 블러로 노이즈 제거
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)

            # 백그라운드 서브트랙션
            fg_mask = bg_sub.apply(blurred)

            # 그림자(127)는 제외, 진짜 움직임(255)만
            fg_mask[fg_mask == 127] = 0

            # 모폴로지 연산으로 작은 노이즈 제거
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)

            # 움직임 영역 비율
            motion_pixels = cv2.countNonZero(fg_mask)
            motion_ratio = motion_pixels / (480 * 270)

            # 워밍업 이후부터 기록
            if analyzed >= self.warmup_frames and motion_ratio >= self.min_area_ratio:
                motion_records.append((time_sec, motion_ratio))

            analyzed += 1
            frame_idx += 1

        cap.release()

        # 움직임 기록을 세그먼트로 그룹화
        segments = self._group_into_segments(motion_records)

        # 최소 지속시간 필터
        segments = [s for s in segments if s.duration >= self.min_duration]

        total_motion = sum(s.duration for s in segments)
        total_video = total_frames / original_fps
        ratio = (total_motion / total_video * 100) if total_video > 0 else 0

        log.info(
            f"  감지 완료: {len(segments)}개 구간, "
            f"총 {total_motion:.0f}초 / {total_video:.0f}초 ({ratio:.1f}%)"
        )

        return segments

    def _group_into_segments(
        self, records: List[Tuple[float, float]]
    ) -> List[MotionSegment]:
        """움직임 기록을 시간 근접도로 그룹화"""
        if not records:
            return []

        segments = []
        current_start = records[0][0]
        current_end = records[0][0]
        current_intensities = [records[0][1]]

        for time_sec, intensity in records[1:]:
            if time_sec - current_end <= self.merge_gap:
                # 이전 움직임에 이어붙임
                current_end = time_sec
                current_intensities.append(intensity)
            else:
                # 새 세그먼트 시작
                segments.append(MotionSegment(
                    start_sec=current_start,
                    end_sec=current_end + 1.0,  # 끝에 1초 여유
                    peak_intensity=max(current_intensities),
                    avg_intensity=sum(current_intensities) / len(current_intensities),
                ))
                current_start = time_sec
                current_end = time_sec
                current_intensities = [intensity]

        # 마지막 세그먼트
        segments.append(MotionSegment(
            start_sec=current_start,
            end_sec=current_end + 1.0,
            peak_intensity=max(current_intensities),
            avg_intensity=sum(current_intensities) / len(current_intensities),
        ))

        return segments


def extract_clip(
    input_path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
    scale: str = "640:360",
    fps: int = 2,
    pre_buffer: float = 2.0,   # 시작 전 2초 여유
    post_buffer: float = 2.0,  # 끝 후 2초 여유
) -> bool:
    """
    ffmpeg로 특정 구간만 다운샘플해서 추출.
    - 해상도 640x360 (CCTV 분석에 충분)
    - 2fps (Gemini가 어차피 1fps 샘플링)
    - 오디오 제거
    - 결과적으로 원본 대비 1/20 ~ 1/50 용량
    """
    start = max(0, start_sec - pre_buffer)
    duration = (end_sec - start_sec) + pre_buffer + post_buffer

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),               # 시작점 (입력 전에 두면 빠른 seek)
        "-i", input_path,
        "-t", str(duration),
        "-vf", f"scale={scale},fps={fps}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "30",                    # 분석용이라 화질 낮춰도 OK
        "-an",                           # 오디오 제거
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            log.error(f"  ffmpeg 실패: {result.stderr[-300:]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error(f"  ffmpeg 타임아웃")
        return False


if __name__ == "__main__":
    # 단독 실행 테스트
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if len(sys.argv) < 2:
        print("사용법: python motion_detector.py <영상파일>")
        sys.exit(1)

    detector = MotionDetector()
    segments = detector.detect(sys.argv[1])

    print(f"\n총 {len(segments)}개 움직임 구간 발견:\n")
    for i, s in enumerate(segments, 1):
        start_m, start_s = divmod(int(s.start_sec), 60)
        end_m, end_s = divmod(int(s.end_sec), 60)
        print(
            f"  {i}. {start_m:02d}:{start_s:02d} ~ {end_m:02d}:{end_s:02d} "
            f"({s.duration:.0f}초, 강도 {s.peak_intensity*100:.1f}%)"
        )
