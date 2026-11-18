# -*- coding: utf-8 -*-
# 제약 BD/전략 공고 다국가 검색 + 이력서 매칭 순위 웹앱 (Streamlit)
# 실행(로컬 테스트): streamlit run app.py

import io                 # 업로드된 PDF를 메모리에서 읽을 때 사용
import json               # AI 답변(JSON 텍스트)을 딕셔너리로 변환
import re                 # 공고 본문에서 "5+ years" 패턴 찾기
import time               # 검색 사이 쉬는 시간

import anthropic          # Claude API
import pandas as pd       # 표 처리
import streamlit as st    # 웹 UI
from jobspy import scrape_jobs   # LinkedIn/Indeed 공고 수집

# ---------------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------------
st.set_page_config(page_title="제약 BD 잡 파인더", page_icon="💊", layout="wide")

# 화면에 보일 나라 이름 -> (검색용 지역 이름, Indeed 국가 코드)
COUNTRY_OPTIONS = {
    "🇯🇵 일본": ("Japan", "japan"),
    "🇸🇬 싱가포르": ("Singapore", "singapore"),
    "🇨🇭 스위스": ("Switzerland", "switzerland"),
    "🇺🇸 미국": ("United States", "usa"),
    "🇬🇧 영국": ("United Kingdom", "uk"),
    "🇩🇪 독일": ("Germany", "germany"),
    "🇨🇦 캐나다": ("Canada", "canada"),
    "🇰🇷 한국": ("South Korea", "south korea"),
    "🇭🇰 홍콩": ("Hong Kong", "hong kong"),
    "🇦🇺 호주": ("Australia", "australia"),
    "🇳🇱 네덜란드": ("Netherlands", "netherlands"),
    "🇫🇷 프랑스": ("France", "france"),
    "🇩🇰 덴마크": ("Denmark", "denmark"),
    "🇧🇪 벨기에": ("Belgium", "belgium"),
}

DEFAULT_TERMS = (
    "pharmaceutical business development licensing\n"
    "pharma strategy business analyst\n"
    "search and evaluation pharma\n"
    "biotech asset valuation\n"
    "事業開発 製薬"
)

TOO_JUNIOR = ["intern", "internship", "graduate", "trainee", "junior", "entry level", "assistant", "インターン", "新卒"]
TOO_SENIOR = ["vice president", "vp,", "vp ", "svp", "evp", "chief", "head of", "executive director", "本部長"]
MODEL = "claude-haiku-5-5"   # 저렴하고 빠른 모델


# ---------------------------------------------------------------
# 간단한 비밀번호 (API 비용을 아무나 쓰지 못하게)
# ---------------------------------------------------------------
def check_password():
    pw = st.secrets.get("APP_PASSWORD", "")          # 배포 설정(Secrets)에 저장한 비밀번호
    if not pw:                                        # 비밀번호를 안 정했으면 그냥 통과
        return True
    if st.session_state.get("authed"):               # 이미 로그인했으면 통과
        return True
    st.title("💊 제약 BD 잡 파인더")
    entered = st.text_input("비밀번호", type="password")
    if st.button("들어가기"):
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()                                # 화면 새로고침
        else:
            st.error("비밀번호가 틀렸어요")
    return False


# ---------------------------------------------------------------
# 이력서 읽기 (PDF 업로드 or 붙여넣기)
# ---------------------------------------------------------------
def read_resume(uploaded, pasted):
    if uploaded is not None:
        if uploaded.name.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(uploaded.read()))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        return uploaded.read().decode("utf-8", errors="ignore")
    return pasted or ""


# ---------------------------------------------------------------
# 직급 필터
# ---------------------------------------------------------------
def required_years(text):
    if not isinstance(text, str):
        return None
    found = re.findall(r"(\d{1,2})\s*(?:\+|-\s*\d{1,2}|〜\s*\d{1,2})?\s*(?:years|yrs|年以上)", text.lower())
    nums = [int(n) for n in found if 0 < int(n) < 30]
    return min(nums) if nums else None


# 채점 전에 공짜로 매기는 관련도 점수: 형 직무 키워드가 제목/본문에 얼마나 들어있는지
KEYWORDS_TITLE = ["business development", "licensing", "search & evaluation", "search and evaluation",
                  "strategy", "strategic", "valuation", "portfolio", "alliance", "transaction",
                  "corporate development", "business analyst", "bd", "事業開発", "ライセンス", "経営企画"]
