"""
CENTURION Macro — 코스피 수급 대시보드
=====================================
개인 / 외국인 / 기관 / 기타법인 순매수 흐름을 추적하고,
규칙 기반 수급 스코어와 LLM 리포트로 현금 투입 타이밍을 점검합니다.

데이터: KRX (pykrx) — data.krx.co.kr 계정 로그인 필요
LLM:   Groq (선택 사항 — 키가 없어도 규칙 기반 분석은 그대로 동작)

실행 로그는 화면의 '진단' 탭과 서버 로그 양쪽에 남습니다.
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

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
KOSPI_INDEX_TICKER = "1001"
INVESTORS = ["개인", "외국인", "기관", "기타법인"]

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}

# 한국 시장 관례: 매수/상승 = 적색, 매도/하락 = 청색
COLOR_BUY = "#E5484D"
COLOR_SELL = "#3E7BFA"
COLOR_ACCENT = "#F5A524"
COLOR_MUTED = "#8A93A5"
SERIES_COLORS = ["#8A93A5", "#E5484D", "#3E7BFA", "#F5A524"]

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

# ──────────────────────────────────────────────────────────────────────────────
# 스타일
# ──────────────────────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard-dynamic-subset.css');
html, body, [class*="css"] {
  font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI',
               'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
}
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
.cm-head { display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; }
.cm-head h1 { font-size: 1.65rem; font-weight: 700; letter-spacing: -.02em; margin: 0; }
.cm-head .cm-asof { color: #8A93A5; font-size: .85rem; font-variant-numeric: tabular-nums; }
.cm-sub { color: #8A93A5; font-size: .9rem; margin: .35rem 0 1.4rem 0; }
.cm-kpi { border: 1px solid rgba(255,255,255,.08); border-left: 3px solid var(--tone, #8A93A5);
          border-radius: 10px; padding: .85rem 1rem .9rem 1rem;
          background: rgba(255,255,255,.025); height: 100%; }
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
.cm-trig { display: flex; gap: .6rem; align-items: flex-start; padding: .45rem 0;
           border-bottom: 1px dashed rgba(255,255,255,.07); }
.cm-trig:last-child { border-bottom: none; }
.cm-trig .cm-mark { width: 1.2rem; flex: none; }
.cm-trig .cm-text { flex: 1; font-size: .9rem; }
.cm-trig .cm-w { color: #6E7787; font-size: .8rem; font-variant-numeric: tabular-nums; }
.cm-trig .cm-detail { color: #8A93A5; font-size: .8rem; font-variant-numeric: tabular-nums; }
.cm-disc { color: #6E7787; font-size: .78rem; line-height: 1.6; }
</style>
""",
    unsafe_allow_html=True,
)


