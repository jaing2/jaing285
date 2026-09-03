"""
CENTURION Macro — 코스피 수급 대시보드 v2
=========================================
캔들 차트 + 투자자 세부 분해 + 기타법인 추적 + 규칙 스코어 백테스트

데이터: KRX Data Marketplace (pykrx) — data.krx.co.kr 계정 로그인 필요
LLM:   Groq (선택)
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import pandas as pd
import streamlit as st

KST = timezone(timedelta(hours=9))
KOSPI_INDEX_TICKER = "1001"

# KRX 투자자 분류
INVESTORS = ["개인", "외국인", "기관", "기타법인"]
INSTITUTIONS = ["금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금"]
FOREIGNERS = ["외국인", "기타외국인"]
OHLC = ["시가", "고가", "저가", "종가"]

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}

# 한국 시장 관례: 상승/매수 = 적색, 하락/매도 = 청색
C_UP = "#E5484D"
C_DOWN = "#3E7BFA"
C_ACCENT = "#F5A524"
C_MUTED = "#8A93A5"
C_TEXT = "#E6EDF3"
SERIES_COLORS = ["#30A46C", "#E5484D", "#3E7BFA", "#F5A524"]  # 개인/외국인/기관/기타법인
FLOW_COLORS = {"개인": "#30A46C", "외국인": "#E5484D", "기관": "#3E7BFA", "기타법인": "#F5A524"}
INST_COLORS = ["#E5484D", "#F5A524", "#30A46C", "#3E7BFA", "#8E4EC6", "#0BA5B7", "#F76808"]

PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
]

st.set_page_config(
    page_title="CENTURION Macro — 코스피 수급",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard-dynamic-subset.css');
html, body, [class*="css"] { font-family: 'Pretendard', -apple-system, BlinkMacSystemFont,
  'Segoe UI', 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
.cm-head { display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; }
.cm-head h1 { font-size: 1.65rem; font-weight: 700; letter-spacing: -.02em; margin: 0; }
.cm-head .cm-asof { color: #8A93A5; font-size: .85rem; font-variant-numeric: tabular-nums; }
.cm-sub { color: #8A93A5; font-size: .9rem; margin: .35rem 0 1.4rem 0; }
.cm-kpi { border: 1px solid rgba(255,255,255,.08); border-left: 3px solid var(--tone, #8A93A5);
  border-radius: 10px; padding: .85rem 1rem .9rem 1rem; background: rgba(255,255,255,.025); height: 100%; }
.cm-kpi .cm-label { font-size: .78rem; color: #8A93A5; margin-bottom: .3rem; }
.cm-kpi .cm-value { font-size: 1.5rem; font-weight: 700; line-height: 1.15;
  font-variant-numeric: tabular-nums; letter-spacing: -.02em; color: var(--tone, inherit); }
.cm-kpi .cm-note { font-size: .76rem; color: #6E7787; margin-top: .3rem; font-variant-numeric: tabular-nums; }
.cm-score { border: 1px solid rgba(255,255,255,.09); border-radius: 12px; padding: 1.1rem 1.3rem;
  background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.015)); }
.cm-score .cm-band { font-size: .8rem; color: #8A93A5; }
.cm-score .cm-big { font-size: 2.6rem; font-weight: 800; line-height: 1;
  font-variant-numeric: tabular-nums; letter-spacing: -.03em; }
.cm-score .cm-verdict { font-size: 1.05rem; font-weight: 600; margin-top: .35rem; }
.cm-bar { height: 6px; border-radius: 3px; background: rgba(255,255,255,.08); margin-top: .8rem; overflow: hidden; }
.cm-bar > div { height: 100%; border-radius: 3px; }
.cm-trig { display: flex; gap: .6rem; align-items: flex-start; padding: .42rem 0;
  border-bottom: 1px dashed rgba(255,255,255,.07); }
.cm-trig:last-child { border-bottom: none; }
.cm-trig .cm-mark { width: 1.2rem; flex: none; }
.cm-trig .cm-text { flex: 1; font-size: .88rem; }
.cm-trig .cm-w { color: #6E7787; font-size: .8rem; font-variant-numeric: tabular-nums; }
.cm-trig .cm-detail { color: #8A93A5; font-size: .78rem; font-variant-numeric: tabular-nums; }
.cm-disc { color: #6E7787; font-size: .78rem; line-height: 1.6; }
</style>
""",
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# 실행 로그
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [centurion] %(levelname)s %(message)s", stream=sys.stderr)
_logger = logging.getLogger("centurion")
_LOG: list[dict] = []
_T0 = time.perf_counter()
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def now_kst() -> datetime:
    return datetime.now(KST)


def log_reset() -> None:
    global _T0
    _LOG.clear()
    _T0 = time.perf_counter()


def log(stage: str, message: str, level: str = "INFO", **fields) -> None:
    entry = {
        "시각": now_kst().strftime("%H:%M:%S"),
        "경과ms": int((time.perf_counter() - _T0) * 1000),
        "레벨": level,
        "단계": stage,
        "내용": message,
        "상세": " ".join(f"{k}={v}" for k, v in fields.items()) if fields else "",
    }
    _LOG.append(entry)
    _logger.log(LEVELS.get(level, 20), "[%s] %s %s", stage, message, entry["상세"])


def log_exc(stage: str, exc: BaseException, message: str = "예외 발생") -> None:
    log(stage, f"{message} — {type(exc).__name__}: {exc}", level="ERROR")
    for line in traceback.format_exc(limit=4).strip().splitlines()[-4:]:
        log(stage, line.strip(), level="DEBUG")


def log_text(entries: Optional[list[dict]] = None) -> str:
    src = entries if entries is not None else _LOG
    out = []
    for e in src:
        line = f"{e['시각']} +{e['경과ms']:>6}ms {e['레벨']:<5} {e['단계']:<16} {e['내용']}"
        if e.get("상세"):
            line += f"  ({e['상세']})"
        out.append(line)
    return "\n".join(out)


@contextlib.contextmanager
def capture_stdout(stage: str):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        for line in buf.getvalue().splitlines():
            if line.strip():
                lvl = "WARN" if ("Error" in line or "실패" in line) else "INFO"
                log(stage, f"pykrx: {line.strip()}", level=lvl)


@contextlib.contextmanager
def timed(stage: str, what: str):
    t = time.perf_counter()
    log(stage, f"{what} 시작")
    try:
        yield
    finally:
        log(stage, f"{what} 종료", ms=int((time.perf_counter() - t) * 1000))


# ──────────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────────

def fmt_eok(v, signed: bool = True) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{'+' if (signed and v > 0) else ''}{v:,.0f}"


def tone_for(v) -> str:
    if v is None or pd.isna(v) or v == 0:
        return C_MUTED
    return C_UP if v > 0 else C_DOWN


def kpi(label: str, value: str, note: str = "", tone: str = C_MUTED) -> str:
    return (f'<div class="cm-kpi" style="--tone:{tone}"><div class="cm-label">{label}</div>'
            f'<div class="cm-value">{value}</div><div class="cm-note">{note}</div></div>')


def _pick(cols: Iterable, *candidates: str) -> Optional[str]:
    cols = list(cols)
    for cand in candidates:
        for c in cols:
            if str(c).strip() == cand:
                return c
    for cand in candidates:
        for c in cols:
            if cand in str(c):
                return c
    return None


DETERMINISTIC = (KeyError, AttributeError, TypeError, IndexError)


def _retry(fn: Callable, stage: str, tries: int = 3, delay: float = 0.8):
    last = None
    for i in range(tries):
        try:
            return fn()
        except DETERMINISTIC as e:
            log(stage, f"재시도 생략 (결정적 오류) — {type(e).__name__}: {e}", level="WARN")
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            log(stage, f"재시도 {i+1}/{tries} — {type(e).__name__}: {e}", level="WARN")
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def secret(key: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(key, default) or default)
    except Exception:  # noqa: BLE001
        return default


