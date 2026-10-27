import base64
import csv
import datetime
import hmac
import os
import re
import time

import requests
import streamlit as st

from extractors import SUPPORTED_EXTS, extract

st.set_page_config(
    page_title="CTS 보도국 Style Assist Pro",
    page_icon="🎙️",
    layout="wide",
)

KST = datetime.timezone(datetime.timedelta(hours=9))
LOG_PATH = "usage_log.csv"
MAX_AUTH_FAILS = 5
MAX_MEDIA_BYTES = 15 * 1024 * 1024
DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
MAX_ATTEMPTS = 3  # 최초 생성 + 분량 보정 재생성 2회


def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# ------------------------------------------------------------------ 1. 보안 인증
ADMIN_PASSWORD = str(get_secret("ACCESS_PASSWORD", "") or "")

if not ADMIN_PASSWORD:
    st.error(
        "서버에 ACCESS_PASSWORD가 설정되어 있지 않아 접속을 막았습니다. "
        "Streamlit 설정의 Secrets에 ACCESS_PASSWORD를 등록해 주세요."
    )
    st.stop()

st.session_state.setdefault("authenticated", False)
st.session_state.setdefault("auth_fails", 0)

if not st.session_state["authenticated"]:
    st.markdown("""
    <style>
        .auth-title { font-size: 20px; font-weight: 800; text-align: center; margin-top: 60px; }
        .auth-desc { font-size: 13px; color: #64748B; text-align: center; margin-bottom: 20px; }
    </style>
    <div class="auth-title">🎙️ CTS 보도국 Style Assist Pro</div>
    <div class="auth-desc">보도국 인가 사용자 전용 시스템입니다.<br>사내 인증 비밀번호를 입력해 주세요.</div>
    """, unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        if st.session_state["auth_fails"] >= MAX_AUTH_FAILS:
            st.error("실패 횟수를 초과했습니다. 페이지를 새로고침하거나 데스크에 문의하세요.")
        else:
            pw_input = st.text_input("보도국 접근 비밀번호", type="password", key="login_pw")
            if st.button("인증 및 접속", type="primary", use_container_width=True):
                if hmac.compare_digest(pw_input.encode(), ADMIN_PASSWORD.encode()):
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.session_state["auth_fails"] += 1
                    st.error("비밀번호가 일치하지 않습니다. 데스크에 문의하세요.")
    st.stop()


# ------------------------------------------------------------------ 2. 보조 함수
RT_SPEC = {
    "50초 단신 (상보 심층 / 공백포함 310~325자)": {"lo": 310, "hi": 325, "sec": 50},
    "40초 단신 (표준 / 공백포함 245~255자)": {"lo": 245, "hi": 255, "sec": 40},
    "30초 단신 (단문 요약 / 공백포함 185~195자)": {"lo": 185, "hi": 195, "sec": 30},
}

BODY_START, BODY_END = "<<본문시작>>", "<<본문끝>>"


class GeminiError(Exception):
    pass


def call_gemini(api_key, contents, models):
    """모델을 순서대로 시도한다. 키 오류는 즉시 중단, 일시 오류/모델 없음은 다음 모델로."""
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {"contents": contents, "generationConfig": {"temperature": 0.3}}
    last_err = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=60)
            except requests.RequestException as ex:
                last_err = f"{model}: 네트워크 오류 {ex}"
                break
            if res.status_code == 200:
                data = res.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"], model
                except (KeyError, IndexError):
                    reason = data.get("promptFeedback", {}).get("blockReason", "응답 없음")
                    last_err = f"{model}: 응답이 비어 있습니다 ({reason})"
                    break
            body = res.text[:300]
            if res.status_code in (401, 403) or (res.status_code == 400 and "API key" in res.text):
                raise GeminiError(f"API 키 오류({res.status_code}). 키를 확인해 주세요. {body}")
            last_err = f"{model}: {res.status_code} {body}"
            if res.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(3)
                continue
            break
    raise GeminiError(last_err or "알 수 없는 오류")


