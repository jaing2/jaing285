import streamlit as st
import pandas as pd
from pykrx import stock
from datetime import datetime, timedelta
from groq import Groq

st.set_page_config(page_title="CENTURION Macro Dashboard", layout="wide")

st.title("CENTURION Macro: 코스피 수급 분석기 📊")
st.markdown("개인/외국인/기관/기타법인 수급 흐름 모니터링 및 AI 현금 투입 시그널")

# 사이드바 설정
with st.sidebar:
    st.header("설정 (Settings)")
    groq_api_key = st.text_input("Groq API Key", type="password")
    lookback_days = st.slider("조회 기간 (영업일)", 5, 30, 10)
    analyze_button = st.button("데이터 수집 및 AI 분석 실행")

# 데이터 수집 함수 (Streamlit 캐싱 적용으로 재로딩 방지)
@st.cache_data(ttl=3600)
def get_market_flow(days):
    today = datetime.today()
    start_date = (today - timedelta(days=days*2)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    b_days = stock.get_business_days_dates(start_date, end_date)[-days:]
    
    flow_data = []
    for d in b_days:
        d_str = d.strftime("%Y%m%d")
        df = stock.get_market_trading_value_by_investor(d_str, d_str, "KOSPI")
        net_buy = df['순매수'] / 100000000 # 억 단위 변환
        
        flow_data.append({
            "날짜": d.strftime("%Y-%m-%d"),
            "개인": round(net_buy.get("개인", 0), 0),
            "외국인": round(net_buy.get("외국인", 0), 0),
            "기관": round(net_buy.get("기관합계", 0), 0),
            "기타법인": round(net_buy.get("기타법인", 0), 0),
            "코스피지수": stock.get_index_ohlcv(d_str, d_str, "1001")['종가'].iloc[0]
        })
    return pd.DataFrame(flow_data)

def analyze_regime_with_llm(df_flow, api_key):
    client = Groq(api_key=api_key)
    flow_text = df_flow.to_string(index=False)
    prompt = f"""
    당신은 코스피 시장을 분석하는 퀀트 전략가입니다.
    아래는 최근 코스피 시장의 일자별 투자자 순매수 금액(단위: 억원)과 지수 흐름입니다.
    현재 사용자는 전체 투자금의 70%를 현금으로 보유 중입니다.

    [최근 수급 데이터]
    {flow_text}

    이 데이터를 바탕으로 다음 사항을 냉철하게 분석하여 리포트를 작성해주세요.
    1. 현재 시장 국면 요약 (기타법인 하방 지지력 평가)
    2. 보유한 70% 현금 투입을 위한 구체적인 '트리거(매수 신호)' 조건
    """
    
    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": "데이터 기반으로 매크로 장세를 분석하는 AI 퀀트 에이전트입니다. 객관적이고 단호하게 작성하세요."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        max_tokens=600
    )
    return completion.choices[0].message.content.strip()

# 메인 실행 로직
if analyze_button:
    if not groq_api_key:
        st.error("좌측 사이드바에 Groq API Key를 입력해주세요.")
    else:
        with st.spinner("한국거래소(KRX) 수급 데이터를 가져오는 중..."):
            df_flow = get_market_flow(lookback_days)
            
        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.subheader("투자기관별 순매수 추이 (억원)")
            chart_data = df_flow.set_index("날짜")[["개인", "외국인", "기관", "기타법인"]]
            st.bar_chart(chart_data)
            
        with col2:
            st.subheader("코스피 지수 추이")
            index_data = df_flow.set_index("날짜")[["코스피지수"]]
            st.line_chart(index_data)

        st.subheader("최근 수급 Raw Data")
        st.dataframe(df_flow, use_container_width=True)

        with st.spinner("Llama-3.3-70b 모델이 시장 국면을 분석하는 중..."):
            try:
                report = analyze_regime_with_llm(df_flow, groq_api_key)
                st.subheader("🤖 AI 매크로 전략 리포트")
                st.info(report)
            except Exception as e:
                st.error(f"AI 분석 중 오류가 발생했습니다: {e}")