# ──────────────────────────────────────────────────────────────────────────────
# pykrx 임포트 및 KRX 인증
# ──────────────────────────────────────────────────────────────────────────────

_STOCK = None
AUTH_STATE: dict = {"시도": False, "성공": False, "계정": "", "메시지": "미시도"}


def get_stock_api(krx_id: str = "", krx_pw: str = ""):
    global _STOCK
    krx_id = krx_id or os.getenv("KRX_ID", "") or secret("KRX_ID")
    krx_pw = krx_pw or os.getenv("KRX_PW", "") or secret("KRX_PW")
    if krx_id:
        os.environ["KRX_ID"] = krx_id
    if krx_pw:
        os.environ["KRX_PW"] = krx_pw

    if _STOCK is None:
        log("임포트", "pykrx 임포트", 자격증명="있음" if (krx_id and krx_pw) else "없음")
        with capture_stdout("임포트"):
            from pykrx import stock as _s
        _STOCK = _s
        try:
            import pykrx
            log("임포트", "pykrx 준비 완료", 버전=getattr(pykrx, "__version__", "unknown"))
        except Exception:  # noqa: BLE001
            pass

    if not (krx_id and krx_pw):
        AUTH_STATE.update(시도=False, 성공=False, 계정="",
                          메시지="KRX_ID / KRX_PW 미설정 — 익명 요청은 KRX 가 거부합니다")
        log("인증", AUTH_STATE["메시지"], level="WARN")
        return _STOCK

    if AUTH_STATE.get("성공") and AUTH_STATE.get("계정") == krx_id:
        return _STOCK

    AUTH_STATE.update(시도=True, 계정=krx_id)
    try:
        from pykrx.website.comm.auth import build_krx_session, set_auth_session
        with capture_stdout("인증"):
            session = build_krx_session(krx_id, krx_pw)
        if session is None:
            AUTH_STATE.update(성공=False, 메시지="로그인 실패 — 아이디 또는 비밀번호를 확인하세요")
            log("인증", AUTH_STATE["메시지"], level="ERROR", 계정=krx_id)
        else:
            set_auth_session(session)
            AUTH_STATE.update(성공=True, 메시지="로그인 성공")
            log("인증", "KRX 로그인 성공", 계정=krx_id)
    except Exception as e:  # noqa: BLE001
        AUTH_STATE.update(성공=False, 메시지=f"로그인 중 예외 — {type(e).__name__}: {e}")
        log_exc("인증", e, "KRX 로그인 실패")
    return _STOCK


# ──────────────────────────────────────────────────────────────────────────────
# 진단
# ──────────────────────────────────────────────────────────────────────────────

def probe_krx(krx_id: str = "", krx_pw: str = "") -> dict:
    import requests

    end = now_kst().strftime("%Y%m%d")
    start = (now_kst() - timedelta(days=14)).strftime("%Y%m%d")
    payload = {"bld": "dbms/MDC/STAT/standard/MDCSTAT00301", "indIdx": "1", "indIdx2": "001",
               "strtDd": start, "endDd": end, "share": "2", "money": "3", "csvxls_isNo": "false"}
    session = None
    if krx_id and krx_pw:
        try:
            from pykrx.website.comm.auth import build_krx_session
            with capture_stdout("프로브"):
                krxs = build_krx_session(krx_id, krx_pw)
            session = getattr(krxs, "session", None)
        except Exception as e:  # noqa: BLE001
            log_exc("프로브", e, "프로브용 로그인 실패")

    out: dict = {"조회기간": f"{start}~{end}", "인증세션": "사용" if session else "미사용"}
    try:
        r = (session or requests.Session()).post(
            KRX_JSON_URL, headers=KRX_HEADERS, data=payload, timeout=20)
        body = r.text[:300]
        out.update({"HTTP상태": r.status_code, "ContentType": r.headers.get("Content-Type", "?"),
                    "응답길이": len(r.content), "응답앞부분": body})
        if r.text.strip().upper() == "LOGOUT" or (r.status_code == 400 and len(r.content) < 32):
            out["판정"] = "인증 필요 — KRX 가 로그인되지 않은 요청을 거부했습니다"
        else:
            try:
                js = r.json()
                key = next((k for k in js if isinstance(js[k], list)), None)
                rows = len(js[key]) if key else 0
                out["데이터행수"] = rows
                out["판정"] = f"정상 — {rows}행 수신" if rows else "JSON 은 왔으나 데이터가 비었습니다"
            except Exception:  # noqa: BLE001
                low = body.lower()
                out["판정"] = ("HTML 응답 — 서버 IP 차단 가능성"
                             if "<html" in low or "<!doctype" in low else "예상치 못한 응답 형식")
    except Exception as e:  # noqa: BLE001
        out["판정"] = f"요청 실패 — {type(e).__name__}: {e}"
        log_exc("프로브", e, "KRX 직접 요청 실패")
    log("프로브", "KRX 직접 요청 완료", 판정=out.get("판정", "?"))
    return out


def classify_failure(exc: BaseException, entries: list[dict]) -> tuple[str, str]:
    blob = " ".join(e["내용"] for e in entries)
    msg = f"{type(exc).__name__}: {exc}"
    if not AUTH_STATE.get("성공"):
        if not AUTH_STATE.get("시도"):
            return ("KRX 계정이 설정되지 않았습니다.",
                    "KRX 는 로그인하지 않은 요청에 본문 'LOGOUT' 과 HTTP 400 을 돌려줍니다. "
                    "data.krx.co.kr 일반 회원가입 후 Secrets 에 KRX_ID / KRX_PW 를 넣으세요.")
        return ("KRX 로그인에 실패했습니다.",
                f"{AUTH_STATE.get('메시지')} — 브라우저에서 data.krx.co.kr 로그인이 되는지 먼저 확인하세요.")
    if "지수명" in blob:
        return ("pykrx 의 지수명 조회 단계가 실패했습니다.",
                "시세 자체는 정상입니다. name_display=False 로 우회하도록 되어 있으니 app.py 가 최신인지 확인하세요.")
    if "Expecting value" in blob or "JSONDecodeError" in blob:
        return ("KRX 가 JSON 이 아닌 응답을 보냈습니다.",
                "세션이 만료됐을 수 있습니다. 진단 탭에서 원본 응답을 확인하세요.")
    if "KeyError" in msg or "KeyError" in blob:
        return ("응답의 컬럼 구조가 예상과 다릅니다.", "pykrx 를 최신으로 올려 보세요.")
    if "Timeout" in msg or "Connection" in msg:
        return ("KRX 서버에 연결하지 못했습니다.", "일시적 장애일 수 있습니다. 잠시 후 새로고침하세요.")
    return ("원인을 자동 분류하지 못했습니다.", "진단 탭의 전체 실행 로그를 확인하세요.")


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 레이어
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_index(start: str, end: str) -> pd.DataFrame:
    """코스피 지수 OHLCV 전체. name_display 는 장식용이라 끕니다 (KeyError 회피)."""
    stock = get_stock_api()
    attempts = [("get_index_ohlcv", {"name_display": False}),
                ("get_index_ohlcv_by_date", {"name_display": False}),
                ("get_index_ohlcv", {}), ("get_index_ohlcv_by_date", {})]
    for name, kwargs in attempts:
        fn = getattr(stock, name, None)
        if fn is None:
            continue
        label = f"{name}({'name_display=False' if kwargs else '기본'})"
        try:
            with capture_stdout("지수조회"):
                df = _retry(lambda: fn(start, end, KOSPI_INDEX_TICKER, **kwargs), "지수조회", tries=2)
        except TypeError as e:
            log("지수조회", f"{label} 인자 미지원 — {e}", level="DEBUG")
            continue
        except Exception as e:  # noqa: BLE001
            log_exc("지수조회", e, f"{label} 호출 실패")
            continue
        if df is None or df.empty:
            log("지수조회", f"{label} 빈 결과", level="WARN")
            continue

        out = pd.DataFrame(index=pd.to_datetime(df.index))
        for want, cands in [("시가", ("시가",)), ("고가", ("고가",)),
                            ("저가", ("저가",)), ("종가", ("종가", "close")),
                            ("거래량", ("거래량",)), ("거래대금", ("거래대금",))]:
            col = _pick(df.columns, *cands)
            if col is not None:
                out[want] = pd.to_numeric(df[col], errors="coerce")
        if "종가" not in out.columns:
            log("지수조회", f"{label} 종가 없음", level="WARN",
                컬럼=",".join(map(str, df.columns))[:120])
            continue
        # OHLC 가 없으면 종가로 대체해 캔들이 최소한 그려지게
        for c in ["시가", "고가", "저가"]:
            if c not in out.columns:
                out[c] = out["종가"]
        out = out.dropna(subset=["종가"])
        log("지수조회", f"{label} 성공", 행수=len(out), 컬럼=",".join(out.columns))
        return out
    return pd.DataFrame()


