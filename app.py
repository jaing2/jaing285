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
# 종목 스크리너
# ──────────────────────────────────────────────────────────────────────────────
#
# 시장 전체를 훑어 '수급은 유입됐는데 가격은 아직 반응하지 않은' 종목을 걸러냅니다.
# 호출 수는 종목 수와 무관하게 7회 내외입니다 (전종목 일괄 조회 API 사용).
#
# 이것은 후보 필터이며 매수 추천이 아닙니다. 수급은 후행 지표이고,
# 실적·공시·유상증자·소송처럼 가격을 좌우하는 정보는 이 데이터에 없습니다.

SCREEN_INVESTORS = ["외국인", "기관합계", "기타법인", "연기금"]
EXCLUDE_KEYWORDS = ("스팩", "우B", "우C")

SCREEN_RULES = [
    ("외국인 순매수 > 0", 25, "외국인 자금 유입"),
    ("기관 순매수 > 0", 20, "기관 동반 여부"),
    ("수급강도 상위 20%", 25, "시총 대비 유입 규모 — 대형·소형주를 공평하게 비교"),
    ("가격 미반응 (수익률 하위 50%)", 15, "수급은 왔는데 아직 안 오름 = 매집 국면 가정"),
    ("기타법인 순매수 > 0", 15, "자사주 매입 등 하방 지지"),
]


def _biz_day(offset_days: int = 0) -> str:
    stock = get_stock_api()
    base = (now_kst() - timedelta(days=offset_days)).strftime("%Y%m%d")
    try:
        with capture_stdout("스크리너"):
            return stock.get_nearest_business_day_in_a_week(base)
    except Exception:  # noqa: BLE001
        return base


@st.cache_data(ttl=1800, show_spinner=False)
def load_screen_data(days: int, auth_key: str) -> tuple[pd.DataFrame, dict]:
    stock = get_stock_api()
    end = _biz_day(0)
    start = _biz_day(int(days * 1.5) + 5)
    meta = {"시작일": start, "종료일": end}
    log("스크리너", "시장 전체 수집 시작", 기간=f"{start}~{end}")

    flows: dict[str, pd.Series] = {}
    for inv in SCREEN_INVESTORS:
        try:
            with capture_stdout("스크리너"):
                d = _retry(
                    lambda: stock.get_market_net_purchases_of_equities(start, end, "KOSPI", inv),
                    "스크리너", tries=2)
            col = _pick(d.columns, "순매수거래대금")
            if col is None:
                continue
            flows[inv] = pd.to_numeric(d[col], errors="coerce") / 1e8
            nm = _pick(d.columns, "종목명")
            if nm is not None and "종목명" not in meta:
                meta["_names"] = d[nm]
            log("스크리너", f"{inv} 종목별 순매수", 종목수=len(d))
        except Exception as e:  # noqa: BLE001
            log_exc("스크리너", e, f"{inv} 순매수 조회 실패")

    if "외국인" not in flows:
        raise RuntimeError("외국인 종목별 순매수 데이터를 가져오지 못했습니다")

    with capture_stdout("스크리너"):
        cap = _retry(lambda: stock.get_market_cap_by_ticker(end, "KOSPI", alternative=True),
                     "스크리너", tries=2)
        px_e = _retry(lambda: stock.get_market_ohlcv_by_ticker(end, "KOSPI", alternative=True),
                      "스크리너", tries=2)
        px_s = _retry(lambda: stock.get_market_ohlcv_by_ticker(start, "KOSPI", alternative=True),
                      "스크리너", tries=2)
    log("스크리너", "시총·가격 수신", 종목수=len(cap))

    fund = None
    try:
        with capture_stdout("스크리너"):
            fund = _retry(
                lambda: stock.get_market_fundamental_by_ticker(end, "KOSPI", alternative=True),
                "스크리너", tries=2)
    except Exception as e:  # noqa: BLE001
        log("스크리너", f"밸류에이션 생략 — {type(e).__name__}", level="WARN")

    out = pd.DataFrame(index=cap.index)
    out["시가총액"] = pd.to_numeric(cap[_pick(cap.columns, "시가총액")], errors="coerce") / 1e8
    dv = _pick(cap.columns, "거래대금")
    out["거래대금"] = (pd.to_numeric(cap[dv], errors="coerce") / 1e8) if dv else float("nan")
    for inv, sr in flows.items():
        out[f"{inv}순매수"] = sr.reindex(out.index)

    out["종가"] = pd.to_numeric(px_e[_pick(px_e.columns, "종가")], errors="coerce").reindex(out.index)
    p0 = pd.to_numeric(px_s[_pick(px_s.columns, "종가")], errors="coerce").reindex(out.index)
    out["기간수익률"] = ((out["종가"] / p0 - 1) * 100).round(2)

    if fund is not None:
        for k in ("PER", "PBR", "DIV"):
            col = _pick(fund.columns, k)
            if col is not None:
                out[k] = pd.to_numeric(fund[col], errors="coerce").reindex(out.index)

    names = meta.pop("_names", None)
    nm_col = _pick(px_e.columns, "종목명") or _pick(cap.columns, "종목명")
    if nm_col:
        src = px_e if nm_col in px_e.columns else cap
        out["종목명"] = src[nm_col].reindex(out.index)
    elif names is not None:
        out["종목명"] = names.reindex(out.index)
    else:
        out["종목명"] = out.index

    out["종목명"] = out["종목명"].fillna(pd.Series(out.index, index=out.index))
    out = out.dropna(subset=["시가총액", "종가"])
    meta["종목수"] = len(out)
    meta["투자자"] = list(flows)
    log("스크리너", "결합 완료", 종목수=len(out))
    return out, meta


def screen(raw: pd.DataFrame, min_cap: float = 3000, min_value: float = 10,
           require_foreign: bool = True, exclude_pref: bool = True) -> pd.DataFrame:
    df = raw.copy()
    n0 = len(df)
    df = df[df["시가총액"] >= min_cap]
    if "거래대금" in df.columns:
        df = df[df["거래대금"].fillna(0) >= min_value]
    if exclude_pref:
        nm = df["종목명"].astype(str)
        df = df[~nm.str.endswith("우")]
        for kw in EXCLUDE_KEYWORDS:
            df = df[~nm.str.contains(kw, na=False)]
    log("스크리너", "유동성 필터", 전=n0, 후=len(df))
    if df.empty:
        return df

    f = df["외국인순매수"].fillna(0)
    i = df.get("기관합계순매수", pd.Series(0.0, index=df.index)).fillna(0)
    c = df.get("기타법인순매수", pd.Series(0.0, index=df.index)).fillna(0)

    df["수급강도"] = ((f + i) / df["시가총액"] * 100).round(3)
    str_thr = float(df["수급강도"].quantile(0.80))
    ret_thr = float(df["기간수익률"].median())

    conds = pd.DataFrame(index=df.index)
    conds["c1"] = f > 0
    conds["c2"] = i > 0
    conds["c3"] = df["수급강도"] >= str_thr
    conds["c4"] = df["기간수익률"] <= ret_thr
    conds["c5"] = c > 0

    df["후보점수"] = sum(conds[f"c{k+1}"].astype(int) * w
                     for k, (_, w, _) in enumerate(SCREEN_RULES))
    df["충족조건"] = conds.apply(
        lambda r: ", ".join(SCREEN_RULES[k][0] for k in range(5) if r[f"c{k+1}"]), axis=1)

    def phase(r):
        if r["수급강도"] > 0 and r["기간수익률"] <= 0:
            return "매집 후보"
        if r["수급강도"] > 0:
            return "동행 상승"
        if r["기간수익률"] > 0:
            return "수급 없는 상승"
        return "수급·가격 동반 약세"

    df["국면"] = df.apply(phase, axis=1)
    if require_foreign:
        df = df[conds["c1"]]
    df.attrs["강도상위20%"] = round(str_thr, 3)
    df.attrs["수익률중앙"] = round(ret_thr, 2)
    return df.sort_values(["후보점수", "수급강도"], ascending=False)


EVIDENCE_COLS = ["종목명", "후보점수", "국면", "수급강도", "외국인순매수", "기관합계순매수",
                 "기타법인순매수", "연기금순매수", "기간수익률", "시가총액", "거래대금",
                 "PER", "PBR", "DIV", "충족조건"]

PERSPECTIVES = {
    "수급 분석가": ("당신은 수급 데이터만으로 판단하는 애널리스트입니다. 제시된 종목에서 "
               "매수 논거가 성립하는 근거를 데이터에서 찾아 제시하십시오. "
               "근거가 약한 종목은 약하다고 명시하십시오."),
    "회의론자": ("당신은 위 매수 논거를 무너뜨리는 역할입니다. 수급 데이터만으로 판단할 때 "
             "생기는 함정, 반대 해석 가능성, 데이터에 없는 위험 요인을 지적하십시오. "
             "'조심하세요' 같은 일반론은 금지하고 이 종목·이 수치에 붙는 반박만 쓰십시오."),
    "리스크 매니저": ("당신은 손실 관리 담당입니다. 각 종목에 대해 (1) 논거가 틀렸다고 판정할 "
                "관측 조건, (2) 최대 손실 시나리오, (3) 포지션 크기를 제한해야 하는 근거를 "
                "제시하십시오. 수익 전망은 절대 언급하지 마십시오."),
}


def build_review_prompt(cand: pd.DataFrame, sig: dict, score: int, days: int,
                        role: str, prior: str = "") -> str:
    keep = [c for c in ["종목명", "후보점수", "국면", "수급강도", "외국인순매수",
                        "기관합계순매수", "기타법인순매수", "기간수익률", "시가총액",
                        "PER", "PBR"] if c in cand.columns]
    prior_block = f"\n[앞선 관점의 주장 — 반박 또는 보완 대상]\n{prior}\n" if prior else ""
    return f"""[역할]
{PERSPECTIVES[role]}

[시장 전체 국면]
- 코스피 수급 스코어 {score}/100 · 다이버전스 판정 {sig['divergence'][0]}
- {sig['short']}일 누적: 외국인 {sig['rolling']['외국인']:,.0f}억 / 기관 {sig['rolling']['기관']:,.0f}억 / 기타법인 {sig['rolling']['기타법인']:,.0f}억

[스크리너 통과 종목 — 최근 {days}일 집계, 순매수 단위 억원]
{cand[keep].to_string()}

[컬럼 정의]
- 수급강도 = (외국인+기관 순매수) / 시가총액 × 100. 시총 대비 비율이라 종목 규모가 중립화된 값
- 국면 '매집 후보' = 수급은 유입인데 기간 수익률이 0 이하
- 후보점수 = 5개 조건의 가중합. 예측력이 검증되지 않은 규칙값
{prior_block}
[작성 규칙]
- 위 표의 수치만 인용. 실적·수주·뉴스·목표주가를 지어내지 마십시오.
- 종목별 2~3문장, 전체 6문단 이내.
- 이 데이터로 판단 불가한 항목은 "이 데이터로는 알 수 없음"이라고 명시.
- 목표주가·기대수익률 제시 금지.
- 한국어로 작성."""