KEYWORDS_BODY = ["pharma", "biotech", "licensing", "in-licens", "valuation", "npv", "rnpv", "due diligence",
                 "phase i", "phase 1", "phase ii", "phase 2", "asset", "deal", "royalt", "forecast", "製薬"]


def pre_score(row):
    title = str(row.get("title", "")).lower()
    body = str(row.get("description", "")).lower()
    s = 3 * sum(k in title for k in KEYWORDS_TITLE)    # 제목에 있으면 3점씩
    s += sum(k in body for k in KEYWORDS_BODY)          # 본문에 있으면 1점씩
    return s


def seniority_ok(row, min_y, max_y):
    title = str(row.get("title", "")).lower()
    if any(w in title for w in TOO_JUNIOR) or any(w in title for w in TOO_SENIOR):
        return False
    yrs = required_years(row.get("description"))
    return yrs is None or (min_y <= yrs <= max_y)


# ---------------------------------------------------------------
# AI 채점
# ---------------------------------------------------------------
PROMPT = """You are a pharma industry recruiter. Compare the candidate's resume with the job posting.

Return ONLY a JSON object:
{{"score": <0-100 integer>, "years_required": "<what the posting asks for, short>", "reason": "<1-2 short sentences in Korean: main fit and main gap>"}}

Score guide: 85+ strong fit, 70-84 good, 50-69 partial, <50 weak.
Weigh pharma BD/licensing/valuation/strategy experience, years, seniority, and language/visa/location requirements.

=== RESUME ===
{resume}

=== JOB: {title} at {company} ({location}) ===
{description}
"""


