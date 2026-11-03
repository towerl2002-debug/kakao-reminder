"""Notion '통합 일정'을 읽어 카카오톡 '나에게 보내기'로 알림을 보낸다.

사용법:
    python reminder.py brief    # 오늘 일정 아침 브리핑
    python reminder.py remind   # 알림 시점이 된 일정 리마인드 (10분마다 실행)

필요한 환경 변수:
    NOTION_TOKEN, NOTION_DATABASE_ID,
    KAKAO_REST_API_KEY, KAKAO_REFRESH_TOKEN, (선택) KAKAO_CLIENT_SECRET
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
NOTION_VERSION = "2022-06-28"

# 알림 시점 → 일정 시작 몇 분 전에 보낼지
OFFSETS = {
    "정시": 0,
    "10분 전": 10,
    "30분 전": 30,
    "1시간 전": 60,
    "하루 전": 24 * 60,
}
DEFAULT_OFFSET = "30분 전"
# 크론이 늦게 돌아도 일정 시작 후 이 시간까지는 보낸다
LATE_GRACE = timedelta(minutes=30)
# 시간 없는(날짜만 있는) 일정에 '하루 전'을 고르면 전날 이 시각에 보낸다
DATE_ONLY_EVE = time(20, 0)
# 카카오 텍스트 메시지 최대 길이
KAKAO_TEXT_LIMIT = 200


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        sys.exit(f"환경 변수 {name} 이(가) 비어 있습니다.")
    return value


def http(method, url, headers=None, data=None, form=None):
    headers = dict(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"{method} {url} 실패 ({e.code}): {detail}")


# ---------- Kakao ----------

def kakao_access_token():
    form = {
        "grant_type": "refresh_token",
        "client_id": env("KAKAO_REST_API_KEY"),
        "refresh_token": env("KAKAO_REFRESH_TOKEN"),
    }
    secret = env("KAKAO_CLIENT_SECRET", required=False)
    if secret:
        form["client_secret"] = secret
    res = http("POST", "https://kauth.kakao.com/oauth/token", form=form)

    # 만료 한 달 전부터 새 refresh token이 발급된다. 워크플로가 GitHub Secret에 저장하도록 넘긴다.
    new_refresh = res.get("refresh_token")
    if new_refresh:
        print(f"::add-mask::{new_refresh}")
        out = os.environ.get("NEW_REFRESH_TOKEN_FILE")
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(new_refresh)
        print("카카오 refresh token이 새로 발급되었습니다.")
    return res["access_token"]


def kakao_send(access_token, text, link_url):
    chunks = split_text(text, KAKAO_TEXT_LIMIT)
    for chunk in chunks:
        template = {
            "object_type": "text",
            "text": chunk,
            "link": {"web_url": link_url, "mobile_web_url": link_url},
            "button_title": "노션에서 보기",
        }
        http(
            "POST",
            "https://kapi.kakao.com/v2/api/talk/memo/default/send",
            headers={"Authorization": f"Bearer {access_token}"},
            form={"template_object": json.dumps(template, ensure_ascii=False)},
        )


def split_text(text, limit):
    """줄 단위로 limit 이하 조각으로 나눈다."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ---------- Notion ----------

def notion_headers():
    return {
        "Authorization": f"Bearer {env('NOTION_TOKEN')}",
        "Notion-Version": NOTION_VERSION,
    }


def notion_query(start_day, end_day):
    """start_day ~ end_day 사이, 취소·완료가 아닌 일정 목록."""
    db_id = env("NOTION_DATABASE_ID")
    payload = {
        "filter": {
            "and": [
                {"property": "일시", "date": {"on_or_after": start_day.isoformat()}},
                {"property": "일시", "date": {"on_or_before": end_day.isoformat()}},
                {"property": "상태", "status": {"does_not_equal": "취소"}},
                {"property": "상태", "status": {"does_not_equal": "완료"}},
                {"property": "완료", "checkbox": {"equals": False}},
            ]
        },
        "page_size": 100,
    }
    pages = []
    while True:
        res = http("POST", f"https://api.notion.com/v1/databases/{db_id}/query",
                   headers=notion_headers(), data=payload)
        pages.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        payload["start_cursor"] = res["next_cursor"]
    return [e for e in (parse_event(p) for p in pages) if e]


def notion_mark_sent(page_id):
    http("PATCH", f"https://api.notion.com/v1/pages/{page_id}", headers=notion_headers(),
         data={"properties": {"알림 보냄": {"checkbox": True}}})