def run_perspective(api_key: str, model: str, prompt: str, max_tokens: int = 1200) -> str:
    from groq import Groq
    r = Groq(api_key=api_key).chat.completions.create(
        model=model,
        messages=[{"role": "system", "content":
                   "당신은 한국 주식시장 수급 데이터를 다루는 분석가입니다. 주어진 수치만 "
                   "근거로 삼고, 모르는 것은 모른다고 말하며, 확신의 정도를 구분해 표현합니다."},
                  {"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=max_tokens,
    )
    return (r.choices[0].message.content or "").strip()


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
# 종목 스크리너
# ──────────────────────────────────────────────────────────────────────────────

SCREEN_INVESTORS = ["기타법인", "외국인", "연기금", "기관합계"]


def _latest_bday() -> str:
    stock = get_stock_api()
    try:
        with capture_stdout("스크리너"):
            return stock.get_nearest_business_day_in_a_week()
    except Exception:  # noqa: BLE001
        return now_kst().strftime("%Y%m%d")


@st.cache_data(ttl=1800, show_spinner=False)
def load_universe(short_days: int, long_days: int, auth_key: str) -> pd.DataFrame:
    """
    시장 전체 스냅샷을 8회 호출로 구성합니다.
    종목별 반복 조회 대신 전종목 API 를 쓰기 때문에 KRX 부하와 소요 시간이 작습니다.
    """
    stock = get_stock_api()
    end = _latest_bday()
    s_start = (pd.Timestamp(end) - pd.Timedelta(days=int(short_days * 1.6) + 6)).strftime("%Y%m%d")
    l_start = (pd.Timestamp(end) - pd.Timedelta(days=int(long_days * 1.6) + 10)).strftime("%Y%m%d")
    log("스크리너", "유니버스 수집 시작", 기준일=end, 단기=s_start, 장기=l_start)

    def call(fn, *a, **k):
        with capture_stdout("스크리너"):
            return _retry(lambda: fn(*a, **k), "스크리너", tries=2)

    cap = call(stock.get_market_cap, end, market="KOSPI")
    fund = call(stock.get_market_fundamental, end, market="KOSPI")
    chg_s = call(stock.get_market_price_change, s_start, end, "KOSPI")
    chg_l = call(stock.get_market_price_change, l_start, end, "KOSPI")
    log("스크리너", "시세·재무 수집 완료", 종목수=len(cap))

    uni = pd.DataFrame(index=cap.index)
    name_col = _pick(chg_s.columns, "종목명")
    uni["종목명"] = chg_s[name_col] if name_col is not None else uni.index
    uni["종가"] = pd.to_numeric(cap[_pick(cap.columns, "종가")], errors="coerce")
    uni["시가총액"] = pd.to_numeric(cap[_pick(cap.columns, "시가총액")], errors="coerce")
    uni["거래대금"] = pd.to_numeric(cap[_pick(cap.columns, "거래대금")], errors="coerce")
    for src, want in [(fund, "PER"), (fund, "PBR"), (fund, "DIV")]:
        col = _pick(src.columns, want)
        uni[want] = pd.to_numeric(src[col], errors="coerce") if col is not None else float("nan")
    uni["단기등락률"] = pd.to_numeric(chg_s[_pick(chg_s.columns, "등락률")], errors="coerce")
    uni["장기등락률"] = pd.to_numeric(chg_l[_pick(chg_l.columns, "등락률")], errors="coerce")

    for inv in SCREEN_INVESTORS:
        try:
            npdf = call(stock.get_market_net_purchases_of_equities, s_start, end, "KOSPI", inv)
            vcol = _pick(npdf.columns, "순매수거래대금")
            uni[f"순매수_{inv}"] = (
                pd.to_numeric(npdf[vcol], errors="coerce").reindex(uni.index).fillna(0) / 1e8
            )
            log("스크리너", f"{inv} 순매수 수집", 종목수=int((uni[f'순매수_{inv}'] != 0).sum()))
        except Exception as e:  # noqa: BLE001
            log_exc("스크리너", e, f"{inv} 순매수 수집 실패")
            uni[f"순매수_{inv}"] = 0.0

    uni["시가총액"] = uni["시가총액"] / 1e8      # 억원
    uni["거래대금"] = uni["거래대금"] / 1e8      # 억원
    uni["기준일"] = end
    log("스크리너", "유니버스 완성", 종목수=len(uni))
    return uni


def screen_stocks(uni: pd.DataFrame, mode: str, min_cap: int, min_value: int,
                  exclude_pref: bool = True) -> pd.DataFrame:
    """
    순매수는 절대 금액이 아니라 시가총액 대비 비율로 평가합니다.
    금액만 보면 대형주만 상위에 남아 정보가 없습니다.
    """
    d = uni.copy()
    if exclude_pref:
        d = d[[str(t).endswith("0") for t in d.index]]          # 우선주 제외
    d = d[(d["시가총액"] >= min_cap) & (d["거래대금"] >= min_value)]
    d = d.dropna(subset=["종가", "시가총액"])
    if d.empty:
        return d

    for inv in SCREEN_INVESTORS:
        d[f"강도_{inv}"] = d[f"순매수_{inv}"] / d["시가총액"] * 100   # 시총 대비 %

    d["수급강도"] = d["강도_기타법인"] + d["강도_외국인"] + d["강도_연기금"]
    d["매수주체수"] = sum((d[f"순매수_{i}"] > 0).astype(int)
                       for i in ["기타법인", "외국인", "연기금", "기관합계"])

    pct = lambda s: s.rank(pct=True) * 100  # noqa: E731

    score = pct(d["수급강도"]) * 0.35
    score += (d["매수주체수"] / 4 * 100) * 0.15
    pbr_rank = (1 - d["PBR"].rank(pct=True)) * 100
    score += pbr_rank.fillna(50) * 0.15

    if mode == "낙폭과대 반등":
        score += (1 - d["장기등락률"].rank(pct=True)) * 100 * 0.20   # 많이 빠진 종목
        score += pct(d["단기등락률"] - d["장기등락률"]) * 0.15        # 최근 개선
    else:  # 추세 지속
        score += pct(d["장기등락률"]) * 0.20
        score += pct(d["단기등락률"]) * 0.15

    d["기회점수"] = score.round(1)
    return d.sort_values("기회점수", ascending=False)


def build_scenario(row: pd.Series, mode: str, short_days: int, long_days: int) -> dict:
    """규칙에서 직접 유도되는 포착 근거와 시나리오. LLM 이 아니라 산술 결과입니다."""
    reasons, checks = [], []

    for inv, label in [("기타법인", "기타법인"), ("외국인", "외국인"), ("연기금", "연기금")]:
        v, s = row[f"순매수_{inv}"], row[f"강도_{inv}"]
        if v > 0:
            reasons.append(f"{label} 순매수 {v:,.0f}억 (시총 대비 {s:.2f}%)")

    if row["매수주체수"] >= 2:
        reasons.append(f"매수 주체 {int(row['매수주체수'])}곳 동시 유입")
    if pd.notna(row["PBR"]) and row["PBR"] < 1.0:
        reasons.append(f"PBR {row['PBR']:.2f} — 청산가치 이하")
    if mode == "낙폭과대 반등":
        reasons.append(f"{long_days}일 {row['장기등락률']:+.1f}% / {short_days}일 {row['단기등락률']:+.1f}%")
    else:
        reasons.append(f"{long_days}일 {row['장기등락률']:+.1f}% 추세 유지")

    price = row["종가"]
    if mode == "낙폭과대 반등":
        bull = (f"기관·외국인 매도가 잦아든 상태에서 {'기타법인' if row['순매수_기타법인'] > 0 else '외국인'} "
                f"매수가 이어지면 낙폭 되돌림. 1차 목표는 {long_days}일 하락분의 3분의 1 회복 지점.")
        entry = f"현재가 {price:,.0f}원 부근 분할 진입, 추가 하락 시 -7% 지점에서 2차."
        invalid = (f"매수 주체가 순매도로 돌아서거나 종가가 최근 저점을 이탈하면 무효. "
                   f"손절 기준 -10% 또는 직전 저점 하회.")
    else:
        bull = "수급과 가격이 같은 방향이라 추세 지속 가능성. 눌림목에서 비중 확대."
        entry = f"현재가 {price:,.0f}원. 20일선 눌림 시 진입, 추격 매수 자제."
        invalid = "외국인 순매수가 3일 연속 순매도로 전환되거나 20일선 이탈 시 무효."

    if row["순매수_기타법인"] > 0:
        checks.append("DART 에서 자기주식취득 공시 확인 — 없으면 계열사 지분 매입일 수 있음")
    if pd.isna(row["PER"]) or row["PER"] <= 0:
        checks.append("PER 산출 불가 — 적자 기업 여부 확인 필요")
    checks.append("최근 공시·실적·유상증자 여부 확인")
    checks.append(f"거래대금 {row['거래대금']:,.0f}억 — 체결 슬리피지 감안")

    return {"reasons": reasons, "bull": bull, "entry": entry,
            "invalid": invalid, "checks": checks}


# ──────────────────────────────────────────────────────────────────────────────
# 페르소나 회의
# ──────────────────────────────────────────────────────────────────────────────

PERSONAS = [
    {"name": "수급 분석가", "icon": "📊",
     "system": "당신은 기관·외국인 자금 흐름만으로 시장을 읽는 수급 분석가입니다. "
               "가격이 아니라 '누가 사고 누가 파는가'와 그 자금의 성격(장기/단기)을 봅니다. "
               "제공된 수치만 근거로 삼고, 자금 성격을 구분해 서술하십시오.",
     "task": "수급 구조를 진단하고, 제시된 종목들의 매수 주체가 어떤 성격인지 구분하십시오. "
             "특히 기타법인 자금이 자사주 매입일 가능성과 그 한계를 짚으십시오."},
    {"name": "가치 투자자", "icon": "🏛",
     "system": "당신은 밸류에이션과 재무 안정성을 우선하는 가치 투자자입니다. "
               "저PBR 이 곧 저평가가 아니라는 것을 알고 있으며, 싼 데는 이유가 있다고 의심합니다.",
     "task": "제시된 종목의 PBR·PER 이 진짜 저평가인지 밸류 트랩인지 판단 기준을 제시하십시오. "
             "재무 데이터가 부족하다면 무엇을 더 봐야 하는지 명시하십시오."},
    {"name": "모멘텀 트레이더", "icon": "📈",
     "system": "당신은 가격 추세와 거래량으로만 판단하는 단기 트레이더입니다. "
               "떨어지는 칼날을 잡지 않으며, 진입 시점과 손절 위치를 항상 구체적으로 말합니다.",
     "task": "낙폭과대 종목의 진입 타이밍 판단 기준을 제시하십시오. "
             "지금 사야 할 이유가 없다면 없다고 말하고, 무엇을 기다려야 하는지 쓰십시오."},
    {"name": "리스크 매니저", "icon": "🛡",
     "system": "당신은 손실 관리를 책임지는 리스크 매니저입니다. 수익 가능성보다 "
               "무엇이 이 거래를 죽이는지에 집중합니다. 포지션 크기와 손절을 반드시 언급합니다.",
     "task": "이 아이디어가 실패하는 경로를 구체적으로 나열하고, 현금 비중과 종목당 배분 한도를 "
             "제시하십시오. 유동성·집중도 위험을 반드시 다루십시오."},
    {"name": "레드팀", "icon": "🔍",
     "system": "당신은 스크리너 자체를 의심하는 검증자입니다. 규칙 기반 선별이 만들어내는 "
               "편향과 착시를 찾아냅니다. 동의하는 것이 당신의 역할이 아닙니다.",
     "task": "이 스크리닝 방식의 구조적 결함을 지적하십시오. 선택 편향, 데이터 한계, "
             "규칙이 놓치는 정보가 무엇인지 구체적으로 쓰십시오. 최소 3가지."},
]


def persona_context(df: pd.DataFrame, sig: dict, guide: dict, cash_ratio: int,
                    picks: Optional[pd.DataFrame], mode: str) -> str:
    div_name, div_desc, _ = sig["divergence"]
    ctx = f"""[시장 전체 수급 — 코스피]
- 최근 영업일: {df.index[-1]:%Y-%m-%d}, 종가 {df['종가'].iloc[-1]:,.2f}
- {sig['short']}일 누적 순매수(억): 외국인 {sig['rolling']['외국인']:,.0f} / 기관 {sig['rolling']['기관']:,.0f} / 기타법인 {sig['rolling']['기타법인']:,.0f} / 개인 {sig['rolling']['개인']:,.0f}
- 외국인 z-score {sig['zscore']:+.2f}, 연속 순매수 {sig['streak_foreign']}일
- 다이버전스 판정: {div_name} ({div_desc})
- 규칙 스코어 {sig['score']}/100 ({guide['band']}) · 현금 비중 {cash_ratio}%
"""
    if picks is not None and not picks.empty:
        rows = []
        for t, r in picks.iterrows():
            rows.append(
                f"- {r['종목명']}({t}): 기회점수 {r['기회점수']:.0f}, 시총 {r['시가총액']:,.0f}억, "
                f"PBR {r['PBR'] if pd.notna(r['PBR']) else float('nan'):.2f}, "
                f"기간등락 {r['단기등락률']:+.1f}%/{r['장기등락률']:+.1f}%, "
                f"순매수(억) 기타법인 {r['순매수_기타법인']:,.0f} 외국인 {r['순매수_외국인']:,.0f} "
                f"연기금 {r['순매수_연기금']:,.0f}, 거래대금 {r['거래대금']:,.0f}억")
        ctx += f"\n[스크리너 선별 종목 — 전략: {mode}]\n" + "\n".join(rows) + "\n"
    ctx += ("\n[제약] 위 수치 외의 뉴스·실적·목표주가를 지어내지 마십시오. "
            "모르는 것은 모른다고 쓰고, 무엇을 확인해야 하는지로 대신하십시오. "
            "한국어, 마크다운, 250자 내외.\n")
    return ctx


def run_persona(api_key: str, model: str, persona: dict, context: str):
    from groq import Groq
    log("페르소나", "호출", 이름=persona["name"], 모델=model)
    stream = Groq(api_key=api_key).chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": persona["system"]},
                  {"role": "user", "content": context + "\n[당신의 과제]\n" + persona["task"]}],
        temperature=0.3, max_tokens=700, stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


def run_synthesis(api_key: str, model: str, context: str, opinions: dict):
    from groq import Groq
    joined = "\n\n".join(f"### {k}\n{v}" for k, v in opinions.items())
    prompt = (context + "\n[다섯 전문가의 의견]\n" + joined +
              "\n\n[당신의 과제]\n위 의견들이 어디서 갈리는지 먼저 짚고, 합의된 부분과 "
              "충돌하는 부분을 구분하십시오. 그다음 실행 가능한 결론을 쓰십시오: "
              "① 지금 행동할 것인가 기다릴 것인가 ② 기다린다면 무엇을 보고 움직일 것인가 "
              "③ 행동한다면 최대 배분 비율. 낙관도 비관도 아닌 조건부 서술로. 400자 내외.")
    stream = Groq(api_key=api_key).chat.completions.create(
        model=model,
        messages=[{"role": "system", "content":
                   "당신은 다섯 전문가의 의견을 조율하는 최종 의사결정자입니다. "
                   "합의를 억지로 만들지 않고, 의견이 갈리는 지점을 그대로 드러냅니다. "
                   "결론은 항상 조건부로 서술합니다."},
                  {"role": "user", "content": prompt}],
        temperature=0.25, max_tokens=900, stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


# ──────────────────────────────────────────────────────────────────────────────
# 리스크 엔진 — 실전 매매의 실제 핵심
# ──────────────────────────────────────────────────────────────────────────────

# 시장 스코어 → 신규 진입 허용 배율. 종목이 아무리 좋아 보여도 국면이 나쁘면 줄입니다.
MARKET_GATE = [(85, 1.00, "정상 배분"), (65, 0.75, "배분 25% 축소"),
               (45, 0.50, "절반 배분"), (25, 0.25, "테스트 물량만"),
               (0, 0.00, "신규 진입 중단")]


def market_gate(score: int) -> tuple[float, str]:
    for lo, mult, label in MARKET_GATE:
        if score >= lo:
            return mult, label
    return 0.0, "신규 진입 중단"


def size_position(price: float, capital: float, risk_pct: float, stop_pct: float,
                  max_weight_pct: float, adv_eok: float, adv_cap_pct: float,
                  gate: float) -> dict:
    """
    포지션 크기를 세 가지 제약 중 가장 작은 값으로 정합니다.
      1) 리스크 한도  — 손절까지 갔을 때 잃는 금액이 계좌의 risk_pct 를 넘지 않을 것
      2) 비중 한도    — 한 종목이 계좌의 max_weight_pct 를 넘지 않을 것
      3) 유동성 한도  — 주문금액이 일평균 거래대금의 adv_cap_pct 를 넘지 않을 것
    셋 중 무엇이 걸렸는지도 함께 돌려줍니다.
    """
    if price <= 0 or stop_pct <= 0:
        return {"수량": 0, "금액": 0.0, "제약": "가격/손절 입력 오류"}

    risk_krw = capital * risk_pct / 100
    by_risk = risk_krw / (stop_pct / 100)
    by_weight = capital * max_weight_pct / 100
    by_liquidity = adv_eok * 1e8 * adv_cap_pct / 100

    limits = {"리스크 한도": by_risk, "비중 한도": by_weight, "유동성 한도": by_liquidity}
    binding = min(limits, key=limits.get)
    value = min(limits.values()) * gate

    shares = int(value // price)
    actual = shares * price
    return {
        "수량": shares,
        "금액": actual,
        "실제리스크": actual * stop_pct / 100,
        "계좌대비": actual / capital * 100 if capital else 0.0,
        "제약": binding if gate > 0 else "국면 게이트 (신규 진입 중단)",
        "게이트배율": gate,
        "한도": limits,
    }


def trade_costs(value: float, fee_pct: float, tax_pct: float) -> dict:
    """왕복 비용. 매수 수수료 + 매도 수수료 + 매도 시 거래세."""
    buy_fee = value * fee_pct / 100
    sell_fee = value * fee_pct / 100
    tax = value * tax_pct / 100
    total = buy_fee + sell_fee + tax
    return {"매수수수료": buy_fee, "매도수수료": sell_fee, "거래세": tax, "합계": total,
            "손익분기(%)": (total / value * 100) if value else 0.0}


def build_trade_plan(picks: pd.DataFrame, capital: float, risk_pct: float, stop_pct: float,
                     max_weight_pct: float, adv_cap_pct: float, fee_pct: float,
                     tax_pct: float, gate: float, target_r: float) -> pd.DataFrame:
    rows = []
    for ticker, r in picks.iterrows():
        sz = size_position(float(r["종가"]), capital, risk_pct, stop_pct,
                           max_weight_pct, float(r["거래대금"]), adv_cap_pct, gate)
        if sz["수량"] <= 0:
            continue
        cost = trade_costs(sz["금액"], fee_pct, tax_pct)
        stop_price = r["종가"] * (1 - stop_pct / 100)
        target_price = r["종가"] * (1 + stop_pct * target_r / 100)
        rows.append({
            "티커": ticker, "종목명": r["종목명"], "점수": r["기회점수"],
            "진입가": float(r["종가"]), "손절가": round(stop_price, 0),
            "목표가": round(target_price, 0), "수량": sz["수량"], "금액": sz["금액"],
            "계좌대비(%)": sz["계좌대비"], "리스크(원)": sz["실제리스크"],
            "왕복비용(원)": cost["합계"], "손익분기(%)": cost["손익분기(%)"],
            "제약": sz["제약"], "거래대금(억)": float(r["거래대금"]),
        })
    return pd.DataFrame(rows).set_index("티커") if rows else pd.DataFrame()


# ──────────────────────────────────────────────────────────────────────────────
# 스크리너 백테스트 — 종목 선별 규칙이 실제로 통했는지
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=7200, show_spinner=False)
def screener_backtest(rebalances: int, hold_days: int, mode: str, top_n: int,
                      min_cap: int, min_val: int, sc_days: int,
                      fee_pct: float, tax_pct: float, auth_key: str) -> dict:
    """
    과거 리밸런스 시점마다 유니버스를 재구성해 상위 N 종목을 뽑고,
    보유 기간 후 수익률을 코스피 대비로 측정합니다. 왕복 비용을 차감합니다.

    리밸런스 1회당 KRX 호출 약 8회. 시간이 걸리므로 2시간 캐시합니다.
    """
    stock = get_stock_api()
    end = _latest_bday()
    span = (rebalances + 2) * hold_days + sc_days + 80
    idx_start = (pd.Timestamp(end) - pd.Timedelta(days=int(span * 1.6))).strftime("%Y%m%d")

    with capture_stdout("스크리너BT"):
        kospi = _retry(
            lambda: stock.get_index_ohlcv(idx_start, end, KOSPI_INDEX_TICKER, name_display=False),
            "스크리너BT", tries=2)
    if kospi is None or kospi.empty:
        raise RuntimeError("백테스트용 코스피 지수를 가져오지 못했습니다")
    cal = pd.to_datetime(kospi.index)
    close = pd.to_numeric(kospi[_pick(kospi.columns, "종가")], errors="coerce")
    close.index = cal

    # 마지막 관측이 hold_days 뒤에 존재해야 하므로 뒤에서부터 배치
    idxs = [len(cal) - 1 - hold_days - i * hold_days for i in range(rebalances)]
    idxs = [i for i in idxs if i - sc_days - 60 > 0][::-1]
    if not idxs:
        raise RuntimeError("표본을 만들 만큼 과거 데이터가 없습니다. 리밸런스 수나 보유일을 줄이세요")

    def call(fn, *a, **k):
        with capture_stdout("스크리너BT"):
            return _retry(lambda: fn(*a, **k), "스크리너BT", tries=2)

    recs, per_period = [], []
    log("스크리너BT", "백테스트 시작", 리밸런스=len(idxs), 보유일=hold_days, 전략=mode)

    for n, i in enumerate(idxs):
        d0 = cal[i]
        d1 = cal[min(i + hold_days, len(cal) - 1)]
        ds, de = d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")
        s_start = cal[max(i - sc_days, 0)].strftime("%Y%m%d")
        l_start = cal[max(i - 60, 0)].strftime("%Y%m%d")
        try:
            cap = call(stock.get_market_cap, ds, market="KOSPI")
            fund = call(stock.get_market_fundamental, ds, market="KOSPI")
            chg_s = call(stock.get_market_price_change, s_start, ds, "KOSPI")
            chg_l = call(stock.get_market_price_change, l_start, ds, "KOSPI")
            fwd = call(stock.get_market_price_change, ds, de, "KOSPI")

            u = pd.DataFrame(index=cap.index)
            nm = _pick(chg_s.columns, "종목명")
            u["종목명"] = chg_s[nm] if nm is not None else u.index
            u["종가"] = pd.to_numeric(cap[_pick(cap.columns, "종가")], errors="coerce")
            u["시가총액"] = pd.to_numeric(cap[_pick(cap.columns, "시가총액")], errors="coerce") / 1e8
            u["거래대금"] = pd.to_numeric(cap[_pick(cap.columns, "거래대금")], errors="coerce") / 1e8
            for want in ("PER", "PBR", "DIV"):
                c_ = _pick(fund.columns, want)
                u[want] = pd.to_numeric(fund[c_], errors="coerce") if c_ is not None else float("nan")
            u["단기등락률"] = pd.to_numeric(chg_s[_pick(chg_s.columns, "등락률")], errors="coerce")
            u["장기등락률"] = pd.to_numeric(chg_l[_pick(chg_l.columns, "등락률")], errors="coerce")
            for inv in ["기타법인", "외국인", "연기금", "기관합계"]:
                try:
                    npdf = call(stock.get_market_net_purchases_of_equities,
                                s_start, ds, "KOSPI", inv)
                    vc = _pick(npdf.columns, "순매수거래대금")
                    u[f"순매수_{inv}"] = (pd.to_numeric(npdf[vc], errors="coerce")
                                        .reindex(u.index).fillna(0) / 1e8)
                except Exception:  # noqa: BLE001
                    u[f"순매수_{inv}"] = 0.0

            ranked = screen_stocks(u, mode, min_cap, min_val)
            if ranked.empty:
                continue
            picks = ranked.head(top_n)
            fret = pd.to_numeric(fwd[_pick(fwd.columns, "등락률")], errors="coerce")
            r = fret.reindex(picks.index).dropna()
            if r.empty:
                continue

            cost = fee_pct * 2 + tax_pct
            net = r.mean() - cost
            bench = (close.iloc[min(i + hold_days, len(close) - 1)] / close.iloc[i] - 1) * 100
            per_period.append({
                "진입일": f"{d0:%Y-%m-%d}", "청산일": f"{d1:%Y-%m-%d}", "종목수": len(r),
                "평균(%)": round(r.mean(), 2), "비용차감(%)": round(net, 2),
                "코스피(%)": round(bench, 2), "초과(%)": round(net - bench, 2),
                "승률(%)": round((r > cost).mean() * 100, 1),
            })
            for t_, v_ in r.items():
                recs.append({"기간": f"{d0:%Y-%m-%d}", "티커": t_, "수익률": v_})
            log("스크리너BT", f"{n+1}/{len(idxs)} 완료", 진입일=f"{d0:%Y-%m-%d}",
                평균=f"{r.mean():.2f}%", 코스피=f"{bench:.2f}%")
        except Exception as e:  # noqa: BLE001
            log_exc("스크리너BT", e, f"{ds} 구간 실패")
            continue

    if not per_period:
        raise RuntimeError("유효한 구간이 없습니다")

    pp = pd.DataFrame(per_period)
    summary = {
        "구간수": len(pp),
        "평균 초과수익(%)": round(pp["초과(%)"].mean(), 2),
        "중앙값 초과수익(%)": round(pp["초과(%)"].median(), 2),
        "코스피 이긴 구간(%)": round((pp["초과(%)"] > 0).mean() * 100, 1),
        "평균 수익(비용차감,%)": round(pp["비용차감(%)"].mean(), 2),
        "코스피 평균(%)": round(pp["코스피(%)"].mean(), 2),
        "최악 구간(%)": round(pp["비용차감(%)"].min(), 2),
        "최고 구간(%)": round(pp["비용차감(%)"].max(), 2),
        "구간 표준편차(%)": round(pp["비용차감(%)"].std(), 2),
    }
    log("스크리너BT", "백테스트 완료", **{k: v for k, v in list(summary.items())[:4]})
    return {"per_period": pp, "summary": summary, "records": pd.DataFrame(recs),
            "mode": mode, "hold": hold_days, "top_n": top_n}


# ──────────────────────────────────────────────────────────────────────────────
# 실전 매매 페르소나 (기본 5 + 실행 5)
# ──────────────────────────────────────────────────────────────────────────────

EXEC_PERSONAS = [
    {"name": "국면 판정관", "icon": "🧭",
     "system": "당신은 시장 국면만 판정하는 전략가입니다. 개별 종목이 아니라 '지금 위험을 "
               "늘릴 때인가 줄일 때인가'만 답합니다. 애매하면 애매하다고 말합니다.",
     "task": "현재 수급·추세 데이터로 국면을 판정하고, 신규 진입 배율을 몇으로 두어야 하는지 "
             "근거와 함께 제시하십시오. 국면이 바뀌었다고 판단할 관측 조건도 쓰십시오."},
    {"name": "포트폴리오 매니저", "icon": "🧮",
     "system": "당신은 배분과 상관을 관리하는 포트폴리오 매니저입니다. 개별 종목의 매력보다 "
               "포트폴리오 전체의 위험 집중을 봅니다. 같은 섹터 중복을 특히 경계합니다.",
     "task": "제시된 종목들의 섹터·성격이 겹치는지 점검하고, 몇 종목까지 담아야 하는지, "
             "종목당 상한을 얼마로 둘지 제시하십시오. 분산이 안 되는 조합이면 지적하십시오."},
    {"name": "집행 트레이더", "icon": "⚡",
     "system": "당신은 실제 주문을 넣는 집행 담당입니다. 좋은 아이디어도 체결이 나쁘면 "
               "손실이라는 것을 압니다. 거래대금 대비 주문 크기와 분할 방식을 항상 따집니다.",
     "task": "제시된 종목의 거래대금 대비 주문 규모가 적절한지 판단하고, 분할 매수 방식과 "
             "체결 시 주의점을 구체적으로 쓰십시오. 유동성이 위험한 종목은 이름을 지목하십시오."},
    {"name": "비용 분석가", "icon": "🧾",
     "system": "당신은 수수료·세금·슬리피지를 계산하는 비용 분석가입니다. 매매 빈도가 "
               "수익을 어떻게 갉아먹는지 숫자로 보여줍니다.",
     "task": "제시된 손익분기 비용을 근거로, 이 전략의 기대 수익이 비용을 감당할 수준인지 "
             "판단하십시오. 회전율을 낮춰야 한다면 얼마나 낮춰야 하는지 쓰십시오."},
    {"name": "사전 부검관", "icon": "⚰️",
     "system": "당신은 사전 부검(pre-mortem)을 담당합니다. 이 계획이 6개월 뒤 실패했다고 "
               "'이미 확정된 사실'로 가정하고, 그 원인을 과거형으로 서술합니다. "
               "가능성을 논하지 말고 실패했다고 전제하십시오.",
     "task": "6개월 뒤 이 매매 계획이 실패했습니다. 무엇 때문이었는지 가장 그럴듯한 원인 "
             "3가지를 과거형으로 서술하고, 각각을 지금 막을 수 있는 조치를 하나씩 쓰십시오."},
]




def exec_context(plan: pd.DataFrame, capital: float, gate: float, gate_label: str,
                 risk_pct: float, stop_pct: float, fee_pct: float, tax_pct: float) -> str:
    if plan is None or plan.empty:
        return "\n[매매 계획] 아직 생성된 계획이 없습니다.\n"
    total_val = plan["금액"].sum()
    total_risk = plan["리스크(원)"].sum()
    lines = "\n".join(
        f"- {r['종목명']}({t}): {r['수량']:,}주 × {r['진입가']:,.0f}원 = {r['금액']/1e4:,.0f}만원 "
        f"(계좌 {r['계좌대비(%)']:.1f}%), 손절 {r['손절가']:,.0f} / 목표 {r['목표가']:,.0f}, "
        f"제약 {r['제약']}, 거래대금 {r['거래대금(억)']:,.0f}억"
        for t, r in plan.iterrows())
    return f"""
[매매 계획]
- 계좌 {capital/1e4:,.0f}만원 · 국면 배율 {gate:.2f} ({gate_label})
- 종목당 리스크 {risk_pct}% · 손절폭 {stop_pct}% · 왕복비용 {fee_pct*2 + tax_pct:.3f}%
- 총 투입 {total_val/1e4:,.0f}만원 (계좌의 {total_val/capital*100:.1f}%)
- 총 리스크(전 종목 손절 시) {total_risk/1e4:,.0f}만원 (계좌의 {total_risk/capital*100:.2f}%)
{lines}
"""


# ──────────────────────────────────────────────────────────────────────────────
# 기술적 지표
# ──────────────────────────────────────────────────────────────────────────────

MA_SET = (5, 20, 60, 120)
TECH_MODES = ["역배열→정배열 전환", "MACD 골든크로스", "RSI 과매도 반등"]
ALL_MODES = ["낙폭과대 반등", "추세 지속"] + TECH_MODES


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 방식 RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1 / period, adjust=False).mean()
    al = loss.ewm(alpha=1 / period, adjust=False).mean()
    out = 100 - 100 / (1 + ag.div(al))
    # 0 나눗셈 경계 처리: 손실 없음→100, 이익 없음→0, 둘 다 없음(보합)→50
    out = out.where(al != 0, 100.0)
    out = out.where(ag != 0, 0.0)
    out = out.where(~((ag == 0) & (al == 0)), 50.0)
    return out.astype(float)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def compute_tech(close: pd.Series, volume: Optional[pd.Series] = None) -> dict:
    """
    한 종목의 종가 시계열에서 배열 상태와 지표를 계산합니다.
    이동평균·MACD 는 모두 후행 지표입니다. 전환을 '예측'하는 것이 아니라
    '이미 일어난 전환을 확인'하는 용도로 씁니다.
    """
    if len(close) < 130:
        return {}
    ma = {n: close.rolling(n).mean() for n in MA_SET}
    m5, m20, m60, m120 = (ma[n] for n in MA_SET)

    bull = (m5 > m20) & (m20 > m60) & (m60 > m120)      # 정배열
    bear = (m5 < m20) & (m20 < m60) & (m60 < m120)      # 역배열

    line, signal, hist = macd(close)
    r = rsi(close)
    last = close.index[-1]

    def at(s, i=-1):
        try:
            v = float(s.iloc[i])
            return v if pd.notna(v) else float("nan")
        except Exception:  # noqa: BLE001
            return float("nan")

    # 정배열이 몇 단계까지 회복됐는지 (0~3)
    steps = int(at(m5) > at(m20)) + int(at(m20) > at(m60)) + int(at(m60) > at(m120))
    # 최근 60일 중 역배열이었던 비율 — 진짜 바닥에서 올라오는 것인지 확인
    bear_ratio = float(bear.tail(60).mean())
    # MACD 히스토그램이 음→양으로 바뀐 지 며칠 됐는가
    h = hist.dropna()
    cross_age = float("nan")
    if len(h) > 2:
        pos = h > 0
        if bool(pos.iloc[-1]):
            flip = pos[::-1].idxmin() if (~pos).any() else None
            if flip is not None:
                cross_age = float(len(h.loc[flip:]) - 1)
    # MA20 기울기 (5일 변화율 %)
    slope20 = ((at(m20) / at(m20, -6) - 1) * 100) if len(m20.dropna()) > 6 else float("nan")

    state = "정배열" if bool(bull.iloc[-1]) else "역배열" if bool(bear.iloc[-1]) else "혼조"

    return {
        "종가": at(close), "MA5": at(m5), "MA20": at(m20), "MA60": at(m60), "MA120": at(m120),
        "배열": state, "정배열단계": steps, "역배열비율60": round(bear_ratio, 2),
        "MA20기울기": slope20, "이격도60": (at(close) / at(m60) - 1) * 100 if at(m60) else float("nan"),
        "MACD": at(line), "MACD시그널": at(signal), "MACD히스토": at(hist),
        "MACD전환일": cross_age, "RSI": at(r),
        "RSI최저20": float(r.tail(20).min()) if len(r.dropna()) > 20 else float("nan"),
        "거래량비": (float(volume.tail(5).mean() / volume.tail(60).mean())
                  if volume is not None and len(volume) > 60 and volume.tail(60).mean() else float("nan")),
        "기준일": last,
        "series": {"close": close, "ma": ma, "macd": (line, signal, hist), "rsi": r},
    }


@st.cache_data(ttl=3600, show_spinner=False)
def load_price_matrix(tickers: tuple, days: int, auth_key: str) -> dict:
    """
    후보 종목만 골라 개별 OHLCV 를 조회합니다.
    전종목을 날짜별로 224회 부르는 대신, 1단계에서 걸러낸 소수만 종목별로 부릅니다.
    """
    stock = get_stock_api()
    end = _latest_bday()
    start = (pd.Timestamp(end) - pd.Timedelta(days=int(days * 1.55) + 20)).strftime("%Y%m%d")
    log("기술적", "개별 시세 조회 시작", 종목수=len(tickers), 기간=f"{start}~{end}")

    out, fails = {}, 0
    prog = st.progress(0.0, text="개별 종목 시세를 불러오는 중…")
    for i, t in enumerate(tickers):
        try:
            with capture_stdout("기술적"):
                d = _retry(lambda: stock.get_market_ohlcv(start, end, t), "기술적", tries=2)
            if d is not None and not d.empty:
                d = d.copy()
                d.index = pd.to_datetime(d.index)
                cc = _pick(d.columns, "종가")
                vc = _pick(d.columns, "거래량")
                if cc is not None:
                    out[t] = {
                        "close": pd.to_numeric(d[cc], errors="coerce").dropna(),
                        "volume": pd.to_numeric(d[vc], errors="coerce") if vc else None,
                        "ohlc": d,
                    }
        except Exception:  # noqa: BLE001
            fails += 1
        prog.progress((i + 1) / len(tickers), text=f"{i+1}/{len(tickers)} 종목")
        time.sleep(0.12)          # KRX 부하 완화
    prog.empty()
    log("기술적", "개별 시세 조회 완료", 성공=len(out), 실패=fails)
    return out


def screen_with_tech(base: pd.DataFrame, tech: dict, mode: str) -> pd.DataFrame:
    """1단계 수급 필터를 통과한 종목에 기술적 조건을 더해 재채점합니다."""
    rows = []
    for t, r in base.iterrows():
        m = tech.get(t)
        if not m:
            continue
        rows.append({**r.to_dict(), "티커": t,
                     **{k: v for k, v in m.items() if k != "series"}})
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows).set_index("티커")

    pct = lambda s: s.rank(pct=True) * 100  # noqa: E731
    flow = pct(d["수급강도"]) * 0.25 + (d["매수주체수"] / 4 * 100) * 0.10

    if mode == "역배열→정배열 전환":
        # 바닥에서 올라오는 중인가: 과거 역배열 + 현재 단계적 회복
        score = flow
        score += d["역배열비율60"] * 100 * 0.15          # 최근까지 역배열이었을 것
        score += (d["정배열단계"] / 3 * 100) * 0.20      # 지금 몇 단계 회복했나
        score += pct(d["MA20기울기"]) * 0.15             # MA20 이 상승 전환했나
        score += (d["종가"] > d["MA60"]).astype(int) * 100 * 0.15   # 60일선 회복
        d["기술판정"] = d.apply(
            lambda x: f"{x['배열']} · {int(x['정배열단계'])}/3단계 회복 · MA20 {x['MA20기울기']:+.1f}%", axis=1)
    elif mode == "MACD 골든크로스":
        fresh = d["MACD전환일"].fillna(999)
        score = flow
        score += (fresh <= 5).astype(int) * 100 * 0.25   # 5일 이내 골든크로스
        score += (fresh <= 15).astype(int) * 100 * 0.10
        score += pct(d["MACD히스토"]) * 0.15
        score += (d["종가"] > d["MA20"]).astype(int) * 100 * 0.15
        d["기술판정"] = d.apply(
            lambda x: (f"골든크로스 {int(x['MACD전환일'])}일 전"
                       if pd.notna(x["MACD전환일"]) else "히스토 음수") + f" · {x['배열']}", axis=1)
    else:  # RSI 과매도 반등
        score = flow
        score += (1 - d["RSI최저20"].rank(pct=True)) * 100 * 0.20    # 최근 깊게 눌렸던 종목
        score += ((d["RSI"] > 30) & (d["RSI"] < 55)).astype(int) * 100 * 0.20  # 반등 초입
        score += pct(d["RSI"] - d["RSI최저20"]) * 0.15               # 저점 대비 회복폭
        score += (d["MACD히스토"] > 0).astype(int) * 100 * 0.10
        d["기술판정"] = d.apply(
            lambda x: f"RSI {x['RSI']:.0f} (20일 저점 {x['RSI최저20']:.0f}) · {x['배열']}", axis=1)

    d["기회점수"] = score.round(1)
    return d.sort_values("기회점수", ascending=False)


