# -*- coding: utf-8 -*-
# 제약 BD/전략 공고 다국가 검색 + 이력서 매칭 순위 웹앱 (Streamlit)
# 실행(로컬 테스트): streamlit run app.py

import io                 # 업로드된 PDF를 메모리에서 읽을 때 사용
import json               # AI 답변(JSON 텍스트)을 딕셔너리로 변환
import hashlib            # 비밀번호로 출입증 문자열 만들기
import pickle             # 결과를 파일로 저장/불러오기
import threading          # 검색을 화면과 별개로 뒤에서 돌리기
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
    token = hashlib.sha256(("jf-" + pw).encode()).hexdigest()[:16]   # 비번에서 만든 출입증 문자열
    if st.session_state.get("authed") or st.query_params.get("k") == token:  # 이미 로그인 or 주소에 출입증
        st.session_state["authed"] = True
        st.query_params["k"] = token                  # 주소창에 출입증 유지 (즐겨찾기해도 OK)
        return True
    st.title("💊 제약 BD 잡 파인더")
    entered = st.text_input("비밀번호", type="password")
    if st.button("들어가기"):
        if entered == pw:
            st.session_state["authed"] = True
            st.query_params["k"] = token
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
# 백그라운드 작업: 화면이 꺼져도 서버에서 계속 돌아가고, 결과는 파일로 저장
# ---------------------------------------------------------------
RESULT_FILE = "/tmp/jobfinder_last_result.pkl"   # 마지막 검색 결과
RESUME_FILE = "/tmp/jobfinder_last_resume.txt"   # 마지막으로 쓴 이력서


@st.cache_resource          # 모든 접속이 같이 쓰는 하나의 상태 보관함 (앱이 켜져 있는 동안 유지)
def job_state():
    return {"running": False, "progress": 0.0, "status": "", "error": "", "lock": threading.Lock()}


def load_result():
    try:
        with open(RESULT_FILE, "rb") as f:
            return pickle.load(f)          # {"jobs": 표, "info": 요약문, "finished": 끝난 시각}
    except Exception:
        return None


def run_search(S, resume, countries, terms, min_y, max_y, days, per, max_score, api_key):
    """화면과 별개의 스레드에서 실행. st.* 화면 함수는 쓰면 안 됨 → S(상태 보관함)만 업데이트"""
    try:
        frames, combos = [], [(c, t) for c in countries for t in terms]
        for i, (label, term) in enumerate(combos):
            loc, code = COUNTRY_OPTIONS[label]
            S["progress"] = 0.5 * i / len(combos)                       # 수집 단계 = 진행률 0~50%
            S["status"] = f"공고 모으는 중 ({i + 1}/{len(combos)}): {label} / {term}"
            try:
                df = scrape_jobs(site_name=["linkedin", "indeed"], search_term=term, location=loc,
                                 country_indeed=code, results_wanted=per, hours_old=days * 24,
                                 linkedin_fetch_description=True, verbose=0)
                df["country"] = label
                frames.append(df)
            except Exception:
                pass                                                    # 한 조합 실패해도 계속
            time.sleep(3)

        if not frames or sum(len(f) for f in frames) == 0:
            S["error"] = "공고를 하나도 못 가져왔어요. 잠시 후 다시 시도해 주세요."
            return

        jobs = pd.concat(frames, ignore_index=True)
        jobs = jobs.drop_duplicates(subset=["job_url"]).drop_duplicates(subset=["title", "company"])
        n_raw = len(jobs)
        jobs = jobs[jobs.apply(lambda r: seniority_ok(r, min_y, max_y), axis=1)].copy()
        jobs["관련도"] = jobs.apply(pre_score, axis=1)
        jobs = jobs.sort_values("관련도", ascending=False)
        rest = jobs.iloc[max_score:].copy()
        jobs = jobs.head(max_score).copy()
        info = (f"공고 {n_raw}개 수집 → 직급 필터 후 {len(jobs) + len(rest)}개 → 관련도 상위 {len(jobs)}개 AI 채점"
                + (f" (나머지 {len(rest)}개는 '미채점')" if len(rest) else ""))

        client = anthropic.Anthropic(api_key=api_key)
        results = []
        for i, (_, row) in enumerate(jobs.iterrows()):
            S["progress"] = 0.5 + 0.5 * i / max(len(jobs), 1)           # 채점 단계 = 50~100%
            S["status"] = f"이력서와 비교하는 중 ({i + 1}/{len(jobs)})"
            try:
                results.append(score_one(client, resume, row))
            except Exception:
                results.append((None, "", "채점 실패"))
        if results:
            jobs["점수"], jobs["요구 경력"], jobs["한줄평"] = zip(*results)
        jobs = jobs.sort_values("점수", ascending=False)
        if len(rest):
            rest["점수"], rest["요구 경력"], rest["한줄평"] = None, "", "미채점 (채점 한도 초과)"
            jobs = pd.concat([jobs, rest], ignore_index=True)

        finished = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 9 * 3600))  # 한국/일본 시간
        with open(RESULT_FILE, "wb") as f:
            pickle.dump({"jobs": jobs, "info": info, "finished": finished}, f)
    except Exception as e:
        S["error"] = f"오류가 났어요: {str(e)[:200]}"
    finally:
        S["running"] = False                                            # 끝나면 항상 '실행 중' 해제


