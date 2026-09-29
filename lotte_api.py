"""롯데시네마 조회 클라이언트.

롯데시네마 예매 페이지가 사용하는 LCWS 엔드포인트를 호출한다.
2026-09 공개 구현/HAR 검증 기준으로 로그인·쿠키·Cloudflare 우회 없이
multipart/form-data 의 paramList 하나로 조회할 수 있다.

내부 회차 dict 는 watcher 가 이미 사용하는 CGV 필드명으로 맞춰 돌려준다.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
BOOK_URL = "https://www.lottecinema.co.kr/NLCHS/Ticketing"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": "https://www.lottecinema.co.kr",
    "Referer": BOOK_URL,
    "User-Agent": UA,
}

TIMEOUT = 15
RETRIES = 3

_PAGE_CACHE = None
_PAGE_CACHE_AT = 0.0
PAGE_CACHE_SECONDS = 300


class LotteError(RuntimeError):
    """롯데시네마 조회 실패."""


class Blocked(LotteError):
    """403/429 등 요청 차단."""


def _jitter():
    time.sleep(random.uniform(0.3, 0.8))


def _multipart(param_list):
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = (
        "--{b}\r\n"
        'Content-Disposition: form-data; name="paramList"\r\n\r\n'
        "{p}\r\n"
        "--{b}--\r\n"
    ).format(b=boundary, p=param_list).encode("utf-8")
    return body, "multipart/form-data; boundary=" + boundary


def _post(method_name, **fields):
    payload = {
        "MethodName": method_name,
        "channelType": "HO",
        "osType": "W",
        "osVersion": UA,
        "memberOnNo": "0",
    }
    payload.update(fields)
    param_list = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body, content_type = _multipart(param_list)

    headers = dict(HEADERS)
    headers["Content-Type"] = content_type
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")

    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise Blocked(
                    "{}: 롯데시네마 요청이 차단됐습니다.".format(exc.code)
                ) from exc
            last = exc
        except Exception as exc:
            last = exc
        else:
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except ValueError as exc:
                raise LotteError(
                    "롯데시네마 JSON 아닌 응답: " + repr(raw[:200])
                ) from exc

            ok = data.get("IsOK")
            if ok not in (True, "true", "True", 1, "1", None):
                raise LotteError(
                    "롯데시네마 API 오류: {}".format(data.get("ResultMessage"))
                )
            return data

        if attempt < RETRIES - 1:
            time.sleep(1.5 ** attempt)

    raise LotteError("롯데시네마 요청 실패: " + repr(last))


def _ticketing_page():
    global _PAGE_CACHE, _PAGE_CACHE_AT

    now = time.time()
    if _PAGE_CACHE is not None and now - _PAGE_CACHE_AT < PAGE_CACHE_SECONDS:
        return _PAGE_CACHE

    data = _post("GetTicketingPageTOBE")
    _PAGE_CACHE = data
    _PAGE_CACHE_AT = now
    return data


def _encode_site(division, detail, cinema_id):
    return "{}_{}_{}".format(division, detail, cinema_id)


def _composite(site_no):
    parts = str(site_no).split("_", 2)
    if len(parts) != 3:
        raise LotteError(
            "롯데시네마 지점코드 형식 오류: {} (예: 1_0001_1016)".format(site_no)
        )
    return "|".join(parts)


def get_branches():
    """전국 지점 목록.

    [{'area':'서울','name':'월드타워','no':'1_0001_1016'}, ...]
    """
    data = _ticketing_page()

    area_items = (
        data.get("CinemaDivison", {})
        .get("AreaDivisions", {})
        .get("Items", [])
        or []
    )
    area_by_detail = {}
    for a in area_items:
        detail = str(a.get("DetailDivisionCode") or "")
        name = a.get("GroupNameKR") or a.get("DivisionNameKR") or ""
        if detail and name:
            area_by_detail[detail] = name

    cinemas = (
        data.get("Cinemas", {})
        .get("Cinemas", {})
        .get("Items", [])
        or []
    )

    out = []
    seen = set()
    for c in cinemas:
        cinema_id = str(c.get("CinemaID") or "")
        name = c.get("CinemaNameKR") or ""
        division = str(c.get("DivisionCode") or "")
        detail = str(c.get("DetailDivisionCode") or "")
        if not (cinema_id and name and division and detail):
            continue

        no = _encode_site(division, detail, cinema_id)
        if no in seen:
            continue
        seen.add(no)

        area = (
            area_by_detail.get(detail)
            or c.get("GroupNameKR")
            or c.get("CinemaAddrSummary")
            or "기타"
        )
        out.append({"area": str(area), "name": str(name), "no": no})

    return out


def get_movies():
    data = _ticketing_page()
    items = (
        data.get("Movies", {})
        .get("Movies", {})
        .get("Items", [])
        or []
    )
    return [
        {
            "movNo": str(m.get("RepresentationMovieCode") or ""),
            "movNm": str(m.get("MovieNameKR") or ""),
        }
        for m in items
        if m.get("RepresentationMovieCode") and m.get("MovieNameKR")
    ]


def get_open_dates():
    data = _ticketing_page()
    items = (
        data.get("MoviePlayDates", {})
        .get("Items", {})
        .get("Items", [])
        or []
    )
    out = []
    for d in items:
        ymd = str(d.get("PlayDate") or "").replace("-", "")
        if not ymd:
            continue
        is_play = d.get("IsPlayDate")
        if is_play in (False, "false", "False", "N", "0", 0):
            continue
        out.append(ymd)
    return out


def _fmt_time(value):
    return str(value or "").replace(":", "").replace(".", "")[:4]


def _fmt_date(value):
    return str(value or "").replace("-", "")[:8]


def to_row(item):
    division_name = (
        item.get("ScreenDivisionNameKR")
        or item.get("BrandNm_KR")
        or "일반"
    )
    return {
        "movNo": str(item.get("RepresentationMovieCode") or ""),
        "movNm": str(item.get("MovieNameKR") or ""),
        "scnYmd": _fmt_date(item.get("PlayDt")),
        "scnsrtTm": _fmt_time(item.get("StartTime")),
        "scnsNo": str(item.get("ScreenID") or ""),
        "tcscnsGradNm": str(division_name),
        "movkndDsplNm": str(item.get("ScreenNameKR") or ""),
        "frSeatCnt": item.get("BookingSeatCount"),
        "stcnt": item.get("TotalSeatCount"),
    }


def get_schedules(site_no, scn_ymd):
    """지점 + 날짜의 모든 영화/모든 상영관 회차를 한 번에 조회."""
    ymd = str(scn_ymd)
    play_date = "{}-{}-{}".format(ymd[:4], ymd[4:6], ymd[6:8])

    data = _post(
        "GetPlaySequence",
        playDate=play_date,
        cinemaID=_composite(site_no),
        representationMovieCode="",
    )
    items = data.get("PlaySeqs", {}).get("Items", []) or []
    return [to_row(r) for r in items]


def get_gate(site_no, target_date=None):
    """변화 감지용 게이트.

    target_date가 있으면 그 날짜 전체 회차 수를 직접 본다. 따라서 이미 열린
    날짜에 새 영화/회차가 추가되어도 다음 점검에서 바로 변화를 잡는다.
    """
    dates = get_open_dates()

    if target_date:
        rows = get_schedules(site_no, target_date)
    else:
        tomorrow = (
            datetime.now(KST) + timedelta(days=1)
        ).strftime("%Y%m%d")
        rows = get_schedules(site_no, tomorrow)

    return {
        "dates": dates,
        "shows": str(len(rows)),
    }


def booking_url(site_no):
    return BOOK_URL
