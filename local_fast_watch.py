#!/usr/bin/env python3
"""집 맥북용 빠른 감시.

- config.json의 CGV/메가박스 대상만 사용
- target_date 하루만 직접 조회
- 메가박스: 지점별 약 12초 간격
- CGV: 지점별 약 60초 간격
- 한 지점씩 순환해서 CGV 요청이 한꺼번에 몰리지 않게 함
- 치이카와가 처음 보이는 즉시 텔레그램 알림
- local_state.json에 알림 완료 대상을 저장해서 재시작해도 중복 알림 방지

실행:
    source .venv/bin/activate
    python local_fast_watch.py
"""

from __future__ import annotations

import json
import random
import time
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
ERROR_BACKOFF = 20


def log(msg):
    print("[{}] {}".format(datetime.now(KST).strftime("%H:%M:%S"), msg), flush=True)


def load_config():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {"alerted": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"alerted": []}


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


def fmt_time(v):
    s = str(v or "")
    if len(s) >= 4:
        return s[:2] + ":" + s[2:4]
    return s


def booking_url(site_no, site_nm):
    return chains.booking_url(site_no, site_nm)


def matches_movie(row, keyword):
    return keyword.replace(" ", "").lower() in str(row.get("movNm") or "").replace(" ", "").lower()


def build_message(t, rows):
    site_no = t["site_no"]
    site_nm = t.get("site_nm", site_no)
    chain = chains.label(site_no)
    first = min(rows, key=lambda r: r.get("scnsrtTm") or "9999")
    times = ", ".join(fmt_time(r.get("scnsrtTm")) for r in sorted(
        rows, key=lambda r: r.get("scnsrtTm") or "9999"
    )[:10])
    movie = first.get("movNm") or t.get("movie_keyword") or "영화"
    return (
        "🚨 <b>{} 예매 오픈 감지</b>\n\n"
        "<b>{}</b>\n"
        "{}\n"
        "첫 회차  <b>{}</b>\n"
        "현재 {}회  {}\n\n"
        "🔗 {}"
    ).format(
        chain, site_nm, movie, fmt_time(first.get("scnsrtTm")),
        len(rows), times, booking_url(site_no, site_nm)
    )


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
    if not targets:
        raise RuntimeError("로컬 감시 대상이 없습니다.")

    state = load_state()
    alerted = set(state.get("alerted", []))

    now = time.monotonic()
    due = {}
    # 시작할 때 모두 즉시 확인하되, CGV끼리는 아래 루프에서 한 번에 하나씩만 처리된다.
    for i, t in enumerate(targets):
        due[target_key(t)] = now + i * 0.25

    try:
        notifier.send(
            "🏠 <b>로컬 빠른 감시 시작</b>\n\n"
            "CGV {}곳 · 메가박스 {}곳\n"
            "CGV 지점별 약 {}초 / 메가박스 지점별 약 {}초\n"
            "대상일 {}".format(
                sum(not chains.is_megabox(t["site_no"]) for t in targets),
                sum(chains.is_megabox(t["site_no"]) for t in targets),
                CGV_INTERVAL, MEGABOX_INTERVAL, target_date,
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

        # 가장 오래 기다린 한 지점만 처리해서 요청이 한꺼번에 몰리지 않게 한다.
        t = min(ready, key=lambda x: due.get(target_key(x), 0))
        key = target_key(t)
        site_no = t["site_no"]
        site_nm = t.get("site_nm", site_no)
        interval = MEGABOX_INTERVAL if chains.is_megabox(site_no) else CGV_INTERVAL

        try:
            rows = fetch_rows(t, target_date)
            hits = [r for r in rows if matches_movie(r, t.get("movie_keyword", ""))]
            log("{} {}: {}회차".format(chains.label(site_no), site_nm, len(hits)))

            if hits and key not in alerted:
                notifier.send(build_message(t, hits))
                alerted.add(key)
                state["alerted"] = sorted(alerted)
                save_state(state)
                log(">>> 알림 전송: {} {}".format(chains.label(site_no), site_nm))

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