def _normalize_flow(raw: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """원 단위 → 억원. 상세 응답이면 기관·외국인을 세부 주체까지 보존합니다."""
    df = raw.copy()
    df.index = pd.to_datetime(df.index)
    cols = list(df.columns)
    detailed = all(_pick(cols, x) is not None for x in ("금융투자", "연기금", "투신"))
    out = pd.DataFrame(index=df.index)

    if detailed:
        for name in INSTITUTIONS + ["기타법인", "개인"] + FOREIGNERS:
            col = _pick(cols, name)
            out[name] = pd.to_numeric(df[col], errors="coerce") if col else 0.0
        out["기관"] = out[INSTITUTIONS].sum(axis=1)
        out["외국인"] = out[FOREIGNERS].sum(axis=1)
        log("수급정규화", "상세 분해 사용", 세부주체=len(INSTITUTIONS) + len(FOREIGNERS))
    else:
        mapping = {"개인": _pick(cols, "개인"),
                   "외국인": _pick(cols, "외국인합계", "외국인"),
                   "기관": _pick(cols, "기관합계", "기관"),
                   "기타법인": _pick(cols, "기타법인")}
        log("수급정규화", "기본 4주체", **{k: (v or "없음") for k, v in mapping.items()})
        for key, col in mapping.items():
            out[key] = pd.to_numeric(df[col], errors="coerce") if col else 0.0

    return (out / 1e8).round(0), detailed


def _fetch_flow(start: str, end: str) -> tuple[pd.DataFrame, bool]:
    stock = get_stock_api()
    fn = getattr(stock, "get_market_trading_value_by_date", None)
    if fn is None:
        raise AttributeError("get_market_trading_value_by_date 없음")

    for detail in (True, False):
        try:
            with capture_stdout("수급조회"):
                df = _retry(lambda: fn(start, end, "KOSPI", detail=detail), "수급조회", tries=2)
        except TypeError:
            if detail:
                log("수급조회", "detail 인자 미지원 — 기본 조회로 전환", level="WARN")
                continue
            raise
        except Exception as e:  # noqa: BLE001
            log_exc("수급조회", e, f"detail={detail} 조회 실패")
            continue
        if df is None or df.empty:
            log("수급조회", f"detail={detail} 빈 결과", level="WARN")
            continue
        log("수급조회", f"detail={detail} 성공", 행수=len(df))
        return _normalize_flow(df)

    raise RuntimeError("투자자별 수급 데이터를 가져오지 못했습니다")


@st.cache_data(ttl=1800, show_spinner=False)
def load_market(lookback: int, ma_window: int, auth_key: str) -> tuple[pd.DataFrame, dict]:
    log_reset()
    log("시작", "데이터 수집 시작", 조회일수=lookback, 이동평균=ma_window)
    meta: dict = {"경고": []}
    today = now_kst()
    pad = int((lookback + ma_window) * 2.2) + 40
    start = (today - timedelta(days=pad)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    get_stock_api()

    with timed("지수조회", f"코스피 지수 {start}~{end}"):
        idx = _fetch_index(start, end)
    if idx.empty:
        meta["로그"] = list(_LOG)
        raise RuntimeError(f"코스피 지수 응답이 비어 있습니다 ({start}~{end})")

    idx["MA"] = idx["종가"].rolling(ma_window, min_periods=max(2, ma_window // 2)).mean()

    with timed("수급조회", f"투자자별 순매수 {start}~{end}"):
        flow, detailed = _fetch_flow(start, end)
    meta["상세분해"] = detailed
    if not detailed:
        meta["경고"].append("KRX 상세 응답을 받지 못해 기관 세부 분해를 표시할 수 없습니다.")

    df = flow.join(idx, how="inner").sort_index()
    before = len(df)
    df = df[df[INVESTORS].abs().sum(axis=1) > 0]
    if before != len(df):
        log("병합", "무거래일 제거", 제거=before - len(df))
    if df.empty:
        meta["로그"] = list(_LOG)
        raise RuntimeError("수급·지수 병합 결과가 비어 있습니다")

    df = df.tail(lookback)
    meta.update({"행수": len(df), "수집시각": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
                 "최근영업일": f"{df.index[-1]:%Y-%m-%d}", "인증": dict(AUTH_STATE)})
    log("완료", "데이터 수집 완료", 행수=len(df), 상세분해=detailed)
    meta["로그"] = list(_LOG)
    return df, meta


@st.cache_data(ttl=1800, show_spinner=False)
def load_net_purchases(days: int, investor: str, auth_key: str) -> pd.DataFrame:
    """투자자별 순매수 상위 종목. '기타법인'의 자금이 어디로 갔는지 보는 창구입니다."""
    stock = get_stock_api()
    end = now_kst().strftime("%Y%m%d")
    start = (now_kst() - timedelta(days=int(days * 1.6) + 8)).strftime("%Y%m%d")
    log("종목조회", f"{investor} 순매수 상위", 기간=f"{start}~{end}")
    with capture_stdout("종목조회"):
        df = _retry(
            lambda: stock.get_market_net_purchases_of_equities(start, end, "KOSPI", investor),
            "종목조회", tries=2,
        )
    if df is None or df.empty:
        raise RuntimeError(f"{investor} 순매수 상위 종목을 가져오지 못했습니다")
    val = _pick(df.columns, "순매수거래대금")
    vol = _pick(df.columns, "순매수거래량")
    name = _pick(df.columns, "종목명")
    out = pd.DataFrame(index=df.index)
    out["종목명"] = df[name] if name else df.index
    out["순매수(억원)"] = (pd.to_numeric(df[val], errors="coerce") / 1e8).round(0) if val else 0
    if vol:
        out["순매수수량"] = pd.to_numeric(df[vol], errors="coerce")
    log("종목조회", f"{investor} 상위 수신", 종목수=len(out))
    return out.sort_values("순매수(억원)", ascending=False)


# ──────────────────────────────────────────────────────────────────────────────
# 시그널 엔진 v2
# ──────────────────────────────────────────────────────────────────────────────

RULES = [
    {"key": "foreign", "w": 25, "label": "외국인 {n}일 누적 순매수 > 0",
     "why": "코스피 방향성의 1차 동인", "needs": None},
    {"key": "inst", "w": 15, "label": "기관 {n}일 누적 순매수 > 0",
     "why": "추세의 지속성을 뒷받침", "needs": None},
    {"key": "pension", "w": 10, "label": "연기금 {n}일 누적 순매수 > 0",
     "why": "국민연금 등 장기 자금. 하방에서 먼저 들어오는 경향", "needs": "연기금"},
    {"key": "corp", "w": 10, "label": "기타법인 {n}일 누적 순매수 > 0",
     "why": "자사주 매입 등 하방 지지 매수", "needs": None},
    {"key": "retail", "w": 5, "label": "개인 {n}일 누적 순매도 (< 0)",
     "why": "역발상 관점의 바닥 신호", "needs": None},
    {"key": "trend", "w": 15, "label": "종가 > {ma}일 이동평균",
     "why": "추세 필터. 하락장 매수를 걸러냄", "needs": None},
    {"key": "streak", "w": 10, "label": "외국인 3일 연속 순매수",
     "why": "수급 모멘텀의 지속성", "needs": None},
    {"key": "zscore", "w": 10, "label": "외국인 순매수 강도 z > 0.5",
     "why": "평소 대비 이례적으로 강한 매수인가", "needs": None},
]


def _streak(series: pd.Series) -> pd.Series:
    pos = (series > 0).astype(int)
    grp = (pos != pos.shift()).cumsum()
    return pos.groupby(grp).cumsum()


def compute_conditions(d: pd.DataFrame, short: int) -> tuple[pd.DataFrame, dict]:
    roll = {c: d[c].rolling(short, min_periods=1).sum() for c in INVESTORS if c in d.columns}
    if "연기금" in d.columns:
        roll["연기금"] = d["연기금"].rolling(short, min_periods=1).sum()

    f_mean = d["외국인"].rolling(20, min_periods=5).mean()
    f_std = d["외국인"].rolling(20, min_periods=5).std()
    z = ((d["외국인"] - f_mean) / f_std.replace(0, pd.NA)).astype(float)

    conds = pd.DataFrame(index=d.index)
    conds["foreign"] = roll["외국인"] > 0
    conds["inst"] = roll["기관"] > 0
    conds["pension"] = roll["연기금"] > 0 if "연기금" in roll else False
    conds["corp"] = roll["기타법인"] > 0
    conds["retail"] = roll["개인"] < 0
    conds["trend"] = d["종가"] > d["MA"]
    conds["streak"] = _streak(d["외국인"]) >= 3
    conds["zscore"] = z > 0.5
    return conds.fillna(False), {"roll": roll, "z": z}


def build_signals(df: pd.DataFrame, short: int = 5, ma_window: int = 20) -> dict:
    d = df.copy()
    conds, extra = compute_conditions(d, short)
    roll, z = extra["roll"], extra["z"]

    active = [r for r in RULES if r["needs"] is None or r["needs"] in d.columns]
    total_w = sum(r["w"] for r in active)
    raw = sum(conds[r["key"]].astype(int) * r["w"] for r in active)
    score_series = (pd.Series(raw, index=d.index) / total_w * 100).round().astype(int)
    score_series.name = "수급스코어"

    last = d.index[-1]
    detail_map = {
        "foreign": f"{fmt_eok(roll['외국인'].loc[last])} 억",
        "inst": f"{fmt_eok(roll['기관'].loc[last])} 억",
        "pension": f"{fmt_eok(roll['연기금'].loc[last])} 억" if "연기금" in roll else "데이터 없음",
        "corp": f"{fmt_eok(roll['기타법인'].loc[last])} 억",
        "retail": f"{fmt_eok(roll['개인'].loc[last])} 억",
        "trend": (f"{d['종가'].loc[last]:,.2f} vs MA {d['MA'].loc[last]:,.2f}"
                  if pd.notna(d["MA"].loc[last]) else "MA 산출 불가"),
        "streak": f"{int(_streak(d['외국인']).loc[last])}일 연속",
        "zscore": f"z = {z.loc[last]:+.2f}" if pd.notna(z.loc[last]) else "산출 불가",
    }
    triggers = [
        {"text": r["label"].format(n=short, ma=ma_window), "weight": r["w"], "why": r["why"],
         "met": bool(conds[r["key"]].loc[last]), "detail": detail_map[r["key"]],
         "active": r in active}
        for r in RULES
    ]

    # 다이버전스: 지수와 외국인 수급의 방향이 엇갈리는가
    ret5 = d["종가"].pct_change(short)
    f5 = roll["외국인"]
    if pd.notna(ret5.loc[last]):
        if ret5.loc[last] <= 0 and f5.loc[last] > 0:
            div = ("축적", "지수는 눌렸는데 외국인은 사고 있습니다. 바닥권 매집 가능성.", C_UP)
        elif ret5.loc[last] > 0 and f5.loc[last] < 0:
            div = ("분산", "지수는 올랐는데 외국인은 팔고 있습니다. 상승 신뢰도 하락.", C_DOWN)
        else:
            div = ("동행", "지수와 외국인 수급이 같은 방향입니다.", C_MUTED)
    else:
        div = ("판정불가", "표본이 부족합니다.", C_MUTED)

    chg = d["종가"].pct_change()
    corr = d["외국인"].tail(20).corr(chg.tail(20))

    return {
        "score": int(score_series.loc[last]),
        "score_series": score_series,
        "triggers": triggers,
        "rolling": {k: float(v.loc[last]) for k, v in roll.items()},
        "zscore": float(z.loc[last]) if pd.notna(z.loc[last]) else float("nan"),
        "streak_foreign": int(_streak(d["외국인"]).loc[last]),
        "corr_foreign_index": float(corr) if pd.notna(corr) else float("nan"),
        "divergence": div,
        "ret_short": float(ret5.loc[last]) if pd.notna(ret5.loc[last]) else float("nan"),
        "active_weight": total_w,
        "short": short,
        "ma_window": ma_window,
    }


def allocation_guide(score: int, cash_ratio: int) -> dict:
    if score >= 85:
        band, dep, verd, tone = "적극 매수 구간", 0.60, "수급 주체 정렬 + 추세 확인. 분할 매수 가속.", C_UP
    elif score >= 65:
        band, dep, verd, tone = "매수 우위 구간", 0.40, "핵심 주체 매수 우위. 계획된 분할 매수 진행.", C_UP
    elif score >= 45:
        band, dep, verd, tone = "중립 상단", 0.25, "1차 진입 가능. 미충족 트리거 확인 후 소량.", C_ACCENT
    elif score >= 25:
        band, dep, verd, tone = "중립 하단", 0.10, "관찰 우위. 테스트 물량 수준으로 제한.", C_ACCENT
    else:
        band, dep, verd, tone = "관망 구간", 0.0, "수급 이탈. 현금 비중 유지.", C_DOWN
    return {"band": band, "deploy_ratio": dep, "total_weight": cash_ratio * dep,
            "verdict": verd, "tone": tone}


def band_of(score: int) -> str:
    for lo, name in [(85, "85-100"), (65, "65-84"), (45, "45-64"), (25, "25-44"), (0, "0-24")]:
        if score >= lo:
            return name
    return "0-24"


def backtest(df: pd.DataFrame, short: int, ma_window: int, horizons=(5, 10, 20)) -> pd.DataFrame:
    """스코어 구간별 N영업일 후 지수 수익률. 표본이 겹치므로 참고용입니다."""
    sig = build_signals(df, short=short, ma_window=ma_window)
    score = sig["score_series"]
    close = df["종가"]
    rows = []
    for h in horizons:
        fwd = close.shift(-h) / close - 1
        tmp = pd.DataFrame({"score": score, "fwd": fwd}).dropna()
        if tmp.empty:
            continue
        tmp["구간"] = tmp["score"].map(band_of)
        for band, g in tmp.groupby("구간"):
            rows.append({
                "구간": band, "기간": f"{h}일 후", "표본": len(g),
                "평균수익률(%)": round(g["fwd"].mean() * 100, 2),
                "중앙값(%)": round(g["fwd"].median() * 100, 2),
                "승률(%)": round((g["fwd"] > 0).mean() * 100, 1),
            })
    if not rows:
        return pd.DataFrame()
    order = ["85-100", "65-84", "45-64", "25-44", "0-24"]
    out = pd.DataFrame(rows)
    out["_o"] = out["구간"].map({b: i for i, b in enumerate(order)})
    return out.sort_values(["기간", "_o"]).drop(columns="_o").reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
# 차트
# ──────────────────────────────────────────────────────────────────────────────

def candlestick_figure(df: pd.DataFrame, ma_window: int,
                       flow_picks: Optional[list[str]] = None,
                       barmode: str = "relative"):
    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    flow_picks = [c for c in (flow_picks or []) if c in df.columns]
    show_flow = bool(flow_picks)
    rows = 3 if show_flow else 2
    heights = [0.56, 0.18, 0.26] if show_flow else [0.75, 0.25]
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=heights)

    fig.add_trace(
        go.Candlestick(
            x=df.index, open=df["시가"], high=df["고가"], low=df["저가"], close=df["종가"],
            name="코스피",
            increasing=dict(line=dict(color=C_UP, width=1), fillcolor=C_UP),
            decreasing=dict(line=dict(color=C_DOWN, width=1), fillcolor=C_DOWN),
        ),
        row=1, col=1,
    )
    if "MA" in df.columns:
        fig.add_trace(
            go.Scatter(x=df.index, y=df["MA"], name=f"MA{ma_window}", mode="lines",
                       line=dict(color=C_ACCENT, width=1.4)),
            row=1, col=1,
        )

    if "거래량" in df.columns:
        up = df["종가"] >= df["시가"]
        fig.add_trace(
            go.Bar(x=df.index, y=df["거래량"], name="거래량",
                   marker_color=[C_UP if u else C_DOWN for u in up],
                   marker_line_width=0, opacity=0.55, showlegend=False),
            row=2, col=1,
        )

    if show_flow:
        for name in flow_picks:
            fig.add_trace(
                go.Bar(x=df.index, y=df[name], name=name,
                       marker_color=FLOW_COLORS.get(name, C_MUTED),
                       marker_line_width=0, opacity=0.85),
                row=3, col=1,
            )
        fig.update_layout(barmode=barmode)

    # 휴장일 공백 제거
    all_days = pd.date_range(df.index.min(), df.index.max(), freq="D")
    missing = [d for d in all_days.difference(df.index)]
    if missing:
        fig.update_xaxes(rangebreaks=[dict(values=missing)])

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=C_TEXT, size=12),
        height=660, margin=dict(l=8, r=8, t=28, b=8),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, bgcolor="rgba(0,0,0,0)"),
        hovermode="x unified",
        dragmode="pan",
    )
    grid = "rgba(255,255,255,.06)"
    fig.update_xaxes(showgrid=True, gridcolor=grid)
    fig.update_yaxes(showgrid=True, gridcolor=grid, zeroline=False)
    fig.update_yaxes(title_text="지수", row=1, col=1)
    fig.update_yaxes(title_text="거래량", row=2, col=1)
    if show_flow:
        fig.update_yaxes(title_text="순매수(억)", row=3, col=1)
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# LLM
# ──────────────────────────────────────────────────────────────────────────────