def analyze_body(text):
    """본문(마커 사이)의 글자 수(공백 포함, 단락 사이 줄바꿈 제외)와 단락 수."""
    m = re.search(re.escape(BODY_START) + r"\s*(.*?)\s*" + re.escape(BODY_END), text, re.S)
    if not m:
        return None, None
    paras = [p for p in re.split(r"\n\s*\n", m.group(1)) if p.strip()]
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in paras]
    return sum(len(p) for p in paras), len(paras)


def finalize_text(text, article_num):
    """마커 제거, 대괄호 제거(헤더 순번 표기는 보존), 빈 줄 정리."""
    token = "\u0000HEADER\u0000"
    text = text.replace(article_num, token)
    text = re.sub(r"[ \t]*" + re.escape(BODY_START) + r"[ \t]*\n?", "", text)
    text = re.sub(r"\n?[ \t]*" + re.escape(BODY_END) + r"[ \t]*", "", text)
    text = text.replace("[", "").replace("]", "")
    text = text.replace(token, article_num)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_log(row):
    try:
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각(KST)", "기사순번", "담당기자", "분량", "모델", "본문글자수", "단락수", "시도횟수", "기준충족"])
            w.writerow(row)
    except Exception:
        pass


def build_prompt(article_num, rep_name, staff_str, target_rt_opt, para_count,
                 custom_instruction, source_text, has_media):
    spec = RT_SPEC[target_rt_opt]
    if source_text:
        source_block = f"[보도자료 원문 텍스트]:\n{source_text}"
    else:
        source_block = "[보도자료 원문]: 첨부된 파일(문서 또는 이미지)의 내용을 직접 읽고 사용하십시오."
    return f"""
당신은 CTS기독교텔레비전 보도국의 시니어 데스크 에디터입니다.
제공된 보도자료를 바탕으로 방송 단신 원고를 작성하십시오.

[작성 옵션]:
- 식별 헤더: {article_num}
- 담당 기자: {rep_name if rep_name else "미입력(제목 뒤 괄호 표기 생략)"}
- 영상 스태프 정보:
{staff_str}
- 목표 분량 (RT): {target_rt_opt}
- 목표 문단 구조: {para_count}
- 기자 특별 강조 지시: {custom_instruction if custom_instruction else "없음 (원문의 중요 팩트 우선 배분)"}

{source_block}

[★ 방송 리딩 분량 규격 ★]:
- 본문 전체 글자 수는 공백 포함 {spec['lo']}~{spec['hi']}자여야 합니다. (단락 사이 줄바꿈은 글자 수에 넣지 않습니다.)
- 이 수치는 프로그램이 실제로 세어 검수하며, 벗어나면 다시 쓰게 됩니다.

[★ 문체 및 언론사 보도 규칙 (매우 중요) ★]:
1. 모든 문장은 철저히 객관적인 방송용 단문 구어체(~했습니다, ~밝혔습니다, ~모으고 있습니다, ~전망입니다)로만 작성하십시오.
2. [절대 금지 사항]: "~복된 시간이 되길 기대를 모읍니다", "~은혜가 되길 바랍니다" 등 주관적이거나 종교적인 기원문, 감상적 맺음말은 기사 본문에 절대 쓰지 마십시오. 오직 드라이한 팩트 위주의 언론사 보도국 톤을 완벽히 유지하십시오.

[★ 기호 사용 금지 규칙 ★]:
- 기사 본문, 바자막(CG), 썸네일 카피를 포함한 모든 출력물에서 대괄호 기호를 절대 사용하지 마십시오. (식별 헤더 "{article_num}"만 주어진 그대로 표기합니다.)
- 보고서명, 저작물, 조례명 등 고유명사는 작은따옴표로 표기하십시오.
- 현장 행사는 슬래시로 표기하십시오. (예: 새문안교회 2026 광복주일찬양예배 // 9일 / 서울 종로구)

[★ 단락 구성 규칙 ★]:
- 본문은 정확히 {para_count[0]}개 단락으로 구성합니다.
- 단락은 내용의 의도와 흐름(도입, 세부, 인용, 전망 등)에 따라 직접 판단해 나누십시오. 단락당 문장 수는 정해져 있지 않으며, 한 단락에 여러 문장이 들어가도 됩니다.
- 단락과 단락 사이에는 빈 줄 하나(엔터 2번)를 넣어 완전히 분리하십시오. 한 덩어리로 뭉쳐 쓰지 마십시오.

[출력 양식]: 아래 형식을 그대로 따르되 안내 문구는 실제 내용으로 바꿔 쓰십시오.
{article_num} 기사 메인 타이틀{f" ({rep_name})" if rep_name else ""}

{BODY_START}
(본문 {para_count[0]}단락. 마커 두 줄은 반드시 그대로 출력)
{BODY_END}

'특정보고서명' 발간 (또는 행사명 // 일시 / 장소)
핵심 요약 자막 1줄 (주요 통계 또는 인용구)
{staff_str}

교단 키워드 : 예장통합, 기감 등 (없으면 X)
인물 키워드 : 주요 인물 쉼표 구분 (없으면 X)
장소 키워드 : 행사 장소 (없으면 X)
조이고 키워드 : 핵심 검색 키워드 쉼표 구분 5~8개

썸네일
썸네일 메인 카피 1줄 (10~18자 내외로 매우 짧고 강렬하게)

썸네일 사진
대표 이미지 가이드 내용

해시태그
#CTS #CTS뉴스 #키워드1 #키워드2 #키워드3
"""