def tech_scenario(row: pd.Series, mode: str) -> dict:
    reasons, checks = [], []
    for inv in ["기타법인", "외국인", "연기금"]:
        v = row.get(f"순매수_{inv}", 0)
        if v > 0:
            reasons.append(f"{inv} 순매수 {v:,.0f}억 (시총 대비 {row.get(f'강도_{inv}', 0):.2f}%)")

    ma_txt = (f"MA5 {row['MA5']:,.0f} / MA20 {row['MA20']:,.0f} / "
              f"MA60 {row['MA60']:,.0f} / MA120 {row['MA120']:,.0f}")
    reasons.append(f"배열 {row['배열']} · {ma_txt}")
    reasons.append(f"RSI {row['RSI']:.0f} · MACD 히스토 {row['MACD히스토']:+,.0f}")
    if pd.notna(row.get("거래량비")) and row["거래량비"] > 1.2:
        reasons.append(f"최근 5일 거래량이 60일 평균의 {row['거래량비']:.1f}배")

    if mode == "역배열→정배열 전환":
        reasons.append(f"최근 60일 중 {row['역배열비율60']*100:.0f}% 가 역배열 → 바닥권에서 회복 시도")
        bull = (f"MA5 가 MA20 을 넘은 뒤 MA20 이 MA60 을 상향 돌파하면 정배열 완성. "
                f"현재 {int(row['정배열단계'])}/3 단계. 남은 단계가 채워지는지가 관건.")
        entry = f"MA20({row['MA20']:,.0f}) 눌림에서 분할 진입, 정배열 완성 시 추가."
        invalid = f"MA5 가 MA20 아래로 재이탈하거나 MA60({row['MA60']:,.0f}) 하회 시 전환 실패로 간주."
    elif mode == "MACD 골든크로스":
        age = row["MACD전환일"]
        reasons.append(f"MACD 골든크로스 {int(age)}일 경과" if pd.notna(age) else "MACD 히스토 음수 구간")
        bull = "히스토그램이 계속 확대되면 모멘텀 지속. 축소 전환 시 되돌림 경계."
        entry = f"현재가 {row['종가']:,.0f}원. 히스토그램 확대 확인 후 진입."
        invalid = "히스토그램이 다시 음수로 전환되면 무효 (통상 데드크로스 선행 신호)."
    else:
        bull = f"RSI 가 20일 저점 {row['RSI최저20']:.0f} 에서 {row['RSI']:.0f} 까지 회복. 50 돌파 시 추세 전환 확인."
        entry = f"현재가 {row['종가']:,.0f}원. RSI 50 상향 돌파를 확인 신호로."
        invalid = f"RSI 가 20일 저점 {row['RSI최저20']:.0f} 아래로 재하락 시 무효."

    checks.append("이 지표들은 모두 후행 지표입니다 — 이미 일어난 움직임의 확인일 뿐입니다")
    if row.get("순매수_기타법인", 0) > 0:
        checks.append("DART 자기주식취득 공시 대조")
    checks.append("직전 실적·적자 여부·유상증자 공시 확인")
    checks.append(f"거래대금 {row['거래대금']:,.0f}억 — 체결 가능 규모인지")
    return {"reasons": reasons, "bull": bull, "entry": entry,
            "invalid": invalid, "checks": checks}