def resolve_groq_model(api_key: str, wanted: str = "auto") -> tuple[str, list[str]]:
    from groq import Groq
    available: list[str] = []
    try:
        available = sorted(m.id for m in Groq(api_key=api_key).models.list().data)
        log("LLM", "모델 목록 조회", 개수=len(available))
    except Exception as e:  # noqa: BLE001
        log_exc("LLM", e, "모델 목록 조회 실패")
    if wanted != "auto":
        return wanted, available
    for m in PREFERRED_MODELS:
        if not available or m in available:
            return m, available
    return (available[0] if available else PREFERRED_MODELS[0]), available


def build_prompt(df: pd.DataFrame, sig: dict, guide: dict, cash_ratio: int,
                 corp_top: Optional[pd.DataFrame] = None) -> str:
    table = df.tail(15).copy()
    table.index = table.index.strftime("%Y-%m-%d")
    cols = [c for c in INVESTORS + ["종가"] if c in table.columns]

    checklist = "\n".join(
        f"- [{'충족' if t['met'] else '미충족'}] {t['text']} (가중치 {t['weight']}) → {t['detail']}"
        for t in sig["triggers"] if t["active"]
    )
    inst_line = ""
    if "연기금" in df.columns:
        tail = df[INSTITUTIONS].tail(sig["short"]).sum()
        inst_line = "- 기관 세부 " + str(sig["short"]) + "일 누적: " + " / ".join(
            f"{k} {v:,.0f}억" for k, v in tail.items()) + "\n"
    corp_line = ""
    if corp_top is not None and not corp_top.empty:
        top = corp_top.head(8)
        corp_line = ("- 기타법인 순매수 상위 종목: "
                     + ", ".join(f"{r['종목명']} {r['순매수(억원)']:,.0f}억"
                                 for _, r in top.iterrows()) + "\n")

    div_name, div_desc, _ = sig["divergence"]
    return f"""[분석 대상] 코스피 시장 투자자별 순매수 (단위: 억원)

[최근 {len(table)} 영업일]
{table[cols].to_string()}

[정량 지표 — 계산 완료된 값이므로 재계산하지 마십시오]
- {sig['short']}일 누적 순매수: 외국인 {sig['rolling']['외국인']:,.0f}억 / 기관 {sig['rolling']['기관']:,.0f}억 / 기타법인 {sig['rolling']['기타법인']:,.0f}억 / 개인 {sig['rolling']['개인']:,.0f}억
{inst_line}{corp_line}- 외국인 순매수 z-score(20일 기준): {sig['zscore']:+.2f}
- 외국인 연속 순매수: {sig['streak_foreign']}일
- 외국인 순매수 ↔ 지수 등락률 20일 상관계수: {sig['corr_foreign_index']:+.2f}
- 지수 {sig['short']}일 수익률: {sig['ret_short']*100:+.2f}%
- 수급 다이버전스 판정: {div_name} ({div_desc})
- 규칙 기반 수급 스코어: {sig['score']}/100 ({guide['band']})

[트리거 체크리스트]
{checklist}

[포트폴리오]
- 현금 비중 {cash_ratio}% / 규칙 엔진 권고: 보유 현금의 {guide['deploy_ratio']*100:.0f}% 투입

[작성 지침]
마크다운 소제목(##)으로 5개 섹션, 각 4문장 이내, 한국어.
## 1. 시장 국면 진단
수급 주체별 방향성과 지수 흐름의 정합성. 다이버전스 판정을 근거로 활용.
## 2. 자금 성격 분석
연기금·투신 등 기관 세부 주체와 기타법인의 매수 성격 구분. 데이터가 없으면 없다고 명시.
## 3. 규칙 엔진에 대한 반론
스코어가 놓친 위험 또는 과대평가 요소. 동의만 하지 말 것.
## 4. 현금 투입 트리거
관측 가능한 조건으로. 수치와 기간 명시.
## 5. 무효화 조건
시나리오를 폐기해야 하는 구체적 이탈 신호.

데이터에 없는 뉴스·이벤트를 지어내지 마십시오."""