def plain(rich):
    return "".join(r.get("plain_text", "") for r in rich or [])


def parse_event(page):
    props = page["properties"]
    when = (props.get("일시") or {}).get("date")
    if not when or not when.get("start"):
        return None
    raw = when["start"]
    has_time = "T" in raw
    if has_time:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=KST)
        start = start.astimezone(KST)
    else:
        start = datetime.combine(date.fromisoformat(raw), time(0, 0), KST)
    remind = ((props.get("알림 시점") or {}).get("select") or {}).get("name")
    return {
        "id": page["id"],
        "title": plain(props.get("이름", {}).get("title")) or "(제목 없음)",
        "place": plain(props.get("장소", {}).get("rich_text")),
        "memo": plain(props.get("메모", {}).get("rich_text")),
        "start": start,
        "has_time": has_time,
        "remind": remind,
        "sent": (props.get("알림 보냄") or {}).get("checkbox", False),
    }


# ---------- 동작 ----------

def describe(event):
    line = event["title"]
    if event["place"]:
        line += f" @ {event['place']}"
    return line


def run_brief(now):
    today = now.date()
    events = [e for e in notion_query(today, today) if e["start"].date() == today]
    weekday = "월화수목금토일"[today.weekday()]
    lines = [f"☀️ {today.month}월 {today.day}일({weekday}) 오늘 일정"]
    if not events:
        lines.append("등록된 일정이 없어요.")
    timed = sorted((e for e in events if e["has_time"]), key=lambda e: e["start"])
    allday = [e for e in events if not e["has_time"]]
    for e in timed:
        lines.append(f"• {e['start']:%H:%M} {describe(e)}")
    for e in allday:
        lines.append(f"• (종일) {describe(e)}")
    return "\n".join(lines)


def remind_at(event):
    choice = event["remind"] or DEFAULT_OFFSET
    if choice == "알림 없음":
        return None
    if not event["has_time"]:
        # 시간 없는 일정은 아침 브리핑으로 안내되고, '하루 전'만 전날 저녁에 따로 보낸다
        if choice != "하루 전":
            return None
        return datetime.combine(event["start"].date() - timedelta(days=1), DATE_ONLY_EVE, KST)
    return event["start"] - timedelta(minutes=OFFSETS.get(choice, OFFSETS[DEFAULT_OFFSET]))


def due_reminders(now):
    events = notion_query(now.date() - timedelta(days=1), now.date() + timedelta(days=2))
    due = []
    for e in events:
        if e["sent"]:
            continue
        at = remind_at(e)
        if at is None:
            continue
        deadline = e["start"] + LATE_GRACE if e["has_time"] else e["start"]
        if at <= now <= deadline:
            due.append(e)
    return due


def reminder_text(event, now):
    if event["has_time"]:
        minutes = round((event["start"] - now).total_seconds() / 60)
        if minutes > 90:
            hours = minutes // 60
            lead = f"{hours}시간 후" if hours < 24 else "내일"
        elif minutes > 0:
            lead = f"{minutes}분 후"
        else:
            lead = "지금"
        lines = [f"⏰ {lead} 일정: {event['title']}",
                 f"🕒 {event['start']:%m/%d %H:%M}"]
    else:
        lines = [f"⏰ 내일 일정: {event['title']}",
                 f"📅 {event['start']:%m/%d} (종일)"]
    if event["place"]:
        lines.append(f"📍 {event['place']}")
    if event["memo"]:
        lines.append(f"📝 {event['memo']}")
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "remind"
    now = datetime.now(KST)
    link = "https://www.notion.so/" + env("NOTION_DATABASE_ID").replace("-", "")

    if mode == "brief":
        token = kakao_access_token()
        kakao_send(token, run_brief(now), link)
        print("아침 브리핑을 보냈습니다.")
    elif mode == "remind":
        due = due_reminders(now)
        if not due:
            print("보낼 알림이 없습니다.")
            return
        token = kakao_access_token()
        for e in due:
            kakao_send(token, reminder_text(e, now), link)
            notion_mark_sent(e["id"])
        print(f"알림 {len(due)}건을 보냈습니다.")
    elif mode == "test":
        token = kakao_access_token()
        kakao_send(token, "✅ 일정 알림 연결 테스트 메시지입니다.", link)
        print("테스트 메시지를 보냈습니다.")
    else:
        sys.exit(f"알 수 없는 모드: {mode} (brief / remind / test)")


if __name__ == "__main__":
    main()
