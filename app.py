"""
CENTURION Macro — 코스피 수급 대시보드
=====================================
개인 / 외국인 / 기관 / 기타법인 순매수 흐름을 추적하고,
규칙 기반 수급 스코어와 LLM 리포트로 현금 투입 타이밍을 점검합니다.

배포: Streamlit Community Cloud + GitHub
데이터: KRX (pykrx)
LLM: Groq (선택 사항 — 키가 없어도 규칙 기반 분석은 그대로 동작)
"""

from __future__ import annotations

import contextlib
import io
import os
import platform
import sys
import time
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

# 한국 시장 관례: 매수/상승 = 적색, 매도/하락 = 청색
COLOR_BUY = "#E5484D"
COLOR_SELL = "#3E7BFA"
COLOR_ACCENT = "#F5A524"
COLOR_MUTED = "#8A93A5"
SERIES_COLORS = ["#8A93A5", "#E5484D", "#3E7BFA", "#F5A524"]  # 개인/외국인/기관/기타법인

# Groq: llama-3.3-70b-versatile 은 2026-06-17 deprecated → 2026-08 decommission.
# 아래 순서대로 계정에서 사용 가능한 첫 모델을 자동 선택합니다.
PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k2-instruct-0905",
    "llama-3.3-70b-versatile",
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

CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard-dynamic-subset.css');

html, body, [class*="css"] {
  font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI',
               'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
}

/* 상단 여백 축소 */
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; }