# ---------------------------------------------------------------
# 화면
# ---------------------------------------------------------------
if not check_password():
    st.stop()

S = job_state()
st.title("💊 제약 BD 잡 파인더")
st.caption("여러 나라 공고를 한 번에 모아서, 내 이력서와 잘 맞는 순서대로 보여줘요. "
           "검색 중에 화면을 꺼도 서버에서 계속 돌아가요.")

try:
    with open(RESUME_FILE, encoding="utf-8") as f:
        last_resume = f.read()                     # 지난번 이력서 불러오기 (다시 안 올려도 됨)
except Exception:
    last_resume = ""

with st.sidebar:
    st.header("① 이력서")
    up = st.file_uploader("PDF나 TXT 파일 올리기", type=["pdf", "txt"])
    pasted = st.text_area("또는 여기에 붙여넣기 (지난번 이력서가 자동으로 채워져요)",
                          value=last_resume, height=150)

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

    go = st.button("🔍 검색 시작", type="primary", width="stretch", disabled=S["running"])
    if S["running"]:
        st.caption("검색이 진행 중이라 끝날 때까지 새 검색은 못 해요.")

if go:
    resume = read_resume(up, pasted)
    terms = [t.strip() for t in terms_text.splitlines() if t.strip()]
    if len(resume.strip()) < 100:
        st.error("이력서를 먼저 올리거나 붙여넣어 주세요.")
    elif not countries or not terms:
        st.error("나라와 검색어를 하나 이상 골라주세요.")
    else:
        with S["lock"]:                                  # 동시에 두 번 눌러도 하나만 시작
            if not S["running"]:
                with open(RESUME_FILE, "w", encoding="utf-8") as f:
                    f.write(resume)                      # 이력서 저장 → 다음에 자동으로 채워짐
                S.update(running=True, progress=0.0, status="시작하는 중...", error="")
                threading.Thread(
                    target=run_search, daemon=True,
                    args=(S, resume, countries, terms, min_y, max_y, days, per, max_score,
                          st.secrets["ANTHROPIC_API_KEY"]),
                ).start()
        st.rerun()


@st.fragment(run_every=3)        # 이 부분만 3초마다 새로고침해서 진행률 표시
def progress_box():
    if S["running"]:
        st.progress(min(S["progress"], 1.0), text=S["status"])
        st.caption("화면을 꺼도 괜찮아요. 나중에 다시 들어오면 여기서 이어서 보여요.")
        st.session_state["was_running"] = True
    elif st.session_state.pop("was_running", False):
        st.rerun(scope="app")                            # 방금 끝났으면 전체 새로고침해서 결과 표시


progress_box()
if S["error"]:
    st.error(S["error"])

# ---------------------------------------------------------------
# 결과 표시 (서버에 저장된 마지막 결과)
# ---------------------------------------------------------------
saved = load_result()
if saved is not None:
    jobs = saved["jobs"]
    st.success(f"마지막 검색 결과 · {saved['finished']} 완료 · {saved['info']}")
    c1, c2 = st.columns([1, 2])
    min_show = c1.slider("최소 점수", 0, 100, 60)
    picked = c2.multiselect("나라 필터", sorted(jobs["country"].unique()),
                            default=sorted(jobs["country"].unique()))
    show_unscored = st.checkbox("미채점 공고도 보기", value=False)
    ok_score = jobs["점수"].fillna(-1) >= min_show
    if show_unscored:
        ok_score = ok_score | jobs["점수"].isna()
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
elif not S["running"]:
    st.info("👈 왼쪽에서 이력서를 올리고 나라를 고른 뒤 **검색 시작**을 누르세요. 10~30분 걸릴 수 있어요.")