def stream_report(api_key: str, model: str, prompt: str):
    from groq import Groq
    log("LLM", "리포트 스트리밍 시작", 모델=model, 프롬프트길이=len(prompt))
    stream = Groq(api_key=api_key).chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content":
             "당신은 한국 주식시장 수급 데이터를 다루는 퀀트 전략가입니다. 제공된 수치만 근거로 "
             "삼고, 확신의 정도를 구분해 표현하며, 듣기 좋은 말보다 반증 가능한 조건을 제시합니다."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.15, max_tokens=1800, stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


# ──────────────────────────────────────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.subheader("설정")
    groq_api_key = st.text_input("Groq API Key", value=secret("GROQ_API_KEY"), type="password")
    with st.expander("KRX 계정", expanded=not secret("KRX_ID")):
        st.caption("KRX 는 로그인하지 않은 요청을 거부합니다.")
        krx_id = st.text_input("KRX 아이디", value=secret("KRX_ID"))
        krx_pw = st.text_input("KRX 비밀번호", value=secret("KRX_PW"), type="password")

    lookback = st.slider("조회 기간 (영업일)", 20, 250, 90, step=10)
    ma_window = st.select_slider("추세 필터 이동평균", options=[5, 10, 20, 60, 120], value=20)
    short = st.select_slider("누적 순매수 창", options=[3, 5, 10, 20], value=5)
    cash_ratio = st.slider("현재 현금 비중 (%)", 0, 100, 70, step=5)
    model_choice = st.selectbox("LLM 모델", ["auto"] + PREFERRED_MODELS)

    st.divider()
    if st.button("데이터 새로고침", use_container_width=True):
        load_market.clear()
        load_net_purchases.clear()
        st.session_state.pop("report", None)
        st.rerun()
    st.caption("데이터: KRX Data Marketplace · 30분 캐시")

