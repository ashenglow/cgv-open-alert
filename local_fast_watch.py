#!/usr/bin/env python3
"""집 맥북용 빠른 감시.

- config.json의 CGV/메가박스 대상만 사용
- target_date 하루만 직접 조회
- 메가박스: 지점별 약 12초 간격
- CGV: 지점별 약 60초 간격
- CGV는 '모닝'(salsTznCd=01) 회차만 추적
- 같은 지점/같은 영화에서 새 모닝 회차가 추가되면 매번 다시 알림
- 메가박스는 기존처럼 영화가 처음 보일 때 1회 알림
- local_state.json에 기록해서 재시작해도 같은 회차 중복 알림 방지

실행:
    source .venv/bin/activate
    python local_fast_watch.py
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cgv_api
import chains
import megabox_api
import notifier

KST = timezone(timedelta(hours=9))
CONFIG_FILE = Path(__file__).with_name("config.json")
STATE_FILE = Path(__file__).with_name("local_state.json")

MEGABOX_INTERVAL = 12
CGV_INTERVAL = 60
PRIORITY_CGV_INTERVAL = 15
ERROR_BACKOFF = 20


def log(msg):
    print("[{}] {}".format(datetime.now(KST).strftime("%H:%M:%S"), msg), flush=True)


def load_config():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {"alerted": [], "cgv_morning_seen": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("alerted", [])
        state.setdefault("cgv_morning_seen", {})
        return state
    except Exception:
        return {"alerted": [], "cgv_morning_seen": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def target_key(t):
    return "{}|{}|{}".format(
        t["site_no"],
        t.get("movie_keyword", ""),
        t.get("notify", "movie"),
    )


def session_key(row):
    """같은 모닝 회차를 재조회해도 동일하게 잡히는 키."""
    return "|".join(str(row.get(k) or "") for k in (
        "scnYmd", "scnsNo", "scnSseq", "scnsrtTm", "prodNo"
    ))


def fmt_time(v):
    s = str(v or "")
    if len(s) >= 4:
        return s[:2] + ":" + s[2:4]
    return s


def booking_url(site_no, site_nm):
    return chains.booking_url(site_no, site_nm)


def cgv_session_url(site_no, site_nm, row):
    """CGV 웹 예매를 해당 날짜/지점/상영관/회차로 최대한 직접 연다."""
    params = {
        "movNo": row.get("movNo", ""),
        "scnSseq": row.get("scnSseq", ""),
        "scnYmd": row.get("scnYmd", ""),
        "scnsNo": row.get("scnsNo", ""),
        "siteNm": "CGV " + site_nm,
        "siteNo": site_no,
    }
    return "https://cgv.co.kr/cnm/movieBook/movie?" + urllib.parse.urlencode(params)


def matches_movie(row, keyword):
    return keyword.replace(" ", "").lower() in str(row.get("movNm") or "").replace(" ", "").lower()


def is_cgv_morning(row):
    # 실제 CGV 응답에서 모닝 = salsTznCd "01", salsTznNm "모닝"
    return str(row.get("salsTznCd") or "") == "01" or row.get("salsTznNm") == "모닝"


def build_megabox_message(t, rows):
    site_no = t["site_no"]
    site_nm = t.get("site_nm", site_no)
    first = min(rows, key=lambda r: r.get("scnsrtTm") or "9999")
    times = ", ".join(fmt_time(r.get("scnsrtTm")) for r in sorted(
        rows, key=lambda r: r.get("scnsrtTm") or "9999"
    )[:10])
    movie = first.get("movNm") or t.get("movie_keyword") or "영화"
    return (
        "🚨 <b>메가박스 예매 오픈 감지</b>\n\n"
        "<b>{}</b>\n"
        "{}\n"
        "첫 회차  <b>{}</b>\n"
        "현재 {}회  {}\n\n"
        "🔗 {}"
    ).format(
        site_nm, movie, fmt_time(first.get("scnsrtTm")),
        len(rows), times, booking_url(site_no, site_nm)
    )


def build_cgv_morning_message(t, new_rows, all_morning_rows):
    site_no = t["site_no"]
    site_nm = t.get("site_nm", site_no)
    new_rows = sorted(new_rows, key=lambda r: r.get("scnsrtTm") or "9999")
    all_morning_rows = sorted(all_morning_rows, key=lambda r: r.get("scnsrtTm") or "9999")
    movie = new_rows[0].get("movNm") or t.get("movie_keyword") or "영화"
    new_times = ", ".join(fmt_time(r.get("scnsrtTm")) for r in new_rows)
    all_times = ", ".join(fmt_time(r.get("scnsrtTm")) for r in all_morning_rows)

    is_added = len(all_morning_rows) > len(new_rows)
    if is_added:
        title = "☀️☀️🚨 <b>CGV 조조 추가 오픈</b>"
        detail = "새로 추가된 조조  <b>{}</b>".format(new_times)
    else:
        title = "☀️🚨 <b>CGV 첫 조조 오픈</b>"
        detail = "조조  <b>{}</b>".format(new_times)

    text = (
        "{}\n\n"
        "<b>{}</b>\n"
        "{}\n"
        "{}\n"
        "현재 조조 전체  {}\n\n"
        "👇 <b>아래 버튼을 누르면 해당 10/3 회차 웹 예매로 이동</b>"
    ).format(
        title, site_nm, movie, detail, all_times
    )

    keyboard = [[{
        "text": "🎟 {} {} 바로 예매".format(site_nm, fmt_time(r.get("scnsrtTm"))),
        "url": cgv_session_url(site_no, site_nm, r),
    }] for r in new_rows[:4]]

    return text, keyboard

def fetch_rows(t, target_date):
    site_no = t["site_no"]
    if chains.is_megabox(site_no):
        return megabox_api.get_schedules(chains.code(site_no), target_date)
    return cgv_api.get_schedules(site_no, target_date)


def main():
    cfg = load_config()
    target_date = cfg.get("target_date")
    if not target_date:
        raise RuntimeError("config.json에 target_date가 없습니다.")

    targets = [
        t for t in cfg.get("targets", [])
        if not chains.is_lotte(t["site_no"])
    ]
    priority_cgv_sites = set(str(x) for x in cfg.get("priority_cgv_sites", []))
    if not targets:
        raise RuntimeError("로컬 감시 대상이 없습니다.")

    state = load_state()
    alerted = set(state.get("alerted", []))
    morning_seen = state.setdefault("cgv_morning_seen", {})

    now = time.monotonic()
    due = {}
    for i, t in enumerate(targets):
        due[target_key(t)] = now + i * 0.25

    try:
        notifier.send(
            "🏠 <b>로컬 빠른 감시 시작</b>\n\n"
            "CGV {}곳 · 메가박스 {}곳\n"
            "CGV 일반 약 {}초 / 집중감시 약 {}초 / 메가박스 약 {}초\n"
            "CGV는 새 모닝 회차가 추가될 때마다 재알림\n"
            "집중감시 CGV: {}\n"
            "대상일 {}".format(
                sum(not chains.is_megabox(t["site_no"]) for t in targets),
                sum(chains.is_megabox(t["site_no"]) for t in targets),
                CGV_INTERVAL, PRIORITY_CGV_INTERVAL, MEGABOX_INTERVAL,
                ", ".join(
                    t.get("site_nm", t["site_no"]) for t in targets
                    if (not chains.is_megabox(t["site_no"])
                        and str(t["site_no"]) in priority_cgv_sites)
                ) or "없음",
                target_date,
            )
        )
    except Exception as exc:
        log("시작 알림 실패: {}".format(exc))

    log("로컬 감시 시작: {}건".format(len(targets)))

    while True:
        now = time.monotonic()
        ready = [t for t in targets if due.get(target_key(t), 0) <= now]

        if not ready:
            time.sleep(0.5)
            continue

        t = min(ready, key=lambda x: due.get(target_key(x), 0))
        key = target_key(t)
        site_no = t["site_no"]
        site_nm = t.get("site_nm", site_no)
        if chains.is_megabox(site_no):
            interval = MEGABOX_INTERVAL
        elif str(site_no) in priority_cgv_sites:
            interval = PRIORITY_CGV_INTERVAL
        else:
            interval = CGV_INTERVAL

        try:
            rows = fetch_rows(t, target_date)
            hits = [r for r in rows if matches_movie(r, t.get("movie_keyword", ""))]

            if chains.is_megabox(site_no):
                log("메가박스 {}: {}회차".format(site_nm, len(hits)))
                if hits and key not in alerted:
                    notifier.send(build_megabox_message(t, hits))
                    alerted.add(key)
                    state["alerted"] = sorted(alerted)
                    save_state(state)
                    log(">>> 메가박스 알림 전송: {}".format(site_nm))
            else:
                morning = [r for r in hits if is_cgv_morning(r)]
                current = {session_key(r): r for r in morning}

                # 이전 버전에서 이미 이 지점에 한 번 알림을 보낸 적이 있다면,
                # 업그레이드 직후 현재 존재하는 조조는 기준선으로만 등록한다.
                # 이후 새 조조가 추가되면 그때 다시 알린다.
                if key not in morning_seen:
                    previous = set()
                    if key in alerted:
                        morning_seen[key] = sorted(current)
                        save_state(state)
                        new_rows = []
                    else:
                        new_rows = list(current.values())
                        morning_seen[key] = sorted(current)
                        save_state(state)
                else:
                    previous = set(morning_seen.get(key, []))
                    new_ids = set(current) - previous
                    new_rows = [current[x] for x in new_ids]
                    if new_ids:
                        morning_seen[key] = sorted(previous | set(current))
                        save_state(state)

                log(
                    "CGV {}: 치이카와 {}회 / 모닝 {}회{}".format(
                        site_nm, len(hits), len(morning),
                        " / 새 모닝 {}".format(
                            ",".join(fmt_time(r.get("scnsrtTm")) for r in sorted(
                                new_rows, key=lambda r: r.get("scnsrtTm") or "9999"
                            ))
                        ) if new_rows else ""
                    )
                )

                if new_rows:
                    message, keyboard = build_cgv_morning_message(t, new_rows, morning)
                    notifier.send(message, keyboard=keyboard)
                    alerted.add(key)
                    state["alerted"] = sorted(alerted)
                    save_state(state)
                    log(">>> CGV 새 조조 알림 전송: {}".format(site_nm))

            due[key] = time.monotonic() + interval + random.uniform(-1.0, 1.0)

        except cgv_api.CloudflareBlocked as exc:
            log("CGV {} 403: {}".format(site_nm, str(exc).splitlines()[0]))
            due[key] = time.monotonic() + max(CGV_INTERVAL, ERROR_BACKOFF)

        except Exception as exc:
            log("{} {} 조회 실패: {}".format(chains.label(site_no), site_nm, exc))
            due[key] = time.monotonic() + ERROR_BACKOFF

        time.sleep(random.uniform(0.25, 0.6))


if __name__ == "__main__":
    main()
