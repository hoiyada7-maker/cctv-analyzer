"""스케줄러 진입점: RTSP 녹화 → 기존 분석 파이프라인(_run_single.py) 실행 → 리포트 저장.

Windows 작업 스케줄러가 이 파일을 정해진 시각에 실행한다.
  1) config.yaml 의 rtsp.record_seconds 만큼 녹화
  2) 녹화 파일을 _run_single.py 로 분석 (움직임 감지 + 클립 추출 + AI 분석)
  3) 분석 결과를 reports/ 에 텍스트로 저장
"""
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

import yaml

from record_rtsp import record
import telegram_notifier

BASE = Path(__file__).parent
os.chdir(BASE)  # 상대경로(work/, recordings/ 등)를 프로젝트 기준으로 고정


def main() -> int:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))

    # ① 녹화
    video_path = record(cfg["rtsp"])
    if not video_path:
        print("녹화 단계 실패 → 종료")
        return 1

    # ② 기존 분석 파이프라인 실행 (한글 출력 위해 PYTHONUTF8=1)
    print(f"\n🔍 분석 시작: {video_path.name}")
    env = dict(os.environ, PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, str(BASE / "_run_single.py"), str(video_path)],
        cwd=str(BASE), env=env,
        capture_output=True, text=True, encoding="utf-8",
    )

    report = proc.stdout or ""
    print(report)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    # ③ 리포트 저장
    reports_dir = BASE / "reports"
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"report_{ts}.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n📄 리포트 저장: {report_path}")

    # ④ 텔레그램 전송
    header = f"📹 CCTV 분석 결과 ({datetime.now():%Y-%m-%d %H:%M})\n\n"
    if telegram_notifier.send(cfg["telegram"], header + report):
        print("📱 텔레그램 전송 완료")

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