if krx_id:
    os.environ["KRX_ID"] = krx_id
if krx_pw:
    os.environ["KRX_PW"] = krx_pw

st.markdown(
    f'<div class="cm-head"><h1>CENTURION Macro</h1>'
    f'<span class="cm-asof">코스피 투자자별 수급 · {now_kst():%Y-%m-%d %H:%M} KST</span></div>'
    f'<div class="cm-sub">캔들 차트 · 투자자 세부 분해 · 기타법인 추적 · 규칙 스코어 백테스트</div>',
    unsafe_allow_html=True,
)

df: Optional[pd.DataFrame] = None
meta: dict = {}
load_error: Optional[BaseException] = None

with st.spinner("KRX 데이터를 불러오는 중…"):
    try:
        df, meta = load_market(lookback, ma_window, krx_id)
    except Exception as e:  # noqa: BLE001
        load_error = e
        log_exc("치명", e, "데이터 수집 중단")

if load_error is not None:
    cause, action = classify_failure(load_error, _LOG)
    st.error(f"**{cause}**\n\n{action}")
    if not AUTH_STATE.get("성공"):
        st.info("data.krx.co.kr 일반 회원가입(무료) 후 Secrets 에 KRX_ID / KRX_PW 를 추가하세요.",
                icon="🔑")
    st.markdown("### 실행 로그")
    st.code(log_text() or "(로그 없음)", language="text")
    st.download_button("로그 내려받기", data=log_text().encode("utf-8"),
                       file_name=f"centurion_log_{now_kst():%Y%m%d_%H%M%S}.txt")
    with st.expander("KRX 원본 응답 확인"):
        if st.button("KRX 서버에 직접 요청"):
            st.json(probe_krx(krx_id, krx_pw))
    st.stop()

assert df is not None
sig = build_signals(df, short=short, ma_window=ma_window)
guide = allocation_guide(sig["score"], cash_ratio)
for w in meta.get("경고", []):
    st.warning(w, icon="⚠️")

# ──────────────────────────────────────────────────────────────────────────────
# KPI
# ──────────────────────────────────────────────────────────────────────────────

last = df.index[-1]
prev = df["종가"].iloc[-2] if len(df) > 1 else df["종가"].iloc[-1]
chg = df["종가"].iloc[-1] - prev
chg_pct = (chg / prev * 100) if prev else 0.0

c = st.columns(5)
c[0].markdown(kpi("코스피 종가", f"{df['종가'].iloc[-1]:,.2f}",
                  f"{chg:+,.2f} ({chg_pct:+.2f}%) · {last:%m/%d}", tone_for(chg)),
              unsafe_allow_html=True)
for i, name in enumerate(["외국인", "기관", "기타법인", "개인"]):
    v = sig["rolling"][name]
    c[i + 1].markdown(kpi(f"{name} {short}일 누적", f"{fmt_eok(v)} 억",
                          f"당일 {fmt_eok(df[name].iloc[-1])} 억", tone_for(v)),
                      unsafe_allow_html=True)

st.write("")

left, right = st.columns([1, 1.35])
with left:
    st.markdown(
        f'<div class="cm-score"><div class="cm-band">수급 스코어 · {guide["band"]}</div>'
        f'<div class="cm-big" style="color:{guide["tone"]}">{sig["score"]}'
        f'<span style="font-size:1rem;color:#6E7787;font-weight:500"> / 100</span></div>'
        f'<div class="cm-verdict">{guide["verdict"]}</div>'
        f'<div class="cm-bar"><div style="width:{sig["score"]}%;background:{guide["tone"]}"></div></div>'
        f"</div>", unsafe_allow_html=True)
    st.write("")
    d1 = st.columns(2)
    d1[0].markdown(kpi("권고 투입 (현금 대비)", f"{guide['deploy_ratio']*100:.0f}%",
                       f"현금 {cash_ratio}% 중", guide["tone"]), unsafe_allow_html=True)
    d1[1].markdown(kpi("전체 자산 대비", f"{guide['total_weight']:.0f}%",
                       f"잔여 현금 {cash_ratio - guide['total_weight']:.0f}%", guide["tone"]),
                   unsafe_allow_html=True)
    dv_name, dv_desc, dv_tone = sig["divergence"]
    st.write("")
    st.markdown(kpi(f"수급 다이버전스 · {dv_name}", f"z {sig['zscore']:+.2f}"
                    if pd.notna(sig["zscore"]) else "z —", dv_desc, dv_tone),
                unsafe_allow_html=True)

with right:
    st.markdown("**트리거 체크리스트**")
    rows = []
    for t in sig["triggers"]:
        if not t["active"]:
            mark, color, w = "➖", "#4A5261", "제외"
        else:
            mark = "✅" if t["met"] else "⬜"
            color = C_UP if t["met"] else "#6E7787"
            w = f"+{t['weight']}"
        rows.append(
            f'<div class="cm-trig"><div class="cm-mark">{mark}</div>'
            f'<div class="cm-text" style="color:{color}">{t["text"]}'
            f'<div class="cm-detail">{t["detail"]} · {t["why"]}</div></div>'
            f'<div class="cm-w">{w}</div></div>')
    st.markdown("".join(rows), unsafe_allow_html=True)
    if sig["active_weight"] != 100:
        st.caption(f"활성 가중치 합 {sig['active_weight']} → 100점 기준으로 환산했습니다.")

st.write("")

# ──────────────────────────────────────────────────────────────────────────────
# 탭
# ──────────────────────────────────────────────────────────────────────────────

tabs = st.tabs(["캔들 차트", "수급 상세", "기타법인 추적", "백테스트", "AI 리포트", "진단"])

