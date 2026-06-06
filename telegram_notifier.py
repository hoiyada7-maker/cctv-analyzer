"""텔레그램 전송: 분석 리포트를 봇으로 보낸다.

- 메시지 4096자 제한 → 줄 단위로 안전하게 분할 전송
- config.yaml 의 telegram.enabled 가 false면 아무것도 안 함
"""
import logging
import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 3900  # 4096 한도보다 여유


def _chunks(text: str):
    """줄 경계를 지키며 LIMIT 이하 조각으로 나눔."""
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > LIMIT:
            if buf:
                yield buf
            buf = line
        else:
            buf += line
    if buf:
        yield buf


def send(tg: dict, text: str) -> bool:
    """tg = config['telegram']. chat_id 는 단일 값 또는 리스트 모두 허용. 성공 시 True."""
    if not tg.get("enabled"):
        return False
    if not text.strip():
        text = "(내용 없음)"

    chat_ids = tg["chat_id"]
    if not isinstance(chat_ids, (list, tuple)):
        chat_ids = [chat_ids]

    url = API.format(token=tg["bot_token"])
    ok = True
    for chat_id in chat_ids:
        for part in _chunks(text):
            try:
                r = requests.post(url, data={
                    "chat_id": chat_id,
                    "text": part,
                    "disable_web_page_preview": True,
                }, timeout=20)
                if not r.ok:
                    log.error(f"텔레그램 전송 실패 {r.status_code} (chat {chat_id}): {r.text[:200]}")
                    ok = False
            except Exception as e:
                log.error(f"텔레그램 전송 예외 (chat {chat_id}): {e}")
                ok = False
    return ok


if __name__ == "__main__":
    import sys, yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path(__file__).with_name("config.yaml").read_text(encoding="utf-8"))
    msg = sys.argv[1] if len(sys.argv) > 1 else "✅ CCTV 분석봇 테스트 메시지입니다."
    print("전송 성공" if send(cfg["telegram"], msg) else "전송 실패")