# ──────────────────────────────────────────────────────────────────────────────
# 실행 로그
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [centurion] %(levelname)s %(message)s",
    stream=sys.stderr,
)
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
    """화면과 서버 로그 양쪽에 한 줄 남깁니다."""
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
    tb = traceback.format_exc(limit=4).strip().splitlines()
    for line in tb[-4:]:
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
    """
    pykrx 는 내부 예외를 삼키고 'Error occurred in ...' 를 stdout 으로만 흘립니다.
    그 문구를 잡아 로그에 편입시킵니다.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        for line in buf.getvalue().splitlines():
            if line.strip():
                lvl = "WARN" if "Error" in line or "실패" in line else "INFO"
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

def fmt_eok(v: float, signed: bool = True) -> str:
    if v is None or pd.isna(v):
        return "—"
    sign = "+" if (signed and v > 0) else ""
    return f"{sign}{v:,.0f}"


def tone_for(v: float) -> str:
    if v is None or pd.isna(v) or v == 0:
        return COLOR_MUTED
    return COLOR_BUY if v > 0 else COLOR_SELL


def kpi(label: str, value: str, note: str = "", tone: str = COLOR_MUTED) -> str:
    return (
        f'<div class="cm-kpi" style="--tone:{tone}">'
        f'<div class="cm-label">{label}</div>'
        f'<div class="cm-value">{value}</div>'
        f'<div class="cm-note">{note}</div></div>'
    )


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


def _retry(fn: Callable, stage: str, tries: int = 3, delay: float = 0.8):
    last = None
    for i in range(tries):
        try:
            return fn()
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
    """
    pykrx 를 지연 임포트하고 KRX 로그인 세션을 준비합니다.

    KRX 는 2026년부터 비로그인 요청에 본문 'LOGOUT' 과 HTTP 400 을 돌려주므로
    data.krx.co.kr 계정이 사실상 필수입니다.
    """
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
        AUTH_STATE.update(
            시도=False, 성공=False, 계정="",
            메시지="KRX_ID / KRX_PW 미설정 — 익명 요청은 KRX 가 거부합니다",
        )
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
# 진단 프로브
# ──────────────────────────────────────────────────────────────────────────────

def probe_krx(krx_id: str = "", krx_pw: str = "") -> dict:
    """pykrx 와 동일한 요청을 직접 보내 KRX 원본 응답을 확인합니다."""
    import requests

    end = now_kst().strftime("%Y%m%d")
    start = (now_kst() - timedelta(days=14)).strftime("%Y%m%d")
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT00301",
        "indIdx": "1",
        "indIdx2": "001",
        "strtDd": start,
        "endDd": end,
        "share": "2",
        "money": "3",
        "csvxls_isNo": "false",
    }

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
        req = session or requests.Session()
        r = req.post(KRX_JSON_URL, headers=KRX_HEADERS, data=payload, timeout=20)
        body = r.text[:300]
        out.update(
            {
                "HTTP상태": r.status_code,
                "ContentType": r.headers.get("Content-Type", "?"),
                "응답길이": len(r.content),
                "응답앞부분": body,
            }
        )
        stripped = r.text.strip().upper()
        if stripped == "LOGOUT" or (r.status_code == 400 and len(r.content) < 32):
            out["판정"] = "인증 필요 — KRX 가 로그인되지 않은 요청을 거부했습니다"
        else:
            try:
                js = r.json()
                key = next((k for k in js if isinstance(js[k], list)), None)
                rows = len(js[key]) if key else 0
                out["데이터행수"] = rows
                out["판정"] = (
                    f"정상 — {rows}행 수신" if rows else "JSON 은 왔으나 데이터가 비었습니다"
                )
            except Exception:  # noqa: BLE001
                low = body.lower()
                out["판정"] = (
                    "HTML 응답 — 서버 IP 차단 가능성"
                    if "<html" in low or "<!doctype" in low
                    else "예상치 못한 응답 형식"
                )
    except Exception as e:  # noqa: BLE001
        out["판정"] = f"요청 실패 — {type(e).__name__}: {e}"
        log_exc("프로브", e, "KRX 직접 요청 실패")

    log("프로브", "KRX 직접 요청 완료", 판정=out.get("판정", "?"))
    return out


def classify_failure(exc: BaseException, entries: list[dict]) -> tuple[str, str]:
    """로그와 예외를 근거로 원인과 조치를 한 줄씩 돌려줍니다."""
    blob = " ".join(e["내용"] for e in entries)
    msg = f"{type(exc).__name__}: {exc}"

    if not AUTH_STATE.get("성공"):
        if not AUTH_STATE.get("시도"):
            return (
                "KRX 계정이 설정되지 않았습니다.",
                "KRX 는 로그인하지 않은 요청에 본문 'LOGOUT' 과 HTTP 400 을 돌려줍니다. "
                "data.krx.co.kr 에 가입한 뒤 Secrets 에 KRX_ID / KRX_PW 를 넣거나 "
                "사이드바에 직접 입력하세요.",
            )
        return (
            "KRX 로그인에 실패했습니다.",
            f"{AUTH_STATE.get('메시지')} — 아이디·비밀번호를 다시 확인하고, "
            "브라우저에서 data.krx.co.kr 로그인이 되는지 먼저 확인하세요.",
        )
    if "Expecting value" in blob or "JSONDecodeError" in blob:
        return (
            "KRX 가 JSON 이 아닌 응답을 보냈습니다.",
            "로그인은 됐지만 세션이 곧바로 만료됐을 수 있습니다. "
            "진단 탭의 KRX 직접 요청으로 원본 응답을 확인하세요.",
        )
    if "KeyError" in msg or "KeyError" in blob:
        return (
            "응답은 왔지만 컬럼 구조가 예상과 다릅니다.",
            "KRX 가 응답 형식을 바꿨을 수 있습니다. pykrx 를 최신으로 올려 보세요.",
        )
    if "Timeout" in msg or "Connection" in msg:
        return ("KRX 서버에 연결하지 못했습니다.", "일시적 장애일 수 있습니다. 잠시 후 새로고침하세요.")
    return ("원인을 자동 분류하지 못했습니다.", "진단 탭의 전체 실행 로그를 확인하세요.")


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 레이어
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_index(start: str, end: str) -> pd.DataFrame:
    stock = get_stock_api()
    for name in ("get_index_ohlcv", "get_index_ohlcv_by_date"):
        fn = getattr(stock, name, None)
        if fn is None:
            log("지수조회", f"{name} 없음", level="DEBUG")
            continue
        try:
            with capture_stdout("지수조회"):
                df = _retry(lambda: fn(start, end, KOSPI_INDEX_TICKER), "지수조회")
        except Exception as e:  # noqa: BLE001
            log_exc("지수조회", e, f"{name} 호출 실패")
            continue
        if df is None or df.empty:
            log("지수조회", f"{name} 이 빈 결과 반환", level="WARN")
            continue
        close_col = _pick(df.columns, "종가", "close")
        if close_col is None:
            log("지수조회", f"{name} 응답에 종가 컬럼 없음", level="WARN",
                컬럼=",".join(map(str, df.columns))[:120])
            continue
        out = pd.DataFrame(
            {"코스피지수": pd.to_numeric(df[close_col], errors="coerce")}
        )
        out.index = pd.to_datetime(df.index)
        out = out.dropna()
        log("지수조회", f"{name} 성공", 행수=len(out),
            기간=f"{out.index[0]:%Y-%m-%d}~{out.index[-1]:%Y-%m-%d}" if len(out) else "-")
        return out
    return pd.DataFrame(columns=["코스피지수"])


def _normalize_flow(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df.index = pd.to_datetime(df.index)
    cols = list(df.columns)
    mapping = {
        "개인": _pick(cols, "개인"),
        "외국인": _pick(cols, "외국인합계", "외국인"),
        "기관": _pick(cols, "기관합계", "기관"),
        "기타법인": _pick(cols, "기타법인"),
    }
    log("수급정규화", "컬럼 매핑", **{k: (v or "없음") for k, v in mapping.items()})
    out = pd.DataFrame(index=df.index)
    for key, col in mapping.items():
        out[key] = pd.to_numeric(df[col], errors="coerce") if col is not None else 0.0
    return (out / 1e8).round(0)


def _fetch_flow_by_date(start: str, end: str) -> pd.DataFrame:
    stock = get_stock_api()
    fn = getattr(stock, "get_market_trading_value_by_date", None)
    if fn is None:
        raise AttributeError("get_market_trading_value_by_date 없음")
    with capture_stdout("수급조회"):
        df = _retry(lambda: fn(start, end, "KOSPI"), "수급조회")
    if df is None or df.empty:
        raise ValueError("수급 응답이 비어 있습니다")
    cols = list(df.columns)
    if _pick(cols, "개인") is None or _pick(cols, "외국인합계", "외국인") is None:
        raise ValueError(f"예상한 투자자 컬럼 없음: {cols}")
    log("수급조회", "일괄 조회 성공", 행수=len(df))
    return _normalize_flow(df)


def _fetch_flow_loop(dates: Iterable[pd.Timestamp]) -> pd.DataFrame:
    stock = get_stock_api()
    rows = {}
    dates = list(dates)
    log("수급조회", "일자별 폴백 시작", 대상일수=len(dates))
    for d in dates:
        ds = pd.Timestamp(d).strftime("%Y%m%d")
        try:
            with capture_stdout("수급조회"):
                df = _retry(
                    lambda: stock.get_market_trading_value_by_investor(ds, ds, "KOSPI"),
                    "수급조회",
                    tries=2,
                )
        except Exception as e:  # noqa: BLE001
            log("수급조회", f"{ds} 실패 — {type(e).__name__}", level="WARN")
            continue
        col = _pick(df.columns, "순매수")
        if col is None:
            continue
        rows[pd.Timestamp(d)] = df[col]
    if not rows:
        raise RuntimeError("일자별 조회에서 한 건도 수집하지 못했습니다")
    log("수급조회", "일자별 폴백 완료", 수집일수=len(rows))
    return _normalize_flow(pd.DataFrame(rows).T)


@st.cache_data(ttl=1800, show_spinner=False)
def load_market(lookback: int, ma_window: int, auth_key: str) -> tuple[pd.DataFrame, dict]:
    log_reset()
    log("시작", "데이터 수집 시작", 조회일수=lookback, 이동평균=ma_window,
        계정="설정됨" if auth_key else "없음")

    meta: dict = {"경고": []}
    today = now_kst()
    flow_start = (today - timedelta(days=int(lookback * 2.2) + 20)).strftime("%Y%m%d")
    index_start = (today - timedelta(days=int((lookback + ma_window) * 2.2) + 40)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    get_stock_api()  # 인증 먼저 수행해 로그에 남김

    with timed("지수조회", f"코스피 지수 {index_start}~{end}"):
        idx = _fetch_index(index_start, end)
    if idx.empty:
        meta["로그"] = list(_LOG)
        raise RuntimeError(f"코스피 지수 응답이 비어 있습니다 (조회 {index_start}~{end})")

    idx["MA"] = idx["코스피지수"].rolling(ma_window, min_periods=max(2, ma_window // 2)).mean()

    with timed("수급조회", f"투자자별 순매수 {flow_start}~{end}"):
        try:
            flow = _fetch_flow_by_date(flow_start, end)
            meta["조회경로"] = "get_market_trading_value_by_date (일괄)"
        except Exception as e:  # noqa: BLE001
            log_exc("수급조회", e, "일괄 조회 실패 — 폴백 전환")
            meta["경고"].append(f"일괄 조회 실패 → 일자별 폴백 ({type(e).__name__})")
            flow = _fetch_flow_loop(idx.index[-(lookback + 5):])
            meta["조회경로"] = "get_market_trading_value_by_investor (폴백)"

    df = flow.join(idx, how="inner").sort_index()
    before = len(df)
    df = df[df[INVESTORS].abs().sum(axis=1) > 0]
    if before != len(df):
        log("병합", "무거래일 제거", 제거=before - len(df))
    if df.empty:
        meta["로그"] = list(_LOG)
        raise RuntimeError("수급·지수 병합 결과가 비어 있습니다")

    df = df.tail(lookback)
    meta.update(
        {
            "행수": len(df),
            "수집시각": now_kst().strftime("%Y-%m-%d %H:%M:%S KST"),
            "최근영업일": f"{df.index[-1]:%Y-%m-%d}",
            "인증": dict(AUTH_STATE),
        }
    )
    log("완료", "데이터 수집 완료", 행수=len(df), 최근일=f"{df.index[-1]:%Y-%m-%d}")
    meta["로그"] = list(_LOG)
    return df, meta


# ──────────────────────────────────────────────────────────────────────────────
# 시그널 엔진
# ──────────────────────────────────────────────────────────────────────────────

RULES = [
    ("외국인 5일 누적 순매수 > 0", 30, "외국인은 코스피 방향성의 1차 동인입니다."),
    ("기관 5일 누적 순매수 > 0", 20, "기관 동반 매수는 추세의 지속성을 높입니다."),
    ("기타법인 5일 누적 순매수 > 0", 15, "자사주 매입 등 하방 지지 매수 주체입니다."),
    ("개인 5일 누적 순매도 (< 0)", 10, "개인 매도는 역발상 관점의 바닥 신호로 봅니다."),
    ("코스피 종가 > {ma}일 이동평균", 15, "추세 필터. 하락 추세에서의 매수를 걸러냅니다."),
    ("외국인 3일 연속 순매수", 10, "수급 모멘텀의 지속 여부를 확인합니다."),
]


def _streak(series: pd.Series) -> pd.Series:
    pos = (series > 0).astype(int)
    grp = (pos != pos.shift()).cumsum()
    return pos.groupby(grp).cumsum()


def build_signals(df: pd.DataFrame, short: int = 5, ma_window: int = 20) -> dict:
    d = df.copy()
    roll = {c: d[c].rolling(short, min_periods=1).sum() for c in INVESTORS}
    streak_f = _streak(d["외국인"])

    conds = pd.DataFrame(
        {
            "c1": roll["외국인"] > 0,
            "c2": roll["기관"] > 0,
            "c3": roll["기타법인"] > 0,
            "c4": roll["개인"] < 0,
            "c5": d["코스피지수"] > d["MA"],
            "c6": streak_f >= 3,
        }
    ).fillna(False)

    weights = [r[1] for r in RULES]
    score_series = sum(conds[f"c{i+1}"].astype(int) * w for i, w in enumerate(weights))
    score_series = pd.Series(score_series, index=d.index, name="수급스코어")

    last = d.index[-1]
    details = [
        fmt_eok(roll["외국인"].loc[last]) + " 억",
        fmt_eok(roll["기관"].loc[last]) + " 억",
        fmt_eok(roll["기타법인"].loc[last]) + " 억",
        fmt_eok(roll["개인"].loc[last]) + " 억",
        f"{d['코스피지수'].loc[last]:,.2f} vs MA {d['MA'].loc[last]:,.2f}"
        if pd.notna(d["MA"].loc[last]) else "MA 산출 불가",
        f"{int(streak_f.loc[last])}일 연속",
    ]
    triggers = [
        {
            "text": RULES[i][0].format(ma=ma_window),
            "weight": RULES[i][1],
            "why": RULES[i][2],
            "met": bool(conds[f"c{i+1}"].loc[last]),
            "detail": details[i],
        }
        for i in range(len(RULES))
    ]

    chg = d["코스피지수"].pct_change()
    corr = d["외국인"].tail(20).corr(chg.tail(20))

    return {
        "score": int(score_series.loc[last]),
        "score_series": score_series,
        "triggers": triggers,
        "rolling": {k: float(v.loc[last]) for k, v in roll.items()},
        "streak_foreign": int(streak_f.loc[last]),
        "corr_foreign_index": float(corr) if pd.notna(corr) else float("nan"),
        "short": short,
        "ma_window": ma_window,
    }


def allocation_guide(score: int, cash_ratio: int) -> dict:
    if score >= 85:
        band, deploy, verdict, tone = "적극 매수 구간", 0.60, "수급 4주체 정렬 + 추세 확인. 분할 매수 가속.", COLOR_BUY
    elif score >= 65:
        band, deploy, verdict, tone = "매수 우위 구간", 0.40, "핵심 주체 매수 우위. 계획된 분할 매수 진행.", COLOR_BUY
    elif score >= 45:
        band, deploy, verdict, tone = "중립 상단", 0.25, "1차 진입 가능. 미충족 트리거 확인 후 소량.", COLOR_ACCENT
    elif score >= 25:
        band, deploy, verdict, tone = "중립 하단", 0.10, "관찰 우위. 테스트 물량 수준으로 제한.", COLOR_ACCENT
    else:
        band, deploy, verdict, tone = "관망 구간", 0.0, "수급 이탈. 현금 비중 유지.", COLOR_SELL
    return {
        "band": band,
        "deploy_ratio": deploy,
        "total_weight": cash_ratio * deploy,
        "verdict": verdict,
        "tone": tone,
    }


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


def build_prompt(df: pd.DataFrame, sig: dict, guide: dict, cash_ratio: int) -> str:
    table = df.tail(15).copy()
    table.index = table.index.strftime("%Y-%m-%d")
    table = table[INVESTORS + ["코스피지수"]]
    checklist = "\n".join(
        f"- [{'충족' if t['met'] else '미충족'}] {t['text']} (가중치 {t['weight']}) → {t['detail']}"
        for t in sig["triggers"]
    )
    return f"""[분석 대상] 코스피 시장 투자자별 순매수 (단위: 억원)