def tech_figure(ticker: str, name: str, hist: dict, tech: dict):
    """개별 종목 캔들 + 이동평균 + MACD + RSI."""
    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    ohlc = hist["ohlc"].tail(160)
    s = tech["series"]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.035,
                        row_heights=[0.56, 0.22, 0.22])

    o, h, l, c = (_pick(ohlc.columns, x) for x in ("시가", "고가", "저가", "종가"))
    fig.add_trace(go.Candlestick(
        x=ohlc.index, open=ohlc[o], high=ohlc[h], low=ohlc[l], close=ohlc[c], name=name,
        increasing=dict(line=dict(color=C_UP, width=1), fillcolor=C_UP),
        decreasing=dict(line=dict(color=C_DOWN, width=1), fillcolor=C_DOWN)), row=1, col=1)
    for n, col in zip(MA_SET, ["#E6EDF3", C_ACCENT, "#30A46C", "#8E4EC6"]):
        fig.add_trace(go.Scatter(x=ohlc.index, y=s["ma"][n].reindex(ohlc.index),
                                 name=f"MA{n}", mode="lines",
                                 line=dict(color=col, width=1.2)), row=1, col=1)

    line, signal, histo = s["macd"]
    fig.add_trace(go.Bar(x=ohlc.index, y=histo.reindex(ohlc.index), name="히스토",
                         marker_color=[C_UP if v > 0 else C_DOWN
                                       for v in histo.reindex(ohlc.index).fillna(0)],
                         marker_line_width=0, opacity=.6), row=2, col=1)
    fig.add_trace(go.Scatter(x=ohlc.index, y=line.reindex(ohlc.index), name="MACD",
                             line=dict(color="#E6EDF3", width=1.2)), row=2, col=1)
    fig.add_trace(go.Scatter(x=ohlc.index, y=signal.reindex(ohlc.index), name="시그널",
                             line=dict(color=C_ACCENT, width=1.2)), row=2, col=1)

    fig.add_trace(go.Scatter(x=ohlc.index, y=s["rsi"].reindex(ohlc.index), name="RSI",
                             line=dict(color="#8E4EC6", width=1.4)), row=3, col=1)
    for y_, cc_ in [(70, C_UP), (50, C_MUTED), (30, C_DOWN)]:
        fig.add_hline(y=y_, line=dict(color=cc_, width=.8, dash="dot"), row=3, col=1)

    missing = [d for d in pd.date_range(ohlc.index.min(), ohlc.index.max(), freq="D")
               .difference(ohlc.index)]
    if missing:
        fig.update_xaxes(rangebreaks=[dict(values=missing)])
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font=dict(color=C_TEXT, size=11),
                      height=680, margin=dict(l=8, r=8, t=30, b=8),
                      xaxis_rangeslider_visible=False, hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                  bgcolor="rgba(0,0,0,0)"))
    grid = "rgba(255,255,255,.06)"
    fig.update_xaxes(showgrid=True, gridcolor=grid)
    fig.update_yaxes(showgrid=True, gridcolor=grid, zeroline=False)
    fig.update_yaxes(title_text="주가", row=1, col=1)
    fig.update_yaxes(title_text="MACD", row=2, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
    return fig


CHART_PERSONA = {
    "name": "차트 분석가", "icon": "🕯",
    "system": "당신은 이동평균 배열, MACD, RSI, 거래량으로 판단하는 기술적 분석가입니다. "
              "당신은 이 지표들이 모두 후행 지표라는 것을 알고 있습니다. 따라서 '예측'이라는 "
              "말을 쓰지 않고 '확인'과 '조건'으로만 서술합니다. 배열 전환은 완성되기 전까지 "
              "실패할 수 있다는 것을 전제하며, 항상 무효화 가격을 함께 제시합니다.",
    "task": "제시된 종목의 이동평균 배열 단계, MACD 히스토그램, RSI 위치를 근거로 "
            "차트 국면을 판정하십시오. 전환이 완성된 종목과 아직 시도 중인 종목을 구분하고, "
            "각각에 대해 확인 신호와 무효화 가격을 제시하십시오. "
            "지표가 후행한다는 점이 이 판단에 어떤 한계를 만드는지도 한 문장으로 밝히십시오.",
}


# 판단 5인 + 실행 5인 + 차트 1인
ALL_PERSONAS = PERSONAS + EXEC_PERSONAS + [CHART_PERSONA]


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

(tab_chart, tab_flow, tab_corp, tab_screen, tab_plan, tab_persona, tab_bt, tab_diag) = st.tabs(
    ["캔들 차트", "수급 상세", "기타법인 추적", "종목 스크리너", "매매 계획",
     "페르소나 회의", "검증", "진단"])

with tab_chart:
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

with tab_flow:
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

with tab_corp:
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

with tab_bt:
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

    st.divider()
    st.markdown("### 종목 스크리너 검증")
    st.markdown(
        "과거 리밸런스 시점마다 유니버스를 다시 만들어 상위 종목을 뽑고, 보유 기간 뒤 "
        "수익률을 코스피와 비교합니다. 왕복 비용을 차감합니다. **초과수익이 0 근처면 "
        "이 규칙은 코스피를 사는 것과 다르지 않습니다.**"
    )
    b = st.columns(4)
    bt_reb = b[0].number_input("리밸런스 횟수", 4, 24, 10, step=1)
    bt_hold = b[1].number_input("보유 기간(영업일)", 5, 60, 20, step=5)
    bt_top = b[2].number_input("편입 종목 수", 3, 30, 10, step=1)
    bt_mode = b[3].selectbox("전략", ["낙폭과대 반등", "추세 지속"], key="btmode")
    st.caption(f"예상 KRX 호출 약 {int(bt_reb) * 8}회 · 1~3분 소요 · 결과는 2시간 캐시")

    if st.button("스크리너 백테스트 실행"):
        try:
            with st.spinner("과거 시점 유니버스를 재구성하는 중… 시간이 걸립니다"):
                sbt = screener_backtest(int(bt_reb), int(bt_hold), bt_mode, int(bt_top),
                                        3000, 20, 20, 0.015, 0.15, krx_id)
            st.session_state["sbt"] = sbt
        except Exception as e:  # noqa: BLE001
            log_exc("스크리너BT", e, "백테스트 실패")
            st.error(f"백테스트 실패 — {type(e).__name__}: {e}")

    sbt = st.session_state.get("sbt")
    if sbt:
        s_ = sbt["summary"]
        m1 = st.columns(4)
        exc = s_["평균 초과수익(%)"]
        m1[0].markdown(kpi("평균 초과수익", f"{exc:+.2f}%p",
                           f"코스피 대비 · {s_['구간수']}개 구간",
                           C_UP if exc > 0 else C_DOWN), unsafe_allow_html=True)
        m1[1].markdown(kpi("코스피 이긴 구간", f"{s_['코스피 이긴 구간(%)']:.0f}%",
                           "50% 는 동전던지기", C_ACCENT), unsafe_allow_html=True)
        m1[2].markdown(kpi("전략 평균(비용차감)", f"{s_['평균 수익(비용차감,%)']:+.2f}%",
                           f"코스피 {s_['코스피 평균(%)']:+.2f}%",
                           tone_for(s_["평균 수익(비용차감,%)"])), unsafe_allow_html=True)
        m1[3].markdown(kpi("최악 구간", f"{s_['최악 구간(%)']:+.2f}%",
                           f"표준편차 {s_['구간 표준편차(%)']:.2f}%p", C_DOWN),
                       unsafe_allow_html=True)

        st.dataframe(sbt["per_period"], use_container_width=True, hide_index=True,
                     height=min(420, 60 + 36 * len(sbt["per_period"])))
        cmp_df = sbt["per_period"].set_index("진입일")[["비용차감(%)", "코스피(%)"]]
        st.markdown("**구간별 전략 vs 코스피**")
        st.bar_chart(cmp_df, color=[C_UP, C_MUTED], height=260)

        if abs(exc) < 0.5:
            st.warning("초과수익이 0 근처입니다. 이 선별 규칙은 코스피를 그냥 사는 것과 "
                       "구분되지 않습니다. 가중치를 바꾸거나 전략을 재검토하세요.", icon="⚠️")
        elif exc < 0:
            st.error("규칙이 코스피보다 못했습니다. 현재 형태로 실전에 쓰지 마십시오.", icon="🛑")
        else:
            st.info(f"구간 {s_['구간수']}개는 통계적으로 매우 적은 표본입니다. "
                    "구간 수를 늘려도 결과가 유지되는지, 다른 기간에서도 재현되는지 "
                    "확인하기 전에는 우연일 가능성이 큽니다.", icon="🔬")


with tab_diag:
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


# ──────────────────────────────────────────────────────────────────────────────
# 종목 스크리너 탭
# ──────────────────────────────────────────────────────────────────────────────

with tab_screen:
    st.markdown("### 수급 기반 종목 후보 선별")
    st.markdown(
        "시장 전체를 8회 호출로 훑어 **순매수를 시가총액 대비 비율로** 환산합니다. "
        "절대 금액으로 줄 세우면 대형주만 남아 정보가 없기 때문입니다. "
        "여기서 나오는 것은 매수 추천이 아니라 **확인해 볼 가치가 있는 후보 목록**입니다."
    )

    f = st.columns(5)
    mode = f[0].selectbox("전략", ALL_MODES,
                          help="뒤 3개는 개별 종목 시세를 추가로 조회합니다(2단계).")
    n_pick = f[1].number_input("종목 수", 5, 30, 10, step=1)
    min_cap = f[2].number_input("최소 시총(억)", 500, 100000, 3000, step=500)
    min_val = f[3].number_input("최소 거래대금(억)", 1, 5000, 20, step=5)
    sc_days = f[4].number_input("수급 집계일", 5, 60, 20, step=5)

    is_tech = mode in TECH_MODES
    if is_tech:
        t1, t2 = st.columns([1, 3])
        cand_n = t1.number_input("2단계 후보 수", 20, 120, 50, step=10,
                                 help="1단계 수급 상위 N종목만 개별 시세를 조회합니다.")
        t2.caption(f"KRX 호출 = 8회(전종목 스냅샷) + {int(cand_n)}회(개별 시세). "
                   f"약 {int(cand_n)*0.4/60:.0f}~{int(cand_n)*0.9/60:.0f}분 소요 · 1시간 캐시")
    else:
        cand_n = 0

    if st.button("종목 스크리닝 실행", type="primary"):
        try:
            with st.spinner("시장 전체 스냅샷을 수집하는 중… (8회 호출)"):
                uni = load_universe(int(sc_days), 60, krx_id)
            res = screen_stocks(uni, "낙폭과대 반등" if is_tech else mode,
                                int(min_cap), int(min_val))
            tech_map = {}
            if is_tech and not res.empty:
                cands = tuple(res.sort_values("수급강도", ascending=False)
                              .head(int(cand_n)).index)
                hist = load_price_matrix(cands, 224, krx_id)
                for t_, h_ in hist.items():
                    m_ = compute_tech(h_["close"], h_.get("volume"))
                    if m_:
                        tech_map[t_] = m_
                st.session_state["hist"] = hist
                st.session_state["tech"] = tech_map
                res = screen_with_tech(res.loc[[c for c in cands if c in tech_map]],
                                       tech_map, mode)
            st.session_state["screen"] = {
                "res": res, "mode": mode, "n": int(n_pick),
                "days": int(sc_days), "base": uni["기준일"].iloc[0] if len(uni) else "",
                "universe_n": len(uni), "filtered_n": len(res), "tech": is_tech,
            }
        except Exception as e:  # noqa: BLE001
            log_exc("스크리너", e, "스크리닝 실패")
            st.error(f"스크리닝 실패 — {type(e).__name__}: {e}")

    sc = st.session_state.get("screen")
    if sc and not sc["res"].empty:
        picks = sc["res"].head(sc["n"])
        st.session_state["picks"] = picks
        st.caption(
            f"기준일 {sc['base']} · 전체 {sc['universe_n']}종목 → 필터 통과 {sc['filtered_n']}종목 "
            f"→ 상위 {len(picks)}종목 · 전략 '{sc['mode']}' · 수급 집계 {sc['days']}일"
        )

        cols_base = ["종목명", "기회점수", "종가", "시가총액", "PBR",
                     "순매수_기타법인", "순매수_외국인", "순매수_연기금",
                     "매수주체수", "거래대금"]
        names_base = ["종목명", "점수", "종가", "시총(억)", "PBR",
                      "기타법인(억)", "외국인(억)", "연기금(억)", "매수주체", "거래대금(억)"]
        fmt = {"점수": "{:.0f}", "종가": "{:,.0f}", "시총(억)": "{:,.0f}", "PBR": "{:.2f}",
               "기타법인(억)": "{:+,.0f}", "외국인(억)": "{:+,.0f}", "연기금(억)": "{:+,.0f}",
               "매수주체": "{:.0f}", "거래대금(억)": "{:,.0f}"}
        if sc.get("tech"):
            cols_base += ["배열", "정배열단계", "RSI", "MACD히스토", "MA20기울기", "이격도60"]
            names_base += ["배열", "단계", "RSI", "MACD히스토", "MA20기울기%", "60일이격%"]
            fmt.update({"단계": "{:.0f}", "RSI": "{:.0f}", "MACD히스토": "{:+,.0f}",
                        "MA20기울기%": "{:+.1f}", "60일이격%": "{:+.1f}"})
        else:
            cols_base += ["단기등락률", "장기등락률"]
            names_base += [f"{sc['days']}일%", "60일%"]
            fmt.update({f"{sc['days']}일%": "{:+.1f}", "60일%": "{:+.1f}"})
        view = picks[cols_base].copy()
        view.columns = names_base
        st.dataframe(view.style.format(fmt, na_rep="—"),
                     use_container_width=True, height=min(460, 60 + 36 * len(view)))

        st.markdown("#### 포착 근거와 시나리오")
        for ticker, row in picks.iterrows():
            sc_dict = (tech_scenario(row, sc["mode"]) if sc.get("tech")
                       else build_scenario(row, sc["mode"], sc["days"], 60))
            tail = (f"{row['배열']} · RSI {row['RSI']:.0f}" if sc.get("tech")
                    else f"60일 {row['장기등락률']:+.1f}%")
            head = (f"{row['종목명']} ({ticker}) · 점수 {row['기회점수']:.0f} · "
                    f"{row['종가']:,.0f}원 · {tail}")
            with st.expander(head):
                a, b = st.columns([1, 1])
                with a:
                    st.markdown("**포착 이유**")
                    for r in sc_dict["reasons"]:
                        st.markdown(f"- {r}")
                    st.markdown("**강세 시나리오**")
                    st.markdown(sc_dict["bull"])
                with b:
                    st.markdown("**진입 구상**")
                    st.markdown(sc_dict["entry"])
                    st.markdown("**무효화 조건**")
                    st.markdown(f":red[{sc_dict['invalid']}]")
                    st.markdown("**직접 확인할 것**")
                    for c_ in sc_dict["checks"]:
                        st.markdown(f"- {c_}")
                if sc.get("tech"):
                    hm = st.session_state.get("hist", {}).get(ticker)
                    tm = st.session_state.get("tech", {}).get(ticker)
                    if hm and tm:
                        try:
                            st.plotly_chart(tech_figure(ticker, row["종목명"], hm, tm),
                                            use_container_width=True,
                                            config={"displaylogo": False},
                                            key=f"tf_{ticker}")
                        except Exception as e:  # noqa: BLE001
                            st.caption(f"차트 생성 실패: {e}")

        st.download_button("후보 CSV 내려받기",
                           data=view.to_csv().encode("utf-8-sig"),
                           file_name=f"screen_{now_kst():%Y%m%d_%H%M}.csv", mime="text/csv")

        st.warning(
            "이 목록은 공개 수급·시세·재무 데이터에 고정 규칙을 적용한 산술 결과입니다. "
            "뉴스, 실적 발표, 공시, 업황, 지배구조는 전혀 반영되어 있지 않습니다. "
            "종목 단위 점수는 백테스트로 검증된 바 없으며, 시장 스코어와 달리 검증 수단도 "
            "아직 없습니다. 실제 매매 전에 각 종목의 공시와 실적을 직접 확인하십시오.",
            icon="⚠️")
    elif sc:
        st.info("필터를 통과한 종목이 없습니다. 최소 시총·거래대금을 낮춰 보세요.")
    else:
        st.caption("조건을 정하고 버튼을 누르세요. 결과는 30분간 캐시됩니다.")


# ──────────────────────────────────────────────────────────────────────────────
# 페르소나 회의 탭
# ──────────────────────────────────────────────────────────────────────────────

with tab_persona:
    st.markdown("### 페르소나 회의")
    st.markdown(
        "같은 데이터를 다섯 명의 서로 다른 관점에 각각 넘기고, 마지막에 조율합니다. "
        "합의를 만드는 것이 목적이 아니라 **어디서 의견이 갈리는지** 드러내는 것이 목적입니다."
    )
    chosen_names = st.multiselect(
        "소집할 페르소나", [p["name"] for p in ALL_PERSONAS],
        default=[p["name"] for p in ALL_PERSONAS],
        help="앞 5명은 판단, 뒤 5명은 실행을 담당합니다. 호출 수 = 선택 인원 + 1")
    chosen = [p for p in ALL_PERSONAS if p["name"] in chosen_names]
    if chosen:
        cols_n = min(5, len(chosen))
        for start in range(0, len(chosen), cols_n):
            pc = st.columns(cols_n)
            for j, p in enumerate(chosen[start:start + cols_n]):
                pc[j].markdown(kpi(f"{p['icon']} {p['name']}", "",
                                   p["task"][:34] + "…", C_MUTED), unsafe_allow_html=True)
    st.write("")

    if not groq_api_key:
        st.info("Groq API Key 를 입력하면 페르소나 회의를 실행할 수 있습니다.", icon="🔑")
    else:
        picks_df = st.session_state.get("picks")
        oc = st.columns([1, 1, 2])
        include = oc[0].checkbox("스크리너 결과 포함", value=picks_df is not None,
                                 disabled=picks_df is None)
        run_all = oc[1].button("회의 소집", type="primary", use_container_width=True)
        if picks_df is None:
            oc[2].caption("종목 스크리너를 먼저 실행하면 종목별 의견까지 받을 수 있습니다.")

        if run_all:
            model, available = resolve_groq_model(groq_api_key, model_choice)
            st.caption(f"모델 `{model}` · 호출 {len(chosen) + 1}회")
            ctx = persona_context(
                df, sig, guide, cash_ratio,
                picks_df if include else None,
                st.session_state.get("screen", {}).get("mode", "-"))
            pl = st.session_state.get("plan")
            if pl is not None and not pl["plan"].empty:
                ctx += exec_context(pl["plan"], pl["capital"], pl["gate"], pl["gate_label"],
                                    pl["risk_pct"], pl["stop_pct"], pl["fee_pct"], pl["tax_pct"])
            opinions: dict[str, str] = {}
            for p in chosen:
                st.markdown(f"#### {p['icon']} {p['name']}")
                try:
                    opinions[p["name"]] = st.write_stream(
                        run_persona(groq_api_key, model, p, ctx))
                except Exception as e:  # noqa: BLE001
                    log_exc("페르소나", e, f"{p['name']} 실패")
                    st.error(f"{p['name']} 실패 — {type(e).__name__}: {e}")
            if opinions:
                st.divider()
                st.markdown("#### ⚖️ 종합 판정")
                try:
                    final = st.write_stream(run_synthesis(groq_api_key, model, ctx, opinions))
                    st.session_state["council"] = {
                        "opinions": opinions, "final": final,
                        "model": model, "at": now_kst()}
                except Exception as e:  # noqa: BLE001
                    log_exc("페르소나", e, "종합 판정 실패")
                    st.error(f"종합 판정 실패 — {type(e).__name__}: {e}")
        elif st.session_state.get("council"):
            cl = st.session_state["council"]
            st.caption(f"모델 `{cl['model']}` · 생성 {cl['at']:%Y-%m-%d %H:%M} KST")
            for name, text in cl["opinions"].items():
                icon = next((p["icon"] for p in ALL_PERSONAS if p["name"] == name), "•")
                with st.expander(f"{icon} {name}"):
                    st.markdown(text)
            st.markdown("#### ⚖️ 종합 판정")
            st.markdown(cl["final"])



# ──────────────────────────────────────────────────────────────────────────────
# 매매 계획 탭 — 신호를 주문으로 바꾸는 곳
# ──────────────────────────────────────────────────────────────────────────────

with tab_plan:
    st.markdown("### 매매 계획")
    st.markdown(
        "신호가 아니라 **여기가 계좌를 지키는 자리**입니다. 진입가·손절가·수량·리스크를 "
        "주문 넣기 전에 확정하고, 세 가지 한도 중 가장 낮은 값으로 크기를 정합니다."
    )

    gate, gate_label = market_gate(sig["score"])
    g = st.columns(3)
    g[0].markdown(kpi("시장 국면 게이트", f"×{gate:.2f}",
                      f"수급 스코어 {sig['score']} → {gate_label}",
                      C_UP if gate >= 0.75 else C_ACCENT if gate > 0 else C_DOWN),
                  unsafe_allow_html=True)
    stale = (now_kst().date() - df.index[-1].date()).days
    g[1].markdown(kpi("데이터 신선도", f"{stale}일 전",
                      f"최근 영업일 {df.index[-1]:%Y-%m-%d}",
                      C_UP if stale <= 3 else C_DOWN), unsafe_allow_html=True)
    g[2].markdown(kpi("KRX 인증", "정상" if AUTH_STATE.get("성공") else "실패",
                      AUTH_STATE.get("메시지", ""),
                      C_UP if AUTH_STATE.get("성공") else C_DOWN), unsafe_allow_html=True)

    if gate == 0:
        st.error("현재 국면에서는 신규 진입을 권하지 않습니다. 수량이 0으로 계산됩니다.", icon="🛑")
    if stale > 4:
        st.warning(f"데이터가 {stale}일 지났습니다. 최신 시세로 진입가를 다시 확인하세요.", icon="⏱")

    st.divider()
    p1 = st.columns(4)
    capital = p1[0].number_input("계좌 자본(만원)", 100, 1000000, 5000, step=100) * 1e4
    risk_pct = p1[1].number_input("종목당 리스크(%)", 0.1, 5.0, 1.0, step=0.1,
                                  help="손절까지 갔을 때 계좌에서 잃을 비율")
    stop_pct = p1[2].number_input("손절폭(%)", 3.0, 30.0, 10.0, step=0.5)
    target_r = p1[3].number_input("목표 배수(R)", 1.0, 5.0, 2.0, step=0.5,
                                  help="손절폭의 몇 배를 목표로 할지")

    p2 = st.columns(4)
    max_weight = p2[0].number_input("종목당 비중 상한(%)", 1.0, 50.0, 10.0, step=1.0)
    adv_cap = p2[1].number_input("거래대금 대비 상한(%)", 0.1, 20.0, 2.0, step=0.1,
                                 help="주문금액이 일평균 거래대금의 몇 %를 넘지 않을지")
    fee_pct = p2[2].number_input("수수료(편도 %)", 0.0, 1.0, 0.015, step=0.005, format="%.3f")
    tax_pct = p2[3].number_input("매도 거래세(%)", 0.0, 1.0, 0.15, step=0.01, format="%.3f",
                                 help="세율은 수시로 바뀝니다. 본인 증권사 기준으로 확인하세요.")

    picks_df = st.session_state.get("picks")
    if picks_df is None or picks_df.empty:
        st.info("종목 스크리너를 먼저 실행하면 계획이 생성됩니다.", icon="📋")
    else:
        plan = build_trade_plan(picks_df, capital, risk_pct, stop_pct, max_weight,
                                adv_cap, fee_pct, tax_pct, gate, target_r)
        if plan.empty:
            st.warning("현재 조건에서 수량이 1주 이상 나오는 종목이 없습니다. "
                       "자본을 늘리거나 손절폭·한도를 조정하세요.")
        else:
            st.session_state["plan"] = {
                "plan": plan, "capital": capital, "gate": gate, "gate_label": gate_label,
                "risk_pct": risk_pct, "stop_pct": stop_pct,
                "fee_pct": fee_pct, "tax_pct": tax_pct}

            inv_total = plan["금액"].sum()
            risk_total = plan["리스크(원)"].sum()
            cost_total = plan["왕복비용(원)"].sum()
            k = st.columns(4)
            k[0].markdown(kpi("총 투입", f"{inv_total/1e4:,.0f}만원",
                              f"계좌의 {inv_total/capital*100:.1f}%", C_ACCENT),
                          unsafe_allow_html=True)
            k[1].markdown(kpi("총 리스크", f"{risk_total/1e4:,.0f}만원",
                              f"전 종목 손절 시 계좌의 {risk_total/capital*100:.2f}%",
                              C_DOWN if risk_total/capital*100 > 6 else C_UP),
                          unsafe_allow_html=True)
            k[2].markdown(kpi("왕복 비용", f"{cost_total/1e4:,.0f}만원",
                              f"투입액의 {cost_total/inv_total*100:.3f}%", C_MUTED),
                          unsafe_allow_html=True)
            k[3].markdown(kpi("편입 종목", f"{len(plan)}개",
                              f"평균 {plan['계좌대비(%)'].mean():.1f}% / 종목", C_MUTED),
                          unsafe_allow_html=True)

            if risk_total / capital * 100 > 6:
                st.error(
                    f"총 리스크가 계좌의 {risk_total/capital*100:.1f}% 입니다. "
                    "전 종목이 동시에 손절되는 상황은 하락장에서 실제로 일어납니다. "
                    "6% 이내를 권합니다 — 종목당 리스크를 낮추거나 종목 수를 줄이세요.", icon="🔥")

            view = plan.drop(columns=["점수"]).copy()
            st.dataframe(
                view.style.format({
                    "진입가": "{:,.0f}", "손절가": "{:,.0f}", "목표가": "{:,.0f}",
                    "수량": "{:,.0f}", "금액": "{:,.0f}", "계좌대비(%)": "{:.2f}",
                    "리스크(원)": "{:,.0f}", "왕복비용(원)": "{:,.0f}",
                    "손익분기(%)": "{:.3f}", "거래대금(억)": "{:,.0f}"}, na_rep="—"),
                use_container_width=True, height=min(460, 60 + 36 * len(view)))

            binding = plan["제약"].value_counts()
            st.caption("크기를 결정한 제약: " +
                       " · ".join(f"{k_} {v_}종목" for k_, v_ in binding.items()))

            st.download_button("매매 계획 CSV 내려받기",
                               data=plan.to_csv().encode("utf-8-sig"),
                               file_name=f"plan_{now_kst():%Y%m%d_%H%M}.csv", mime="text/csv")

            st.markdown("#### 주문 전 체크리스트")
            checks = [
                "각 종목의 최근 공시를 DART 에서 확인했다 (특히 자기주식취득·유상증자)",
                "직전 분기 실적과 적자 여부를 확인했다",
                "손절가를 주문 시스템에 실제로 입력했다 (머릿속 손절은 지켜지지 않는다)",
                f"총 리스크 {risk_total/capital*100:.2f}% 를 감당할 수 있다",
                "같은 섹터에 과도하게 몰려 있지 않다",
                "이 계획이 틀렸을 때 무엇을 보고 알 것인지 정했다",
            ]
            done = [st.checkbox(c, key=f"chk_{i}") for i, c in enumerate(checks)]
            if all(done):
                st.success("체크리스트 완료. 계획대로 집행하세요.", icon="✅")
            else:
                st.caption(f"{sum(done)}/{len(checks)} 완료 — 전부 확인하기 전에는 주문하지 마세요.")


st.divider()
st.markdown(
    '<div class="cm-disc">이 도구는 공개된 KRX 수급 데이터를 정리해 보여주는 참고 자료입니다. '
    "수급 스코어와 투입 비율은 화면에 표시된 고정 규칙의 산술 결과이며, 백테스트 수치는 표본이 "
    "겹치는 구간 통계라 미래 수익을 예측하지 않습니다. 투자 판단과 그 결과에 대한 책임은 "
    "이용자 본인에게 있습니다.</div>",
    unsafe_allow_html=True,
)