with tabs[0]:
    opt = st.columns([2.2, 1, 1])
    picks = opt[0].multiselect("수급 패널에 표시할 투자자", INVESTORS, default=INVESTORS)
    mode_label = opt[1].radio("표시 방식", ["겹쳐보기", "나란히"], horizontal=True)
    n_show = opt[2].number_input("표시 봉 수", 20, len(df), min(len(df), 90), step=10)
    plot_df = df.tail(int(n_show))
    try:
        st.plotly_chart(
            candlestick_figure(plot_df, ma_window, picks,
                               "relative" if mode_label == "겹쳐보기" else "group"),
            use_container_width=True,
            config={"scrollZoom": True, "displaylogo": False})
    except ModuleNotFoundError:
        st.error("plotly 가 설치되지 않았습니다. requirements.txt 에 plotly 를 추가하세요.")
        st.line_chart(plot_df[["종가", "MA"]], color=[C_TEXT, C_ACCENT], height=320)
    st.caption(
        "캔들은 적색 상승·청색 하락(국내 관례). 수급 패널의 색은 투자자 구분입니다 — "
        "개인 초록 / 외국인 적색 / 기관 청색 / 기타법인 주황. 드래그로 이동, 스크롤로 확대."
    )
    st.info(
        "네 주체의 순매수 합계는 항상 0 근처입니다. 누군가 사면 누군가는 팔기 때문입니다. "
        "그래서 **개인은 나머지 셋의 거울상**입니다 — 외국인·기관이 팔면 개인이 받고 있다는 뜻입니다. "
        "'겹쳐보기'로 두면 위쪽이 매수 주체, 아래쪽이 매도 주체로 갈려 한눈에 들어옵니다.",
        icon="ℹ️")

with tabs[1]:
    m = st.columns(4)
    m[0].metric("외국인 연속 순매수", f"{sig['streak_foreign']}일")
    corr = sig["corr_foreign_index"]
    m[1].metric("외국인↔지수 상관 (20일)", "—" if pd.isna(corr) else f"{corr:+.2f}")
    m[2].metric("외국인 z-score", "—" if pd.isna(sig["zscore"]) else f"{sig['zscore']:+.2f}")
    m[3].metric("조회 영업일", f"{len(df)}일")

    st.markdown("**투자자 4주체 일별 순매수 (억원)**")
    g1, g2 = st.columns([1.15, 1])
    with g1:
        st.bar_chart(df[INVESTORS], color=SERIES_COLORS, height=280)
    with g2:
        st.markdown("기간 내 누적")
        st.area_chart(df[INVESTORS].cumsum(), color=SERIES_COLORS, height=252)

    tot = df[INVESTORS].sum()
    resid = float(df[INVESTORS].sum(axis=1).abs().mean())
    tc = st.columns(4)
    for i, name in enumerate(INVESTORS):
        tc[i].markdown(kpi(name, f"{fmt_eok(tot[name])} 억", "기간 누적",
                           FLOW_COLORS.get(name, C_MUTED)), unsafe_allow_html=True)
    st.caption(
        f"네 주체 순매수의 일평균 합계는 {resid:,.0f}억으로 0 에 수렴합니다. "
        "시장은 제로섬이라 개인은 나머지 셋의 거울상이고, 그래서 개인 지표는 독립적인 "
        "정보라기보다 잔차에 가깝습니다."
    )
    st.divider()

    if "연기금" in df.columns:
        st.markdown("**기관 세부 주체별 순매수 (억원)**")
        st.caption("기관합계는 아래 7개 주체의 합입니다. 연기금은 장기 자금, 투신·사모는 상대적으로 단기입니다.")
        st.bar_chart(df[INSTITUTIONS], color=INST_COLORS, height=280)

        cum = df[INSTITUTIONS].sum().sort_values(ascending=False)
        cc = st.columns(len(INSTITUTIONS))
        for i, (name, v) in enumerate(cum.items()):
            cc[i].markdown(kpi(name, f"{fmt_eok(v)}", "기간 누적 억원", tone_for(v)),
                           unsafe_allow_html=True)
        st.write("")
        st.markdown("**외국인 구분**")
        st.caption("'외국인'은 등록 외국인, '기타외국인'은 미등록 외국인입니다.")
        st.bar_chart(df[FOREIGNERS], color=[C_UP, C_ACCENT], height=220)
    else:
        st.info("KRX 상세 응답을 받지 못해 기관 세부 분해를 표시할 수 없습니다.", icon="ℹ️")

    st.markdown("**원본 데이터**")
    cols = [c for c in (INVESTORS + (INSTITUTIONS if "연기금" in df.columns else [])
                        + OHLC + ["거래량", "MA"]) if c in df.columns]
    show = df[cols].copy()
    show.index = show.index.strftime("%Y-%m-%d")
    show.index.name = "날짜"
    flow_cols = [c for c in cols if c in INVESTORS + INSTITUTIONS]
    st.dataframe(
        show.sort_index(ascending=False).style.format(
            {**{c: "{:+,.0f}" for c in flow_cols},
             **{c: "{:,.2f}" for c in OHLC + ["MA"] if c in cols},
             "거래량": "{:,.0f}"}, na_rep="—"),
        use_container_width=True, height=380)
    st.download_button("CSV 내려받기", data=show.to_csv().encode("utf-8-sig"),
                       file_name=f"kospi_flow_{last:%Y%m%d}.csv", mime="text/csv")

with tabs[2]:
    st.markdown("### 기타법인은 누구인가")
    st.markdown(
        "KRX 분류에서 **기타법인**은 금융기관이 아닌 일반 법인입니다. 사업회사, 지주회사, "
        "공익법인 등이 여기 들어갑니다. 이들이 시장에서 주식을 사는 이유는 대체로 셋입니다.\n\n"
        "1. **자기주식(자사주) 취득** — 주가 방어나 주주환원 목적. 가장 큰 비중\n"
        "2. **계열사·관계사 지분 매입** — 지배구조 강화, 지주회사 지분율 확대\n"
        "3. **공익법인 출연 및 기타 법인 거래**\n\n"
        "그래서 실무에서는 기타법인 순매수를 자사주 매입의 대리 지표로 읽습니다. "
        "다만 KRX 집계는 주체를 익명 합산하기 때문에 **어느 회사인지는 집계 데이터만으로 알 수 없습니다.** "
        "대신 어느 *종목*으로 들어갔는지는 알 수 있고, 그게 사실상 같은 질문의 답입니다."
    )
    st.divider()

    cA, cB = st.columns([1, 2])
    inv = cA.selectbox("투자자", ["기타법인", "연기금", "외국인", "기관합계", "개인", "금융투자", "투신"])
    days = cB.slider("집계 기간 (달력일)", 5, 120, 20, step=5, key="np_days")

    if st.button("순매수 상위 종목 조회", type="primary"):
        try:
            with st.spinner(f"{inv} 순매수 상위 종목을 조회하는 중…"):
                top = load_net_purchases(days, inv, krx_id)
            st.session_state["corp_top"] = top
            st.session_state["corp_inv"] = inv
        except Exception as e:  # noqa: BLE001
            log_exc("종목조회", e, "순매수 상위 조회 실패")
            st.error(f"조회 실패 — {type(e).__name__}: {e}")

    top = st.session_state.get("corp_top")
    if top is not None and not top.empty:
        inv_name = st.session_state.get("corp_inv", inv)
        buy = top.head(15)
        sell = top.tail(10).sort_values("순매수(억원)")

        t1, t2 = st.columns(2)
        with t1:
            st.markdown(f"**{inv_name} 순매수 상위 15**")
            st.dataframe(buy.style.format({"순매수(억원)": "{:+,.0f}", "순매수수량": "{:,.0f}"}),
                         use_container_width=True, height=430)
        with t2:
            st.markdown(f"**{inv_name} 순매도 상위 10**")
            st.dataframe(sell.style.format({"순매수(억원)": "{:+,.0f}", "순매수수량": "{:,.0f}"}),
                         use_container_width=True, height=430)

        chart = buy.set_index("종목명")[["순매수(억원)"]].head(12)
        st.markdown(f"**{inv_name} 순매수 집중도**")
        st.bar_chart(chart, color=[C_UP], height=280)

        total = top["순매수(억원)"].sum()
        top5 = buy.head(5)["순매수(억원)"].sum()
        conc = (top5 / total * 100) if total else float("nan")
        k = st.columns(3)
        k[0].markdown(kpi("순매수 총액", f"{fmt_eok(total)} 억", f"최근 {days}일", tone_for(total)),
                      unsafe_allow_html=True)
        k[1].markdown(kpi("상위 5종목 비중", "—" if pd.isna(conc) else f"{conc:.0f}%",
                          "높을수록 특정 종목 집중", C_ACCENT), unsafe_allow_html=True)
        k[2].markdown(kpi("조회 종목 수", f"{len(top)}", "순매수·순매도 합계", C_MUTED),
                      unsafe_allow_html=True)

        if inv_name == "기타법인":
            st.info(
                "상위 종목이 자사주 매입인지 확인하려면 DART(dart.fss.or.kr)에서 해당 종목의 "
                "**'주요사항보고서(자기주식취득결정)'** 또는 **'자기주식 취득 결과보고서'** 공시를 "
                "대조하세요. 공시일과 순매수 급증일이 겹치면 자사주 매입일 가능성이 높습니다. "
                "겹치지 않으면 계열사 지분 매입 쪽을 의심할 만합니다.",
                icon="🔍")
    else:
        st.caption("위 버튼을 눌러 조회하세요. 종목 단위 집계라 시장 전체보다 응답이 조금 느립니다.")