def score_one(client, resume, row):
    prompt = PROMPT.format(
        resume=resume[:8000], title=row.get("title"), company=row.get("company"),
        location=row.get("location"), description=str(row.get("description", ""))[:6000],
    )
    msg = client.messages.create(model=MODEL, max_tokens=300,
                                 messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return int(data.get("score", 0)), data.get("years_required", ""), data.get("reason", "")


# ---------------------------------------------------------------
# 화면
# ---------------------------------------------------------------
if not check_password():
    st.stop()

st.title("💊 제약 BD 잡 파인더")
st.caption("여러 나라 공고를 한 번에 모아서, 내 이력서와 잘 맞는 순서대로 보여줘요.")

with st.sidebar:
    st.header("① 이력서")
    up = st.file_uploader("PDF나 TXT 파일 올리기", type=["pdf", "txt"])
    pasted = st.text_area("또는 여기에 붙여넣기", height=150)

    st.header("② 나라")
    countries = st.multiselect("검색할 나라", list(COUNTRY_OPTIONS),
                               default=["🇯🇵 일본", "🇸🇬 싱가포르", "🇨🇭 스위스", "🇺🇸 미국"])

    st.header("③ 검색어")
    terms_text = st.text_area("한 줄에 하나씩", DEFAULT_TERMS, height=140)

    st.header("④ 경력 범위")
    min_y, max_y = st.slider("공고가 요구하는 경력(년)", 0, 20, (3, 12))

    st.header("⑤ 기타")
    days = st.selectbox("최근 며칠 공고", [7, 14, 30, 60], index=2)
    per = st.select_slider("검색 1번당 공고 수", [10, 20, 30, 50], value=20)
    max_score = st.select_slider("AI 채점 최대 개수 (비용 조절)", [30, 50, 100, 200, 300], value=100)

    go = st.button("🔍 검색 시작", type="primary", width="stretch")

if go:
    resume = read_resume(up, pasted)
    terms = [t.strip() for t in terms_text.splitlines() if t.strip()]
    if len(resume.strip()) < 100:
        st.error("이력서를 먼저 올리거나 붙여넣어 주세요.")
        st.stop()
    if not countries or not terms:
        st.error("나라와 검색어를 하나 이상 골라주세요.")
        st.stop()

    # 1) 수집
    frames, total = [], len(countries) * len(terms)
    bar = st.progress(0.0, text="공고 모으는 중...")
    for i, (label, term) in enumerate([(c, t) for c in countries for t in terms]):
        loc, code = COUNTRY_OPTIONS[label]
        bar.progress(i / total, text=f"공고 모으는 중... {label} / {term}")
        try:
            df = scrape_jobs(site_name=["linkedin", "indeed"], search_term=term, location=loc,
                             country_indeed=code, results_wanted=per, hours_old=days * 24,
                             linkedin_fetch_description=True, verbose=0)
            df["country"] = label
            frames.append(df)
        except Exception as e:
            st.toast(f"{label} / {term} 검색 실패: {str(e)[:80]}")
        time.sleep(3)
    bar.progress(1.0, text="수집 완료")

    if not frames or sum(len(f) for f in frames) == 0:
        st.error("공고를 하나도 못 가져왔어요. 잠시 후 다시 시도해 주세요.")
        st.stop()

    jobs = pd.concat(frames, ignore_index=True)
    jobs = jobs.drop_duplicates(subset=["job_url"]).drop_duplicates(subset=["title", "company"])
    n_raw = len(jobs)
    jobs = jobs[jobs.apply(lambda r: seniority_ok(r, min_y, max_y), axis=1)].copy()
    jobs["관련도"] = jobs.apply(pre_score, axis=1)                 # 키워드 관련도 계산
    jobs = jobs.sort_values("관련도", ascending=False)             # 관련도 높은 순으로 정렬
    rest = jobs.iloc[max_score:].copy()                            # 채점 못 하는 나머지 (버리지 않고 보관)
    jobs = jobs.head(max_score).copy()                             # 관련도 상위 N개만 AI 채점
    st.info(f"공고 {n_raw}개 수집 → 직급 필터 후 {len(jobs) + len(rest)}개 → 관련도 상위 {len(jobs)}개 AI 채점"
            + (f" (나머지 {len(rest)}개는 '미채점'으로 표 아래쪽에 표시)" if len(rest) else ""))

    # 2) 채점
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    bar2 = st.progress(0.0, text="이력서와 비교하는 중...")
    results = []
    for i, (_, row) in enumerate(jobs.iterrows()):
        try:
            results.append(score_one(client, resume, row))
        except Exception:
            results.append((None, "", "채점 실패"))
        bar2.progress((i + 1) / len(jobs), text=f"이력서와 비교하는 중... {i + 1}/{len(jobs)}")
    jobs["점수"], jobs["요구 경력"], jobs["한줄평"] = zip(*results)
    jobs = jobs.sort_values("점수", ascending=False)
    if len(rest):                                    # 미채점 공고를 표 맨 아래에 붙이기
        rest["점수"], rest["요구 경력"], rest["한줄평"] = None, "", "미채점 (채점 한도 초과)"
        jobs = pd.concat([jobs, rest], ignore_index=True)
    st.session_state["result"] = jobs   # 필터 바꿔도 결과 유지

# ---------------------------------------------------------------
# 결과 표시
# ---------------------------------------------------------------
if "result" in st.session_state:
    jobs = st.session_state["result"]
    c1, c2 = st.columns([1, 2])
    min_show = c1.slider("최소 점수", 0, 100, 60)
    picked = c2.multiselect("나라 필터", sorted(jobs["country"].unique()),
                            default=sorted(jobs["country"].unique()))
    show_unscored = st.checkbox("미채점 공고도 보기", value=False)
    ok_score = jobs["점수"].fillna(-1) >= min_show                   # 점수 기준 통과
    if show_unscored:
        ok_score = ok_score | jobs["점수"].isna()                    # 미채점도 포함
    view = jobs[ok_score & (jobs["country"].isin(picked))]

    show = view.rename(columns={"title": "직무", "company": "회사", "location": "위치",
                                "country": "나라", "date_posted": "게시일", "job_url": "링크"})
    cols = ["점수", "직무", "회사", "나라", "위치", "요구 경력", "한줄평", "게시일", "링크"]
    st.dataframe(
        show[[c for c in cols if c in show.columns]],
        hide_index=True, width="stretch", height=600,
        column_config={
            "점수": st.column_config.ProgressColumn("점수", min_value=0, max_value=100, format="%d"),
            "링크": st.column_config.LinkColumn("링크", display_text="열기"),
            "한줄평": st.column_config.TextColumn("한줄평", width="large"),
        },
    )

    buf = io.BytesIO()
    show[[c for c in cols if c in show.columns]].to_excel(buf, index=False)
    st.download_button("📥 엑셀로 받기", buf.getvalue(), "pharma_jobs.xlsx")
elif not go:
    st.info("👈 왼쪽에서 이력서를 올리고 나라를 고른 뒤 **검색 시작**을 누르세요. 10~30분 걸릴 수 있어요.")