# ------------------------------------------------------------------ 3. 메인 화면
st.markdown("""
<style>
    .main-title { font-size: 22px; font-weight: 800; margin-bottom: 20px; }
    .stTextArea textarea { font-family: 'Pretendard', 'Apple SD Gothic Neo', sans-serif; font-size: 13.5px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🎙️ CTS 보도국 Style Assist Pro (단신 자동 생성 시스템)</div>',
            unsafe_allow_html=True)

api_key = str(get_secret("GEMINI_API_KEY", "") or "")

with st.sidebar:
    st.header("⚙️ 시스템")
    if not api_key:
        api_key = st.text_input("Gemini API Key 입력", type="password")
    else:
        st.caption("API 키: 서버 설정 사용 중")
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "rb") as f:
            st.download_button("📊 사용 이력(CSV) 내려받기", f.read(),
                               file_name="usage_log.csv", mime="text/csv")
    if st.button("로그아웃"):
        st.session_state["authenticated"] = False
        st.rerun()

col_in, col_out = st.columns([1, 1])
today_str = datetime.datetime.now(KST).strftime("%m%d")

with col_in:
    st.subheader("📥 기사 세부 설정 & 보도자료 입력")

    c1, c2 = st.columns([1, 1])
    with c1:
        article_num = st.text_input("기사 순번", value=f"{today_str} 기사1 [단신]")
    with c2:
        rep_name = st.text_input("담당 기자명", value="", placeholder="기자 이름 입력").strip()

    c3, c4 = st.columns([1, 1])
    with c3:
        target_rt_opt = st.selectbox("⏱️ 목표 방송 분량 (RT)", list(RT_SPEC.keys()))
    with c4:
        if "30초" in target_rt_opt:
            para_options = ["3단락 (도입-핵심데이터-전망 / 30초 표준)"]
        elif "40초" in target_rt_opt:
            para_options = ["3단락 (도입-세부-인용)", "4단락 (도입-세부-인용-전망)"]
        else:
            para_options = ["4단락 (도입-세부-인용-사역의의 / 50초 표준)"]
        para_count = st.selectbox("📑 기사 문단 구조", para_options)

    st.markdown("---")
    credit_type = st.radio("영상 구분", ["자료제공", "현장취재 (영상취재)"], horizontal=True)

    c5, c6 = st.columns([1, 1])
    with c5:
        if credit_type == "자료제공":
            provider_input = st.text_input("자료제공처 입력", placeholder="예: 목회데이터연구소, 한국교회총연합 등")
            source_input = st.text_input("자료출처 표기 (선택)", placeholder="예: 화면제공 유튜브, 현장 녹화본 등")
            provider_str = f"자료제공 {provider_input}" if provider_input else "자료제공"
        else:
            cam_list = st.multiselect(
                "🎥 영상취재 기자 선택 (중복 가능)",
                ["전승민", "전용완", "김효근", "김대상", "우태림", "오현택"],
                default=["전승민"],
            )
            cam_str = " ".join(cam_list) if cam_list else "전승민"
            source_input = st.text_input("자료출처 표기 (선택)", placeholder="필요시 기재 (예: 유튜브 채널명 등)")

    with c6:
        edit_list = st.multiselect(
            "✂️ 영상편집 기자 선택 (중복 가능)",
            ["최신영", "서혜원", "강성민"],
            default=[],
        )
        edit_str = " ".join(edit_list)

    edit_line = f"\n영상편집 {edit_str}" if edit_str else ""
    if credit_type == "자료제공":
        staff_str = f"{provider_str}{edit_line}"
    else:
        staff_str = f"영상취재 {cam_str}{edit_line}"
    if source_input:
        staff_str += f"\n(자료출처: {source_input})"

    st.markdown("---")
    custom_instruction = st.text_input(
        "💡 특별 강조 사항 (선택)",
        placeholder="예: 대표회장 발언 강조, 기부 금액 명시, 특정 교단명 필수 표기 등",
    )

    uploaded_file = st.file_uploader(
        "📁 보도자료 파일 첨부 (PDF, DOCX, HWP, HWPX, TXT, HTML, 이미지) - 붙여넣기만 해도 됩니다",
        type=SUPPORTED_EXTS,
        key="uploader",
    )

    st.session_state.setdefault("raw_content", "")
    if uploaded_file is None:
        st.session_state["file_id"] = None
        st.session_state["media"] = None
    else:
        file_id = f"{uploaded_file.name}-{uploaded_file.size}"
        if st.session_state.get("file_id") != file_id:  # 새 파일일 때만 추출 (사용자 수정 내용 보존)
            st.session_state["file_id"] = file_id
            st.session_state["media"] = None
            st.session_state["file_msg"] = ("info", "")
            try:
                res = extract(uploaded_file.name, uploaded_file.getvalue())
                if res.text:
                    st.session_state["raw_content"] = res.text
                    msg = f"✅ 텍스트 {len(res.text)}자를 추출했습니다."
                    if res.note:
                        msg += f" {res.note}"
                    st.session_state["file_msg"] = ("success", msg)
                elif res.media:
                    if len(res.media[1]) > MAX_MEDIA_BYTES:
                        st.session_state["file_msg"] = (
                            "error", "파일이 너무 큽니다(15MB 초과). 본문을 직접 붙여넣어 주세요.")
                    else:
                        st.session_state["media"] = res.media
                        st.session_state["file_msg"] = ("warning", "📎 " + res.note)
                else:
                    st.session_state["file_msg"] = (
                        "error", "텍스트를 추출하지 못했습니다. 아래 창에 직접 붙여넣어 주세요.")
            except Exception as ex:
                st.session_state["file_msg"] = (
                    "error", f"파일을 읽는 중 오류가 났습니다: {ex} / 아래 창에 직접 붙여넣어 주세요.")
        kind, msg = st.session_state.get("file_msg", ("info", ""))
        if msg:
            getattr(st, kind)(msg)

    raw_content = st.text_area(
        "📝 보도자료 원문 (파일 첨부 시 추출된 글이 보이며, 직접 수정·붙여넣기도 가능)",
        height=200,
        placeholder="파일을 첨부하거나 여기에 보도자료를 직접 붙여넣으세요...",
        key="raw_content",
    )

    run_btn = st.button("⚡ CTS 맞춤형 단신 원고 생성", type="primary", use_container_width=True)

with col_out:
    st.subheader("📤 CTS 표준 기사 (Output)")

    if run_btn:
        source_text = raw_content.strip()
        media = st.session_state.get("media")

        if not api_key:
            st.error("좌측 사이드바에 Gemini API Key를 입력해주세요.")
        elif not source_text and not media:
            st.error("⚠️ 보도자료 내용이 비어있습니다.")
        else:
            spec = RT_SPEC[target_rt_opt]
            target_paras = int(para_count[0])
            prompt = build_prompt(article_num, rep_name, staff_str, target_rt_opt, para_count,
                                  custom_instruction, source_text, bool(media))

            first_parts = [{"text": prompt}]
            if media and not source_text:
                first_parts.append({"inline_data": {
                    "mime_type": media[0],
                    "data": base64.b64encode(media[1]).decode(),
                }})

            models = [m for m in [str(get_secret("GEMINI_MODEL", "") or "")] + DEFAULT_MODELS if m]
            models = list(dict.fromkeys(models))

            contents = [{"role": "user", "parts": first_parts}]
            best = None  # (거리, 텍스트, 글자수, 단락수, 모델)
            attempts = 0
            error = ""

            with st.spinner("단신 원고를 작성하고 분량을 검수하는 중..."):
                try:
                    for attempts in range(1, MAX_ATTEMPTS + 1):
                        text, used_model = call_gemini(api_key.strip(), contents, models)
                        count, n_paras = analyze_body(text)
                        if count is None:
                            dist = 10**6
                            feedback = (f"본문을 {BODY_START} 와 {BODY_END} 두 줄 사이에 넣지 않았습니다. "
                                        "출력 양식을 처음과 똑같이 지켜 다시 출력하십시오.")
                        else:
                            over = max(spec["lo"] - count, count - spec["hi"], 0)
                            dist = over + (0 if n_paras == target_paras else 100)
                            parts = []
                            if count < spec["lo"]:
                                parts.append(f"{spec['lo'] - count}자 이상 늘리십시오")
                            elif count > spec["hi"]:
                                parts.append(f"{count - spec['hi']}자 이상 줄이십시오")
                            if n_paras != target_paras:
                                parts.append(f"단락을 정확히 {target_paras}개로 맞추십시오")
                            feedback = (
                                f"방금 원고의 본문은 공백 포함 {count}자, {n_paras}단락입니다. "
                                f"목표는 {spec['lo']}~{spec['hi']}자, {target_paras}단락입니다. "
                                + ", ".join(parts) + ". 사실관계와 문체 규칙은 유지하고 분량과 단락만 조정해, "
                                "전체 출력 양식을 처음과 똑같이 다시 출력하십시오."
                            )
                        if best is None or dist < best[0]:
                            best = (dist, text, count, n_paras, used_model)
                        if dist == 0:
                            break
                        contents = contents + [
                            {"role": "model", "parts": [{"text": text}]},
                            {"role": "user", "parts": [{"text": feedback}]},
                        ]
                except GeminiError as ex:
                    error = str(ex)

            if best is None:
                st.session_state["result"] = {"error": error or "생성에 실패했습니다."}
            else:
                dist, text, count, n_paras, used_model = best
                st.session_state["result"] = {
                    "text": finalize_text(text, article_num),
                    "count": count, "paras": n_paras, "model": used_model,
                    "ok": dist == 0, "attempts": attempts,
                    "lo": spec["lo"], "hi": spec["hi"], "target_paras": target_paras,
                    "warn": error,
                }
                write_log([
                    datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                    article_num, rep_name, target_rt_opt, used_model,
                    count, n_paras, attempts, "Y" if dist == 0 else "N",
                ])

    result = st.session_state.get("result")
    if result:
        if "error" in result:
            st.error(f"생성 실패: {result['error']}")
        else:
            st.text_area("완성된 단신 기사 (복사하여 전체 원고에 사용)",
                         value=result["text"], height=470)
            if result["count"] is None:
                st.warning("본문 구간을 인식하지 못해 글자 수를 검수하지 못했습니다. 직접 확인해 주세요.")
            elif result["ok"]:
                st.success(
                    f"✅ 본문 {result['count']}자 / {result['paras']}단락 "
                    f"(기준 {result['lo']}~{result['hi']}자, {result['target_paras']}단락 충족) "
                    f"· 엔진 {result['model']} · 시도 {result['attempts']}회")
            else:
                st.warning(
                    f"⚠️ 본문 {result['count']}자 / {result['paras']}단락으로 기준 "
                    f"({result['lo']}~{result['hi']}자, {result['target_paras']}단락)에 "
                    f"{result['attempts']}회 시도 후에도 맞지 않았습니다. 데스크에서 다듬어 주세요. "
                    f"(엔진 {result['model']})")
            if result.get("warn"):
                st.caption(f"참고: {result['warn']}")
    else:
        st.info("보도자료를 첨부하거나 원문을 넣은 후 [CTS 맞춤형 단신 원고 생성]을 누르면 즉시 작성됩니다.")