with tabs[3]:
    st.markdown("### 스코어가 실제로 의미가 있었는가")
    st.markdown(
        "현재 조회 구간에서 스코어 구간별로 N영업일 뒤 코스피 수익률을 집계합니다. "
        "스코어가 높을수록 이후 수익률이 좋아야 규칙이 의미를 갖습니다."
    )
    bt_days = st.slider("백테스트 기간 (영업일)", 120, 500, 250, step=25)
    if st.button("백테스트 실행", type="primary"):
        try:
            with st.spinner(f"{bt_days}영업일 데이터로 백테스트 중…"):
                bt_df, _ = load_market(bt_days, ma_window, krx_id)
                res = backtest(bt_df, short, ma_window)
            st.session_state["bt"] = {"res": res, "n": len(bt_df),
                                      "from": f"{bt_df.index[0]:%Y-%m-%d}",
                                      "to": f"{bt_df.index[-1]:%Y-%m-%d}"}
        except Exception as e:  # noqa: BLE001
            log_exc("백테스트", e, "백테스트 실패")
            st.error(f"백테스트 실패 — {type(e).__name__}: {e}")

    bt = st.session_state.get("bt")
    if bt and not bt["res"].empty:
        st.caption(f"표본 구간: {bt['from']} ~ {bt['to']} ({bt['n']}영업일)")
        res = bt["res"]
        for h in res["기간"].unique():
            sub = res[res["기간"] == h].drop(columns="기간")
            st.markdown(f"**{h}**")
            st.dataframe(
                sub.style.format({"평균수익률(%)": "{:+.2f}", "중앙값(%)": "{:+.2f}",
                                  "승률(%)": "{:.1f}", "표본": "{:,.0f}"}),
                use_container_width=True, hide_index=True)
        st.warning(
            "해석 주의: 표본 구간이 겹치기 때문에 관측치가 서로 독립이 아닙니다. 표본 수가 "
            "적은 구간의 평균은 우연일 수 있고, 이 기간이 상승장이었다면 모든 구간이 좋아 보입니다. "
            "구간 간 **순서**가 단조로운지(스코어가 높을수록 수익률이 높은지)를 보는 것이 "
            "절대 수치보다 유용합니다.",
            icon="⚠️")
    elif bt:
        st.info("집계할 표본이 부족합니다. 기간을 늘려 보세요.")

with tabs[4]:
    if not groq_api_key:
        st.info("Groq API Key 를 입력하면 LLM 리포트가 추가됩니다.", icon="🔑")
    else:
        ca, cb = st.columns([1, 3])
        use_corp = cb.checkbox("기타법인 상위 종목을 프롬프트에 포함", value=True,
                               help="'기타법인 추적' 탭에서 조회한 결과가 있으면 함께 전달합니다.")
        if ca.button("리포트 생성", type="primary", use_container_width=True):
            model, available = resolve_groq_model(groq_api_key, model_choice)
            cb.caption(f"모델: `{model}`" + (f" · {len(available)}개 사용 가능" if available else ""))
            corp_top = st.session_state.get("corp_top") if use_corp else None
            if corp_top is not None and st.session_state.get("corp_inv") != "기타법인":
                corp_top = None
            try:
                text = st.write_stream(stream_report(
                    groq_api_key, model,
                    build_prompt(df, sig, guide, cash_ratio, corp_top)))
                st.session_state["report"] = {"text": text, "model": model, "at": now_kst()}
                log("LLM", "리포트 생성 완료", 길이=len(text))
            except Exception as e:  # noqa: BLE001
                log_exc("LLM", e, "리포트 생성 실패")
                st.error(f"리포트 생성 실패 — {type(e).__name__}: {e}")
        elif st.session_state.get("report"):
            r = st.session_state["report"]
            st.caption(f"모델: `{r['model']}` · 생성 {r['at']:%Y-%m-%d %H:%M} KST")
            st.markdown(r["text"])

with tabs[5]:
    s1, s2 = st.columns(2)
    s1.markdown("**KRX 인증**")
    s1.json(meta.get("인증", AUTH_STATE))
    s2.markdown("**데이터 파이프라인**")
    s2.json({"상세분해": meta.get("상세분해"), "수집시각": meta.get("수집시각"),
             "행수": meta.get("행수"), "최근영업일": meta.get("최근영업일"),
             "경고": meta.get("경고") or "없음"})

    st.markdown("**실행 로그**")
    entries = meta.get("로그") or _LOG
    lv = st.multiselect("레벨 필터", ["DEBUG", "INFO", "WARN", "ERROR"],
                        default=["INFO", "WARN", "ERROR"])
    shown = [e for e in entries if e["레벨"] in lv]
    if shown:
        st.dataframe(pd.DataFrame(shown), use_container_width=True, height=320)
    else:
        st.caption("표시할 로그가 없습니다.")
    st.download_button("로그 내려받기", data=log_text(entries).encode("utf-8"),
                       file_name=f"centurion_log_{now_kst():%Y%m%d_%H%M%S}.txt")

    st.markdown("**KRX 원본 응답 확인**")
    if st.button("KRX 서버에 직접 요청", key="probe_tab"):
        st.json(probe_krx(krx_id, krx_pw))

    with st.expander("환경 정보"):
        env = {"python": sys.version.split()[0], "platform": platform.platform(),
               "streamlit": st.__version__, "pandas": pd.__version__}
        for mod in ("pykrx", "plotly"):
            try:
                m_ = __import__(mod)
                env[mod] = getattr(m_, "__version__", "unknown")
            except Exception as e:  # noqa: BLE001
                env[mod] = f"import 실패: {e}"
        st.json(env)

st.divider()
st.markdown(
    '<div class="cm-disc">이 도구는 공개된 KRX 수급 데이터를 정리해 보여주는 참고 자료입니다. '
    "수급 스코어와 투입 비율은 화면에 표시된 고정 규칙의 산술 결과이며, 백테스트 수치는 표본이 "
    "겹치는 구간 통계라 미래 수익을 예측하지 않습니다. 투자 판단과 그 결과에 대한 책임은 "
    "이용자 본인에게 있습니다.</div>",
    unsafe_allow_html=True,
)