[최근 {len(table)} 영업일 원본 데이터]
{table.to_string()}

[정량 지표 — 이미 계산된 값이므로 그대로 활용하고 재계산하지 마십시오]
- 5일 누적 순매수: 외국인 {sig['rolling']['외국인']:,.0f}억 / 기관 {sig['rolling']['기관']:,.0f}억 / 기타법인 {sig['rolling']['기타법인']:,.0f}억 / 개인 {sig['rolling']['개인']:,.0f}억
- 외국인 연속 순매수: {sig['streak_foreign']}일
- 외국인 순매수 ↔ 지수 등락률 20일 상관계수: {sig['corr_foreign_index']:.2f}
- 규칙 기반 수급 스코어: {sig['score']}/100 ({guide['band']})

[트리거 체크리스트]
{checklist}

[포트폴리오 상태]
- 현금 비중: 총 투자금의 {cash_ratio}%
- 규칙 엔진 권고: 보유 현금의 {guide['deploy_ratio']*100:.0f}% 투입 (전체 자산 대비 {guide['total_weight']:.0f}%)

[작성 지침]
아래 4개 섹션을 마크다운 소제목(##)으로 나누어 한국어로 작성하십시오. 각 섹션 4문장 이내.
## 1. 시장 국면 진단
수급 주체별 방향성과 지수 흐름의 정합성을 판단. 특히 기타법인의 하방 지지력을 평가.
## 2. 규칙 엔진에 대한 반론
스코어가 놓치고 있을 위험 요인 또는 과대평가 요소를 지적. 동의만 하지 말 것.
## 3. 현금 투입 트리거
관측 가능한 조건으로 서술. 수치와 기간을 명시.
## 4. 무효화 조건
이 시나리오를 폐기해야 하는 구체적 이탈 신호.

데이터에 없는 뉴스·이벤트를 지어내지 마십시오. 수치는 위 데이터에서만 인용하십시오."""


def stream_report(api_key: str, model: str, prompt: str):
    from groq import Groq

    log("LLM", "리포트 스트리밍 시작", 모델=model, 프롬프트길이=len(prompt))
    stream = Groq(api_key=api_key).chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 한국 주식시장 수급 데이터를 다루는 퀀트 전략가입니다. "
                    "제공된 수치만 근거로 삼고, 확신의 정도를 구분해 표현하며, "
                    "듣기 좋은 말보다 반증 가능한 조건을 제시합니다."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.15,
        max_tokens=1400,
        stream=True,
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

    groq_api_key = st.text_input(
        "Groq API Key", value=secret("GROQ_API_KEY"), type="password",
        help="Secrets 에 GROQ_API_KEY 를 넣어두면 자동으로 채워집니다.",
    )

    with st.expander("KRX 계정", expanded=not secret("KRX_ID")):
        st.caption("KRX 는 로그인하지 않은 요청을 거부합니다. data.krx.co.kr 계정이 필요합니다.")
        krx_id = st.text_input("KRX 아이디", value=secret("KRX_ID"))
        krx_pw = st.text_input("KRX 비밀번호", value=secret("KRX_PW"), type="password")

    lookback = st.slider("조회 기간 (영업일)", 10, 120, 40, step=5)
    ma_window = st.select_slider("추세 필터 이동평균", options=[5, 10, 20, 60], value=20)
    short = st.select_slider("누적 순매수 창", options=[3, 5, 10], value=5)
    cash_ratio = st.slider("현재 현금 비중 (%)", 0, 100, 70, step=5)
    model_choice = st.selectbox("LLM 모델", ["auto"] + PREFERRED_MODELS)

    st.divider()
    if st.button("데이터 새로고침", use_container_width=True):
        load_market.clear()
        st.session_state.pop("report", None)
        st.rerun()
    st.caption("데이터: KRX (pykrx) · 30분 캐시")

if krx_id:
    os.environ["KRX_ID"] = krx_id
if krx_pw:
    os.environ["KRX_PW"] = krx_pw

# ──────────────────────────────────────────────────────────────────────────────
# 헤더 + 로드
# ──────────────────────────────────────────────────────────────────────────────

st.markdown(
    f'<div class="cm-head"><h1>CENTURION Macro</h1>'
    f'<span class="cm-asof">코스피 투자자별 수급 · {now_kst():%Y-%m-%d %H:%M} KST</span></div>'
    f'<div class="cm-sub">개인·외국인·기관·기타법인 순매수 흐름과 규칙 기반 현금 투입 시그널</div>',
    unsafe_allow_html=True,
)

df: Optional[pd.DataFrame] = None
meta: dict = {}
load_error: Optional[BaseException] = None

with st.spinner("KRX 수급 데이터를 불러오는 중…"):
    try:
        df, meta = load_market(lookback, ma_window, krx_id)
    except Exception as e:  # noqa: BLE001
        load_error = e
        log_exc("치명", e, "데이터 수집 중단")

if load_error is not None:
    cause, action = classify_failure(load_error, _LOG)
    st.error(f"**{cause}**\n\n{action}")

    if not AUTH_STATE.get("성공"):
        st.info(
            "data.krx.co.kr 회원가입은 무료입니다. 가입 후 Streamlit Cloud 의 "
            "**Settings → Secrets** 에 아래를 추가하거나, 사이드바의 'KRX 계정'에 직접 입력하세요.\n\n"
            "```toml\nKRX_ID = \"아이디\"\nKRX_PW = \"비밀번호\"\n```",
            icon="🔑",
        )

    st.markdown("### 실행 로그")
    st.code(log_text() or "(로그 없음)", language="text")
    c1, c2 = st.columns([1, 3])
    c1.download_button(
        "로그 내려받기",
        data=log_text().encode("utf-8"),
        file_name=f"centurion_log_{now_kst():%Y%m%d_%H%M%S}.txt",
        use_container_width=True,
    )
    with st.expander("KRX 원본 응답 확인"):
        if st.button("KRX 서버에 직접 요청"):
            st.json(probe_krx(krx_id, krx_pw))
    with st.expander("환경 정보"):
        st.json(
            {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "streamlit": st.__version__,
                "pandas": pd.__version__,
                "KRX 인증": AUTH_STATE,
            }
        )
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
prev_close = df["코스피지수"].iloc[-2] if len(df) > 1 else df["코스피지수"].iloc[-1]
chg = df["코스피지수"].iloc[-1] - prev_close
chg_pct = (chg / prev_close * 100) if prev_close else 0.0

c = st.columns(5)
c[0].markdown(
    kpi("코스피 종가", f"{df['코스피지수'].iloc[-1]:,.2f}",
        f"{chg:+,.2f} ({chg_pct:+.2f}%) · {last:%m/%d}", tone_for(chg)),
    unsafe_allow_html=True,
)
for i, name in enumerate(["외국인", "기관", "기타법인", "개인"]):
    v = sig["rolling"][name]
    c[i + 1].markdown(
        kpi(f"{name} {short}일 누적", f"{fmt_eok(v)} 억",
            f"당일 {fmt_eok(df[name].iloc[-1])} 억", tone_for(v)),
        unsafe_allow_html=True,
    )

st.write("")

left, right = st.columns([1, 1.35])
with left:
    st.markdown(
        f'<div class="cm-score">'
        f'<div class="cm-band">수급 스코어 · {guide["band"]}</div>'
        f'<div class="cm-big" style="color:{guide["tone"]}">{sig["score"]}'
        f'<span style="font-size:1rem;color:#6E7787;font-weight:500"> / 100</span></div>'
        f'<div class="cm-verdict">{guide["verdict"]}</div>'
        f'<div class="cm-bar"><div style="width:{sig["score"]}%;background:{guide["tone"]}"></div></div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    d = st.columns(2)
    d[0].markdown(
        kpi("권고 투입 (보유 현금 대비)", f"{guide['deploy_ratio']*100:.0f}%",
            f"현금 {cash_ratio}% 중", guide["tone"]),
        unsafe_allow_html=True,
    )
    d[1].markdown(
        kpi("전체 자산 대비 신규 비중", f"{guide['total_weight']:.0f}%",
            f"잔여 현금 {cash_ratio - guide['total_weight']:.0f}%", guide["tone"]),
        unsafe_allow_html=True,
    )

with right:
    st.markdown("**트리거 체크리스트**")
    rows = []
    for t in sig["triggers"]:
        mark = "✅" if t["met"] else "⬜"
        color = COLOR_BUY if t["met"] else "#6E7787"
        rows.append(
            f'<div class="cm-trig"><div class="cm-mark">{mark}</div>'
            f'<div class="cm-text" style="color:{color}">{t["text"]}'
            f'<div class="cm-detail">{t["detail"]} · {t["why"]}</div></div>'
            f'<div class="cm-w">+{t["weight"]}</div></div>'
        )
    st.markdown("".join(rows), unsafe_allow_html=True)

st.write("")

# ──────────────────────────────────────────────────────────────────────────────
# 탭
# ──────────────────────────────────────────────────────────────────────────────

tab_dash, tab_detail, tab_ai, tab_diag = st.tabs(["차트", "수급 상세", "AI 리포트", "진단"])

with tab_dash:
    a, b = st.columns([1.2, 1])
    with a:
        st.markdown("**투자자별 일일 순매수 (억원)**")
        st.bar_chart(df[INVESTORS], color=SERIES_COLORS, height=300)
    with b:
        st.markdown(f"**코스피 지수 · {ma_window}일 이동평균**")
        st.line_chart(df[["코스피지수", "MA"]], color=["#E6EDF3", COLOR_ACCENT], height=300)
    a2, b2 = st.columns([1.2, 1])
    with a2:
        st.markdown("**누적 순매수 추이 (기간 내 누계, 억원)**")
        st.area_chart(df[INVESTORS].cumsum(), color=SERIES_COLORS, height=260)
    with b2:
        st.markdown("**수급 스코어 추이**")
        st.line_chart(sig["score_series"].to_frame(), color=[guide["tone"]], height=260)

with tab_detail:
    m = st.columns(3)
    m[0].metric("외국인 연속 순매수", f"{sig['streak_foreign']}일")
    corr = sig["corr_foreign_index"]
    m[1].metric("외국인↔지수 상관 (20일)", "—" if pd.isna(corr) else f"{corr:+.2f}")
    m[2].metric("조회 영업일", f"{len(df)}일")

    show = df.copy()
    show.index = show.index.strftime("%Y-%m-%d")
    show.index.name = "날짜"
    show = show[INVESTORS + ["코스피지수", "MA"]].rename(columns={"MA": f"MA{ma_window}"})
    st.dataframe(
        show.sort_index(ascending=False).style.format(
            {**{col: "{:+,.0f}" for col in INVESTORS},
             "코스피지수": "{:,.2f}", f"MA{ma_window}": "{:,.2f}"},
            na_rep="—",
        ),
        use_container_width=True,
        height=420,
    )
    st.download_button(
        "CSV 내려받기",
        data=show.to_csv().encode("utf-8-sig"),
        file_name=f"kospi_flow_{last:%Y%m%d}.csv",
        mime="text/csv",
    )

with tab_ai:
    if not groq_api_key:
        st.info("Groq API Key 를 입력하면 LLM 리포트가 추가됩니다.", icon="🔑")
    else:
        col_a, col_b = st.columns([1, 3])
        if col_a.button("리포트 생성", type="primary", use_container_width=True):
            model, available = resolve_groq_model(groq_api_key, model_choice)
            col_b.caption(f"모델: `{model}`" + (f" · {len(available)}개 사용 가능" if available else ""))
            try:
                text = st.write_stream(
                    stream_report(groq_api_key, model, build_prompt(df, sig, guide, cash_ratio))
                )
                st.session_state["report"] = {"text": text, "model": model, "at": now_kst()}
                log("LLM", "리포트 생성 완료", 길이=len(text))
            except Exception as e:  # noqa: BLE001
                log_exc("LLM", e, "리포트 생성 실패")
                st.error(f"리포트 생성 실패 — {type(e).__name__}: {e}")
        elif st.session_state.get("report"):
            r = st.session_state["report"]
            st.caption(f"모델: `{r['model']}` · 생성 {r['at']:%Y-%m-%d %H:%M} KST")
            st.markdown(r["text"])

with tab_diag:
    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**KRX 인증**")
        st.json(meta.get("인증", AUTH_STATE))
    with s2:
        st.markdown("**데이터 파이프라인**")
        st.json(
            {
                "조회경로": meta.get("조회경로"),
                "수집시각": meta.get("수집시각"),
                "행수": meta.get("행수"),
                "최근영업일": meta.get("최근영업일"),
                "경고": meta.get("경고") or "없음",
            }
        )

    st.markdown("**실행 로그**")
    entries = meta.get("로그") or _LOG
    lv = st.multiselect("레벨 필터", ["DEBUG", "INFO", "WARN", "ERROR"],
                        default=["INFO", "WARN", "ERROR"])
    shown = [e for e in entries if e["레벨"] in lv]
    if shown:
        st.dataframe(pd.DataFrame(shown), use_container_width=True, height=320)
    else:
        st.caption("표시할 로그가 없습니다.")
    st.download_button(
        "로그 내려받기",
        data=log_text(entries).encode("utf-8"),
        file_name=f"centurion_log_{now_kst():%Y%m%d_%H%M%S}.txt",
    )

    st.markdown("**KRX 원본 응답 확인**")
    st.caption("pykrx 와 동일한 요청을 직접 보내 서버 응답을 그대로 보여줍니다.")
    if st.button("KRX 서버에 직접 요청", key="probe_tab"):
        st.json(probe_krx(krx_id, krx_pw))

    with st.expander("환경 정보"):
        env = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "streamlit": st.__version__,
            "pandas": pd.__version__,
        }
        try:
            import pykrx
            env["pykrx"] = getattr(pykrx, "__version__", "unknown")
        except Exception as e:  # noqa: BLE001
            env["pykrx"] = f"import 실패: {e}"
        st.json(env)

st.divider()
st.markdown(
    '<div class="cm-disc">이 도구는 공개된 KRX 수급 데이터를 정리해 보여주는 참고 자료입니다. '
    "수급 스코어와 투입 비율은 화면에 표시된 고정 규칙의 산술 결과이며, 수익을 보장하지 않습니다. "
    "투자 판단과 그 결과에 대한 책임은 이용자 본인에게 있습니다.</div>",
    unsafe_allow_html=True,
)
