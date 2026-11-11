"""Notion '통합 일정'과 '할 일'을 읽어 카카오톡 '나에게 보내기'로 알림을 보낸다.

사용법:
    python reminder.py brief    # 아침 8시 브리핑: 오늘 일정과 할 일
    python reminder.py remind   # 특별 보고(언제든) + 중요 일정의 1주일 전, 3일 전 알림 (아침 8시 이후 1회)
    python reminder.py special  # 특별 보고만 보내기
    python reminder.py test     # 연결 시험

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

# 할 일 표와 일일 보고 표
TODO_DATABASE_ID = "3fe1927d8b2580c7bf8bde3c8af389c1"
REPORT_URL = "https://www.notion.so/808f8251095c453fb9c22cc8b91b1fde"
# 정보분석부 '조사 요청' 표 (특별 보고)
RESEARCH_DATABASE_ID = "ea1bbd79b6584535a1e9dee69a76eb28"
# 특별 보고를 보내는 상태
SPECIAL_STATUSES = ("완료", "진 확인 필요")
# 한 번에 보낼 특별 보고 최대 개수
SPECIAL_LIMIT = 5

# 중요 일정 알림을 보내기 시작하는 시각
IMPORTANT_FROM = time(8, 0)
# 아침 브리핑에 보여 줄 할 일 최대 개수
TODO_LIMIT = 10
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


def notion_todos(today):
    """오늘까지 마감인, 끝나지 않은 할 일 목록. 읽지 못하면 None."""
    payload = {
        "filter": {
            "and": [
                {"property": "마감일", "date": {"on_or_before": today.isoformat()}},
                {"property": "상태", "status": {"does_not_equal": "완료"}},
                {"property": "상태", "status": {"does_not_equal": "보류"}},
                {"property": "완료", "checkbox": {"equals": False}},
            ]
        },
        "sorts": [{"property": "마감일", "direction": "ascending"}],
        "page_size": TODO_LIMIT + 1,
    }
    try:
        res = http("POST", f"https://api.notion.com/v1/databases/{TODO_DATABASE_ID}/query",
                   headers=notion_headers(), data=payload)
    except SystemExit as e:
        print(f"할 일을 읽지 못했습니다: {e}")
        return None
    todos = []
    for page in res.get("results", []):
        props = page["properties"]
        due = ((props.get("마감일") or {}).get("date") or {}).get("start")
        todos.append({
            "title": plain(props.get("할 일", {}).get("title")) or "(제목 없음)",
            "due": date.fromisoformat(due[:10]) if due else None,
        })
    return todos


def notion_special_reports():
    """조사를 마쳤는데 아직 카톡을 보내지 않은 요청. 읽지 못하면 빈 목록."""
    payload = {
        "filter": {
            "and": [
                {"or": [{"property": "상태", "select": {"equals": s}} for s in SPECIAL_STATUSES]},
                {"property": "카톡 보냄", "checkbox": {"equals": False}},
            ]
        },
        "page_size": SPECIAL_LIMIT,
    }
    try:
        res = http("POST", f"https://api.notion.com/v1/databases/{RESEARCH_DATABASE_ID}/query",
                   headers=notion_headers(), data=payload)
    except SystemExit as e:
        print(f"조사 요청 표를 읽지 못했습니다(노션 연결에 표가 추가됐는지 확인): {e}")
        return []
    reports = []
    for page in res.get("results", []):
        props = page["properties"]
        due = ((props.get("기한") or {}).get("date") or {}).get("start")
        reports.append({
            "id": page["id"],
            "url": page.get("url") or REPORT_URL,
            "title": plain(props.get("요청", {}).get("title")) or "(제목 없음)",
            "summary": plain(props.get("결과 한 줄", {}).get("rich_text")),
            "status": ((props.get("상태") or {}).get("select") or {}).get("name", ""),
            "due": date.fromisoformat(due[:10]) if due else None,
        })
    return reports


def notion_check(page_id, prop):
    http("PATCH", f"https://api.notion.com/v1/pages/{page_id}", headers=notion_headers(),
         data={"properties": {prop: {"checkbox": True}}})


def plain(rich):
    return "".join(r.get("plain_text", "") for r in rich or [])


def checked(props, name):
    return (props.get(name) or {}).get("checkbox", False)


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
    return {
        "id": page["id"],
        "title": plain(props.get("이름", {}).get("title")) or "(제목 없음)",
        "place": plain(props.get("장소", {}).get("rich_text")),
        "start": start,
        "has_time": has_time,
        "important": checked(props, "중요"),
        "sent_week": checked(props, "1주 전 알림"),
        "sent_three": checked(props, "3일 전 알림"),
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

    lines.append("")
    lines.append("✅ 오늘 할 일")
    todos = notion_todos(today)
    if todos is None:
        lines.append("할 일을 읽지 못했어요. 노션 연결을 확인해 주세요.")
    elif not todos:
        lines.append("오늘까지 마감인 할 일이 없어요.")
    else:
        for t in todos[:TODO_LIMIT]:
            late = t["due"] is not None and t["due"] < today
            mark = f" (밀림 {t['due'].month}/{t['due'].day})" if late else ""
            lines.append(f"• {t['title']}{mark}")
        if len(todos) > TODO_LIMIT:
            lines.append("• 더 있어요. 노션에서 확인해 주세요.")
    return "\n".join(lines)


def due_important(now):
    """지금 보내야 할 (일정, 체크할 칸) 목록."""
    today = now.date()
    result = []
    for e in notion_query(today, today + timedelta(days=7)):
        if not e["important"]:
            continue
        days = (e["start"].date() - today).days
        if 4 <= days <= 7 and not e["sent_week"]:
            result.append((e, days, "1주 전 알림"))
        elif 1 <= days <= 3 and not e["sent_three"]:
            result.append((e, days, "3일 전 알림"))
    return result


def important_text(event, days):
    if event["has_time"]:
        when = f"{event['start']:%m/%d %H:%M}"
    else:
        when = f"{event['start']:%m/%d} (종일)"
    lines = [f"🔔 중요 일정 {days}일 전: {event['title']}", f"📅 {when}"]
    if event["place"]:
        lines.append(f"📍 {event['place']}")
    return "\n".join(lines)


def special_text(report):
    lines = [f"[특별 보고/정보분석부] {report['title']}"]
    if report["summary"]:
        lines.append(report["summary"])
    if report["status"] == "진 확인 필요":
        if report["due"]:
            lines.append(f"⏰ {report['due'].month}/{report['due'].day}까지 확인이 필요해요.")
        else:
            lines.append("진 님의 확인이 필요해요.")
    lines.append("노션에서 결과를 확인해 주세요.")
    return "\n".join(lines)


def send_special(token=None):
    """특별 보고를 보내고 '카톡 보냄'에 체크한다. 쓴 토큰을 돌려준다."""
    reports = notion_special_reports()
    if not reports:
        print("보낼 특별 보고가 없습니다.")
        return token
    token = token or kakao_access_token()
    for r in reports:
        kakao_send(token, special_text(r), r["url"])
        notion_check(r["id"], "카톡 보냄")
    print(f"특별 보고 {len(reports)}건을 보냈습니다.")
    return token


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "remind"
    now = datetime.now(KST)
    link = "https://www.notion.so/" + env("NOTION_DATABASE_ID").replace("-", "")

    if mode == "special":
        send_special()
    elif mode == "brief":
        token = kakao_access_token()
        kakao_send(token, run_brief(now), REPORT_URL)
        print("아침 브리핑을 보냈습니다.")
    elif mode == "remind":
        token = send_special()
        if now.time() < IMPORTANT_FROM:
            print("아직 알림 시간이 아닙니다.")
            return
        due = due_important(now)
        if not due:
            print("보낼 알림이 없습니다.")
            return
        token = token or kakao_access_token()
        for event, days, prop in due:
            kakao_send(token, important_text(event, days), link)
            notion_check(event["id"], prop)
        print(f"알림 {len(due)}건을 보냈습니다.")
    elif mode == "test":
        token = kakao_access_token()
        kakao_send(token, "✅ 일정 알림 연결 테스트 메시지입니다.", link)
        print("테스트 메시지를 보냈습니다.")
    else:
        sys.exit(f"알 수 없는 모드: {mode} (brief / remind / special / test)")


if __name__ == "__main__":
    main()