/* 헤더 */
.cm-head { display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; }
.cm-head h1 { font-size: 1.65rem; font-weight: 700; letter-spacing: -.02em; margin: 0; }
.cm-head .cm-asof { color: #8A93A5; font-size: .85rem; font-variant-numeric: tabular-nums; }
.cm-sub { color: #8A93A5; font-size: .9rem; margin: .35rem 0 1.4rem 0; }

/* KPI 카드 */
.cm-kpi {
  border: 1px solid rgba(255,255,255,.08);
  border-left: 3px solid var(--tone, #8A93A5);
  border-radius: 10px;
  padding: .85rem 1rem .9rem 1rem;
  background: rgba(255,255,255,.025);
  height: 100%;
}
.cm-kpi .cm-label { font-size: .78rem; color: #8A93A5; margin-bottom: .3rem; }
.cm-kpi .cm-value {
  font-size: 1.5rem; font-weight: 700; line-height: 1.15;
  font-variant-numeric: tabular-nums; letter-spacing: -.02em;
  color: var(--tone, inherit);
}
.cm-kpi .cm-note { font-size: .76rem; color: #6E7787; margin-top: .3rem;
                   font-variant-numeric: tabular-nums; }

/* 스코어 배너 */
.cm-score {
  border: 1px solid rgba(255,255,255,.09);
  border-radius: 12px; padding: 1.1rem 1.3rem;
  background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.015));
}
.cm-score .cm-band { font-size: .8rem; color: #8A93A5; }
.cm-score .cm-big {
  font-size: 2.6rem; font-weight: 800; line-height: 1;
  font-variant-numeric: tabular-nums; letter-spacing: -.03em;
}
.cm-score .cm-verdict { font-size: 1.05rem; font-weight: 600; margin-top: .35rem; }
.cm-bar { height: 6px; border-radius: 3px; background: rgba(255,255,255,.08);
          margin-top: .8rem; overflow: hidden; }
.cm-bar > div { height: 100%; border-radius: 3px; }

/* 트리거 체크리스트 */
.cm-trig { display: flex; gap: .6rem; align-items: flex-start; padding: .45rem 0;
           border-bottom: 1px dashed rgba(255,255,255,.07); }
.cm-trig:last-child { border-bottom: none; }
.cm-trig .cm-mark { width: 1.2rem; flex: none; }
.cm-trig .cm-text { flex: 1; font-size: .9rem; }
.cm-trig .cm-w { color: #6E7787; font-size: .8rem; font-variant-numeric: tabular-nums; }
.cm-trig .cm-detail { color: #8A93A5; font-size: .8rem; font-variant-numeric: tabular-nums; }

.cm-disc { color: #6E7787; font-size: .78rem; line-height: 1.6; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────────

def now_kst() -> datetime:
    return datetime.now(KST)


def fmt_eok(v: float, signed: bool = True) -> str:
    """억원 단위 포맷."""
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
        f'<div class="cm-note">{note}</div>'
        f"</div>"
    )


_PYKRX_LOG: list[str] = []


@contextlib.contextmanager
def capture_pykrx_log():
    """
    pykrx 는 내부에서 예외를 삼키고 빈 DataFrame 을 돌려주면서
    'Error occurred in ...' 를 stdout 으로만 흘립니다. 그 문구를 붙잡아 둡니다.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        text = buf.getvalue().strip()
        if text:
            _PYKRX_LOG.extend(line for line in text.splitlines() if line.strip())


def _pick(cols: Iterable, *candidates: str) -> Optional[str]:
    """정확 일치 → 부분 일치 순으로 컬럼명을 찾습니다 (pykrx 버전별 컬럼명 차이 흡수)."""
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


def _retry(fn: Callable, tries: int = 3, delay: float = 0.8):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# pykrx 지연 임포트
# ──────────────────────────────────────────────────────────────────────────────

_STOCK = None


def get_stock_api():
    """
    pykrx.stock 을 지연 임포트합니다.

    - Secrets 에 KRX_ID / KRX_PW 가 있으면 환경변수로 주입해 인증 세션을 씁니다.
      (없으면 pykrx 가 익명 세션으로 자동 폴백하므로 필수는 아닙니다.)
    - pykrx 가 임포트 시점에 출력하는 로그인 안내문을 화면에서 가립니다.
    """
    global _STOCK
    if _STOCK is not None:
        return _STOCK
    try:
        for key in ("KRX_ID", "KRX_PW"):
            val = st.secrets.get(key, "")
            if val and not os.getenv(key):
                os.environ[key] = str(val)
    except Exception:  # noqa: BLE001
        pass
    with contextlib.redirect_stdout(io.StringIO()):
        from pykrx import stock as _s
    _STOCK = _s
    return _STOCK


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 레이어
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_index(start: str, end: str) -> pd.DataFrame:
    """코스피 지수 OHLCV. pykrx 버전에 따라 함수명이 달라 두 가지를 모두 시도."""
    stock = get_stock_api()

    for name in ("get_index_ohlcv", "get_index_ohlcv_by_date"):
        fn = getattr(stock, name, None)
        if fn is None:
            continue
        try:
            with capture_pykrx_log():
                df = _retry(lambda: fn(start, end, KOSPI_INDEX_TICKER))
        except Exception as e:  # noqa: BLE001
            _PYKRX_LOG.append(f"{name} 예외: {type(e).__name__}: {e}")
            continue
        if df is not None and not df.empty:
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            close_col = _pick(df.columns, "종가", "close")
            if close_col is None:
                continue
            out = pd.DataFrame({"코스피지수": pd.to_numeric(df[close_col], errors="coerce")})
            return out.dropna()
    return pd.DataFrame(columns=["코스피지수"])


def _normalize_flow(raw: pd.DataFrame) -> pd.DataFrame:
    """투자자별 순매수(원) → 억원, 컬럼명 표준화."""
    df = raw.copy()
    df.index = pd.to_datetime(df.index)
    cols = list(df.columns)
    mapping = {
        "개인": _pick(cols, "개인"),
        "외국인": _pick(cols, "외국인합계", "외국인"),
        "기관": _pick(cols, "기관합계", "기관"),
        "기타법인": _pick(cols, "기타법인"),
    }
    out = pd.DataFrame(index=df.index)
    for key, col in mapping.items():
        out[key] = pd.to_numeric(df[col], errors="coerce") if col is not None else 0.0
    return (out / 1e8).round(0)


def _fetch_flow_by_date(start: str, end: str) -> pd.DataFrame:
    """1회 호출로 기간 전체 수급을 가져옵니다 (권장 경로)."""
    stock = get_stock_api()

    fn = getattr(stock, "get_market_trading_value_by_date", None)
    if fn is None:
        raise AttributeError("get_market_trading_value_by_date 없음")
    with capture_pykrx_log():
        df = _retry(lambda: fn(start, end, "KOSPI"))
    if df is None or df.empty:
        raise ValueError("빈 응답")
    cols = list(df.columns)
    if _pick(cols, "개인") is None or _pick(cols, "외국인합계", "외국인") is None:
        raise ValueError(f"예상한 투자자 컬럼이 없습니다: {cols}")
    return _normalize_flow(df)


def _fetch_flow_loop(dates: Iterable[pd.Timestamp]) -> pd.DataFrame:
    """폴백: 영업일별로 하루씩 조회."""
    stock = get_stock_api()

    rows = {}
    for d in dates:
        ds = pd.Timestamp(d).strftime("%Y%m%d")
        try:
            with capture_pykrx_log():
                df = _retry(
                    lambda: stock.get_market_trading_value_by_investor(ds, ds, "KOSPI"), tries=2
                )
        except Exception:  # noqa: BLE001
            continue
        col = _pick(df.columns, "순매수")
        if col is None:
            continue
        rows[pd.Timestamp(d)] = df[col]
    if not rows:
        raise RuntimeError("일자별 조회 실패")
    return _normalize_flow(pd.DataFrame(rows).T)


@st.cache_data(ttl=1800, show_spinner=False)
def load_market(lookback: int, ma_window: int) -> tuple[pd.DataFrame, dict]:
    """
    수급 + 지수 데이터를 병합해 반환.
    이동평균 계산을 위해 지수는 더 긴 구간을 조회한 뒤 마지막 lookback 일만 남깁니다.
    """
    meta: dict = {"path": None, "warnings": []}
    today = now_kst()
    # 영업일 확보를 위해 넉넉히 조회 (주말·공휴일 감안)
    flow_start = (today - timedelta(days=int(lookback * 2.2) + 20)).strftime("%Y%m%d")
    index_start = (today - timedelta(days=int((lookback + ma_window) * 2.2) + 40)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    _PYKRX_LOG.clear()
    idx = _fetch_index(index_start, end)
    if idx.empty:
        detail = " | ".join(_PYKRX_LOG[:6]) or "pykrx 가 아무 메시지도 남기지 않았습니다"
        meta["pykrx_log"] = list(_PYKRX_LOG)
        raise RuntimeError(
            f"코스피 지수 응답이 비어 있습니다 (조회 {index_start}~{end}). pykrx 내부 메시지: {detail}"
        )

    idx["MA"] = idx["코스피지수"].rolling(ma_window, min_periods=max(2, ma_window // 2)).mean()

    try:
        flow = _fetch_flow_by_date(flow_start, end)
        meta["path"] = "get_market_trading_value_by_date (일괄)"
    except Exception as e:  # noqa: BLE001
        meta["warnings"].append(f"일괄 조회 실패 → 일자별 조회로 전환 ({type(e).__name__}: {e})")
        flow = _fetch_flow_loop(idx.index[-(lookback + 5):])
        meta["path"] = "get_market_trading_value_by_investor (일자별 폴백)"

    df = flow.join(idx, how="inner").sort_index()
    df = df[df[INVESTORS].abs().sum(axis=1) > 0]  # 휴장·미집계일 제거
    if df.empty:
        raise RuntimeError("수급·지수 데이터 병합 결과가 비어 있습니다.")

    df = df.tail(lookback)
    meta["pykrx_log"] = list(_PYKRX_LOG)
    meta["rows"] = len(df)
    meta["fetched_at"] = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
    return df, meta



def probe_krx() -> dict:
    """
    pykrx 와 동일한 요청을 직접 보내 KRX 가 무엇을 돌려주는지 확인합니다.
    JSON 이 오면 API 는 살아있는 것이고, HTML/403 이면 접근이 차단된 것입니다.
    """
    import requests

    url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
        "X-Requested-With": "XMLHttpRequest",
    }
    end = now_kst().strftime("%Y%m%d")
    start = (now_kst() - timedelta(days=14)).strftime("%Y%m%d")
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT00301",
        "tboxindIdx_finder_equidx0_0": "코스피",
        "indIdx": "1",
        "indIdx2": "001",
        "codeNmindIdx_finder_equidx0_0": "코스피",
        "strtDd": start,
        "endDd": end,
        "share": "2",
        "money": "3",
        "csvxls_isNo": "false",
    }
    out: dict = {"요청": f"{start}~{end}"}
    try:
        r = requests.post(url, headers=headers, data=payload, timeout=20)
        out["HTTP 상태"] = r.status_code
        out["Content-Type"] = r.headers.get("Content-Type", "?")
        out["응답 길이"] = len(r.content)
        body = r.text[:300]
        out["응답 앞부분"] = body
        try:
            js = r.json()
            key = next((k for k in js if isinstance(js[k], list)), None)
            out["JSON 파싱"] = "성공"
            out["데이터 행 수"] = len(js[key]) if key else 0
            out["판정"] = (
                "정상 — KRX 가 데이터를 반환했습니다"
                if key and js[key]
                else "JSON 은 왔지만 데이터가 비어 있습니다 (파라미터 또는 기간 문제)"
            )
        except Exception:  # noqa: BLE001
            out["JSON 파싱"] = "실패"
            out["판정"] = (
                "HTML 이 돌아왔습니다 — KRX 가 이 서버의 접근을 차단했을 가능성이 높습니다"
                if "<html" in body.lower() or "<!doctype" in body.lower()
                else "예상치 못한 응답 형식입니다"
            )
    except Exception as e:  # noqa: BLE001
        out["판정"] = f"요청 자체 실패 — {type(e).__name__}: {e}"
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 시그널 엔진 (규칙 기반)
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
    """양수 연속 일수."""
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

    # 외국인 순매수와 지수 등락률의 20일 상관계수
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
        band, deploy, verdict = "적극 매수 구간", 0.60, "수급 4주체 정렬 + 추세 확인. 분할 매수 가속."
        tone = COLOR_BUY
    elif score >= 65:
        band, deploy, verdict = "매수 우위 구간", 0.40, "핵심 주체 매수 우위. 계획된 분할 매수 진행."
        tone = COLOR_BUY
    elif score >= 45:
        band, deploy, verdict = "중립 상단", 0.25, "1차 진입 가능. 트리거 미충족 항목 확인 후 소량."
        tone = COLOR_ACCENT
    elif score >= 25:
        band, deploy, verdict = "중립 하단", 0.10, "관찰 우위. 테스트 물량 수준으로 제한."
        tone = COLOR_ACCENT
    else:
        band, deploy, verdict = "관망 구간", 0.0, "수급 이탈. 현금 비중 유지."
        tone = COLOR_SELL

    return {
        "band": band,
        "deploy_ratio": deploy,                     # 보유 현금 중 투입 비율
        "total_weight": cash_ratio * deploy,        # 전체 자산 대비 투입 비중(%p)
        "verdict": verdict,
        "tone": tone,
    }


# ──────────────────────────────────────────────────────────────────────────────
# LLM 레이어
# ──────────────────────────────────────────────────────────────────────────────

def resolve_groq_model(api_key: str, wanted: str = "auto") -> tuple[str, list[str]]:
    """계정에서 실제 사용 가능한 모델을 조회해 선택합니다."""
    from groq import Groq

    available: list[str] = []
    try:
        client = Groq(api_key=api_key)
        available = sorted(m.id for m in client.models.list().data)
    except Exception:  # noqa: BLE001
        pass

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
관측 가능한 조건으로 서술 (예: "외국인 3일 누적 순매수 +5,000억 돌파 시"). 수치와 기간을 명시.
## 4. 무효화 조건
이 시나리오를 폐기해야 하는 구체적 이탈 신호.

데이터에 없는 뉴스·이벤트를 지어내지 마십시오. 수치는 위 데이터에서만 인용하십시오."""


def stream_report(api_key: str, model: str, prompt: str):
    from groq import Groq

    client = Groq(api_key=api_key)
    stream = client.chat.completions.create(
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

    secret_key = ""
    try:
        secret_key = st.secrets.get("GROQ_API_KEY", "")
    except Exception:  # noqa: BLE001
        secret_key = ""

    groq_api_key = st.text_input(
        "Groq API Key",
        value=secret_key,
        type="password",
        help="비워두면 규칙 기반 분석만 실행됩니다. Streamlit Cloud의 Secrets에 "
             "GROQ_API_KEY 를 넣어두면 매번 입력하지 않아도 됩니다.",
    )

    lookback = st.slider("조회 기간 (영업일)", 10, 120, 40, step=5)
    ma_window = st.select_slider("추세 필터 이동평균", options=[5, 10, 20, 60], value=20)
    short = st.select_slider("누적 순매수 창", options=[3, 5, 10], value=5)
    cash_ratio = st.slider("현재 현금 비중 (%)", 0, 100, 70, step=5)

    model_choice = st.selectbox(
        "LLM 모델",
        ["auto"] + PREFERRED_MODELS,
        help="auto 는 계정에서 사용 가능한 모델을 조회해 자동 선택합니다.",
    )

    st.divider()
    if st.button("데이터 새로고침", use_container_width=True):
        load_market.clear()
        st.session_state.pop("report", None)
        st.rerun()

    st.caption("데이터: KRX (pykrx) · 30분 캐시")


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 로드
# ──────────────────────────────────────────────────────────────────────────────

st.markdown(
    f'<div class="cm-head"><h1>CENTURION Macro</h1>'
    f'<span class="cm-asof">코스피 투자자별 수급 · {now_kst():%Y-%m-%d %H:%M} KST</span></div>'
    f'<div class="cm-sub">개인·외국인·기관·기타법인 순매수 흐름과 규칙 기반 현금 투입 시그널</div>',
    unsafe_allow_html=True,
)

df: Optional[pd.DataFrame] = None
meta: dict = {}
load_error: Optional[Exception] = None

with st.spinner("KRX 수급 데이터를 불러오는 중…"):
    try:
        df, meta = load_market(lookback, ma_window)
    except Exception as e:  # noqa: BLE001
        load_error = e

if load_error is not None:
    msg = str(load_error)
    if isinstance(load_error, ModuleNotFoundError) and "pkg_resources" in msg:
        hint = (
            "구버전 pykrx 가 설치됐습니다. setuptools 82 부터 pkg_resources 가 제거되어 "
            "임포트가 실패합니다. requirements.txt 에 `pykrx>=1.2.8` 과 `pandas>=2.3.3,<3` 을 "
            "지정하세요."
        )
    elif isinstance(load_error, ModuleNotFoundError):
        hint = "의존성이 설치되지 않았습니다. requirements.txt 와 빌드 로그를 확인하세요."
    else:
        hint = (
            "KRX 서버 응답 지연이거나 pykrx 응답 형식 변경일 수 있습니다. "
            "사이드바의 **데이터 새로고침**을 눌러 보세요."
        )
    st.error(f"데이터를 불러오지 못했습니다 — {type(load_error).__name__}: {msg}\n\n{hint}")
    if _PYKRX_LOG:
        st.markdown("**pykrx 내부 메시지**")
        st.code("\n".join(_PYKRX_LOG[:12]))

    st.markdown("**KRX 연결 진단**")
    if st.button("KRX 서버에 직접 요청해 보기"):
        st.json(probe_krx())

    with st.expander("환경 정보"):
        st.write({"python": sys.version, "platform": platform.platform()})
        st.write({"pandas": pd.__version__, "streamlit": st.__version__})
        try:
            import pykrx  # noqa: F401
            st.write({"pykrx": getattr(pykrx, "__version__", "unknown")})
        except Exception as e:  # noqa: BLE001
            st.write({"pykrx import 실패": str(e)})
    st.stop()

assert df is not None
sig = build_signals(df, short=short, ma_window=ma_window)
guide = allocation_guide(sig["score"], cash_ratio)

for w in meta.get("warnings", []):
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
    kpi(
        "코스피 종가",
        f"{df['코스피지수'].iloc[-1]:,.2f}",
        f"{chg:+,.2f} ({chg_pct:+.2f}%) · {last:%m/%d}",
        tone_for(chg),
    ),
    unsafe_allow_html=True,
)
for i, name in enumerate(["외국인", "기관", "기타법인", "개인"]):
    v = sig["rolling"][name]
    today_v = df[name].iloc[-1]
    c[i + 1].markdown(
        kpi(
            f"{name} {short}일 누적",
            f"{fmt_eok(v)} 억",
            f"당일 {fmt_eok(today_v)} 억",
            tone_for(v),
        ),
        unsafe_allow_html=True,
    )

st.write("")

# ──────────────────────────────────────────────────────────────────────────────
# 스코어 + 트리거
# ──────────────────────────────────────────────────────────────────────────────

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
        kpi(
            "권고 투입 (보유 현금 대비)",
            f"{guide['deploy_ratio']*100:.0f}%",
            f"현금 {cash_ratio}% 중",
            guide["tone"],
        ),
        unsafe_allow_html=True,
    )
    d[1].markdown(
        kpi(
            "전체 자산 대비 신규 비중",
            f"{guide['total_weight']:.0f}%",
            f"잔여 현금 {cash_ratio - guide['total_weight']:.0f}%",
            guide["tone"],
        ),
        unsafe_allow_html=True,
    )

with right:
    st.markdown("**트리거 체크리스트**")
    rows = []
    for t in sig["triggers"]:
        mark = "✅" if t["met"] else "⬜"
        color = COLOR_BUY if t["met"] else "#6E7787"
        rows.append(
            f'<div class="cm-trig">'
            f'<div class="cm-mark">{mark}</div>'
            f'<div class="cm-text" style="color:{color}">{t["text"]}'
            f'<div class="cm-detail">{t["detail"]} · {t["why"]}</div></div>'
            f'<div class="cm-w">+{t["weight"]}</div>'
            f"</div>"
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
            {**{c: "{:+,.0f}" for c in INVESTORS},
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
        st.info(
            "Groq API Key 를 입력하면 LLM 리포트가 추가됩니다. "
            "키가 없어도 위의 스코어·트리거는 그대로 동작합니다.",
            icon="🔑",
        )
    else:
        col_a, col_b = st.columns([1, 3])
        run = col_a.button("리포트 생성", type="primary", use_container_width=True)

        if run:
            model, available = resolve_groq_model(groq_api_key, model_choice)
            col_b.caption(
                f"모델: `{model}`"
                + (f" · 계정에서 {len(available)}개 모델 사용 가능" if available else "")
            )
            prompt = build_prompt(df, sig, guide, cash_ratio)
            try:
                text = st.write_stream(stream_report(groq_api_key, model, prompt))
                st.session_state["report"] = {"text": text, "model": model, "at": now_kst()}
            except Exception as e:  # noqa: BLE001
                st.error(
                    f"리포트 생성 실패 — {type(e).__name__}: {e}\n\n"
                    "모델이 폐기되었거나 키 권한 문제일 수 있습니다. "
                    "사이드바에서 다른 모델을 선택해 보세요."
                )
        elif st.session_state.get("report"):
            r = st.session_state["report"]
            st.caption(f"모델: `{r['model']}` · 생성 {r['at']:%Y-%m-%d %H:%M} KST")
            st.markdown(r["text"])

with tab_diag:
    st.markdown("**실행 환경**")
    st.json(
        {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "streamlit": st.__version__,
            "pandas": pd.__version__,
            "서버 시각(KST)": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    st.markdown("**데이터 파이프라인**")
    st.json(
        {
            "조회 경로": meta.get("path"),
            "수집 시각": meta.get("fetched_at"),
            "행 수": meta.get("rows"),
            "최근 영업일": f"{last:%Y-%m-%d}",
            "경고": meta.get("warnings") or "없음",
        }
    )
    try:
        import pykrx  # noqa: F401
        st.write("pykrx 버전:", getattr(pykrx, "__version__", "unknown"))
    except Exception as e:  # noqa: BLE001
        st.write("pykrx import 실패:", str(e))

    if meta.get("pykrx_log"):
        st.markdown("**pykrx 내부 메시지**")
        st.code("\n".join(meta["pykrx_log"][:12]))

    st.markdown("**KRX 연결 진단**")
    st.caption("pykrx 와 동일한 요청을 직접 보내 서버 응답을 그대로 보여줍니다.")
    if st.button("KRX 서버에 직접 요청해 보기", key="probe_tab"):
        st.json(probe_krx())

st.divider()
st.markdown(
    '<div class="cm-disc">이 도구는 공개된 KRX 수급 데이터를 정리해 보여주는 참고 자료입니다. '
    "수급 스코어와 투입 비율은 위 화면에 표시된 고정 규칙의 산술 결과이며, 수익을 보장하지 않습니다. "
    "투자 판단과 그 결과에 대한 책임은 이용자 본인에게 있습니다.</div>",
    unsafe_allow_html=True,
)
