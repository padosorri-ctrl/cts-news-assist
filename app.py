import streamlit as st
import datetime
import requests
import io
import re
import zipfile
import pdfplumber
from pypdf import PdfReader
import docx

st.set_page_config(
    page_title="CTS 보도국 Style Assist Pro",
    page_icon="🎙️",
    layout="wide"
)

# 1. 보안 인증 로직
try:
    ADMIN_PASSWORD = st.secrets.get("ACCESS_PASSWORD", "cts2026")
except Exception:
    ADMIN_PASSWORD = "cts2026"

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.markdown("""
    <style>
        .auth-box { max-width: 420px; margin: 80px auto; padding: 30px; border: 1px solid #E2E8F0; border-radius: 12px; background-color: #FFFFFF; }
        .auth-title { font-size: 20px; font-weight: 800; color: #0F2C59; text-align: center; margin-bottom: 8px; }
        .auth-desc { font-size: 13px; color: #64748B; text-align: center; margin-bottom: 20px; }
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<div class="auth-box"><div class="auth-title">🎙️ CTS 보도국 Style Assist Pro</div><div class="auth-desc">보도국 인가 사용자 전용 시스템입니다.<br>사내 인증 비밀번호를 입력해 주세요.</div>', unsafe_allow_html=True)
    
    pw_input = st.text_input("보도국 접근 비밀번호", type="password", key="login_pw")
    if st.button("인증 및 접속", type="primary", use_container_width=True):
        if pw_input == ADMIN_PASSWORD:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다. 데스크에 문의하세요.")
            
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

# 2. 파일 텍스트 추출 엔진
def extract_all_text(uploaded_file):
    text = ""
    file_name = uploaded_file.name
    file_ext = file_name.split('.')[-1].lower()
    bytes_data = uploaded_file.getvalue()
    
    try:
        if file_ext == 'pdf':
            try:
                with pdfplumber.open(io.BytesIO(bytes_data)) as pdf:
                    pages_text = [page.extract_text() for page in pdf.pages if page.extract_text()]
                    text = "\n".join(pages_text)
            except Exception:
                text = ""
            if not text.strip():
                try:
                    pdf_reader = PdfReader(io.BytesIO(bytes_data))
                    pages_text = [p.extract_text() for p in pdf_reader.pages if p.extract_text()]
                    text = "\n".join(pages_text)
                except Exception:
                    pass

        elif file_ext == 'docx':
            doc = docx.Document(io.BytesIO(bytes_data))
            text = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])

        elif file_ext == 'txt':
            try:
                text = bytes_data.decode('utf-8')
            except UnicodeDecodeError:
                text = bytes_data.decode('cp949', errors='ignore')

        elif file_ext == 'hwpx':
            with zipfile.ZipFile(io.BytesIO(bytes_data)) as z:
                xml_files = [f for f in z.namelist() if f.startswith('Contents/section') and f.endswith('.xml')]
                for xf in xml_files:
                    xml_content = z.read(xf).decode('utf-8', errors='ignore')
                    cleaned = re.sub(r'<[^>]+>', ' ', xml_content)
                    text += cleaned + "\n"

        elif file_ext == 'hwp':
            try:
                raw_decoded = bytes_data.decode('utf-16le', errors='ignore')
                hangul_blocks = re.findall(r'[\w\s.,·~"\'()\[\]\n\r\t/가-힣]{5,}', raw_decoded)
                if hangul_blocks:
                    text = "\n".join(hangul_blocks)
            except Exception:
                pass
            if not text.strip():
                raw_cp949 = bytes_data.decode('cp949', errors='ignore')
                hangul_blocks = re.findall(r'[\w\s.,·~"\'()\[\]\n\r\t/가-힣]{5,}', raw_cp949)
                text = "\n".join(hangul_blocks)

    except Exception as e:
        st.error(f"파일 파싱 오류: {str(e)}")
        
    return text.strip()

# 3. 메인 화면 구성
st.markdown("""
<style>
    .main-title { font-size: 22px; font-weight: 800; color: #0F2C59; margin-bottom: 20px; }
    .stTextArea textarea { font-family: 'Pretendard', 'Apple SD Gothic Neo', sans-serif; font-size: 13.5px; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">🎙️ CTS 보도국 Style Assist Pro (단신 자동 생성 시스템)</div>', unsafe_allow_html=True)

try:
    api_key = st.secrets.get("GEMINI_API_KEY", "")
except Exception:
    api_key = ""

if not api_key:
    with st.sidebar:
        st.header("⚙️ 시스템 설정")
        api_key = st.text_input("Gemini API Key 입력", type="password")

col_in, col_out = st.columns([1, 1])
today_str = datetime.datetime.now().strftime("%m%d")

with col_in:
    st.subheader("📥 기사 세부 설정 & 보도자료 입력")
    
    c1, c2 = st.columns([1, 1])
    with c1:
        article_num = st.text_input("기사 순번", value=f"{today_str} 기사1 [단신]")
    with c2:
        rep_name = st.text_input("담당 기자명", value="이가영")
        
    c3, c4 = st.columns([1, 1])
    with c3:
        target_rt_opt = st.selectbox(
            "⏱️ 목표 방송 분량 (RT)",
            [
                "50초 단신 (상보 심층 / 공백포함 310~325자)",
                "40초 단신 (표준 / 공백포함 245~255자)",
                "30초 단신 (단문 요약 / 공백포함 185~195자)"
            ]
        )
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
                default=["전승민"]
            )
            cam_str = " ".join(cam_list) if cam_list else "전승민"
            source_input = st.text_input("자료출처 표기 (선택)", placeholder="필요시 기재 (예: 유튜브 채널명 등)")

    with c6:
        edit_list = st.multiselect(
            "✂️ 영상편집 기자 선택 (중복 가능)",
            ["최신영", "서혜원", "강성민"],
            default=["최신영"]
        )
        edit_str = " ".join(edit_list) if edit_list else "최신영"

    if credit_type == "자료제공":
        staff_str = f"{provider_str}\n영상편집 {edit_str}"
    else:
        staff_str = f"영상취재 {cam_str}\n영상편집 {edit_str}"

    if source_input:
        staff_str += f"\n(자료출처: {source_input})"

    st.markdown("---")
    custom_instruction = st.text_input(
        "💡 특별 강조 사항 (선택)",
        placeholder="예: 대표회장 발언 강조, 기부 금액 명시, 특정 교단명 필수 표기 등"
    )

    uploaded_file = st.file_uploader(
        "📁 보도자료 파일 첨부 (PDF, DOCX, TXT, HWP, HWPX)",
        type=["pdf", "docx", "txt", "hwp", "hwpx"],
        key="uploader"
    )
    
    extracted_text = ""
    if uploaded_file is not None:
        extracted_text = extract_all_text(uploaded_file)
        if extracted_text:
            st.success(f"✅ 파일에서 텍스트 {len(extracted_text)}자를 정상 추출했습니다.")
        else:
            st.error("❌ 파일에서 텍스트를 추출하지 못했습니다. 아래 창에 직접 붙여넣어 주세요.")

    raw_content = st.text_area(
        "📝 보도자료 원문 (파일 첨부 시 여기에 추출된 글이 보입니다)",
        value=extracted_text if extracted_text else "",
        height=200,
        placeholder="파일을 첨부하거나 여기에 보도자료를 직접 붙여넣으세요..."
    )
    
    run_btn = st.button("⚡ CTS 맞춤형 단신 원고 생성", type="primary", use_container_width=True)

with col_out:
    st.subheader("📤 CTS 표준 기사 (Output)")
    
    if run_btn:
        final_input_text = raw_content.strip()
        
        if not api_key:
            st.error("좌측 사이드바에 Gemini API Key를 입력해주세요.")
        elif not final_input_text:
            st.error("⚠️ 보도자료 내용이 비어있습니다.")
        else:
            with st.spinner("단신 원고를 작성 중..."):
                clean_key = api_key.strip()

                prompt = f"""
당신은 CTS기독교텔레비전 보도국의 시니어 데스크 에디터입니다.
제공된 [보도자료 원문 텍스트]를 바탕으로 방송 단신 원고를 작성하십시오.

[작성 옵션]:
- 식별 헤더: {article_num}
- 담당 기자: {rep_name}
- 영상 스태프 정보:
{staff_str}
- 목표 분량 (RT): {target_rt_opt}
- 목표 문단 구조: {para_count}
- 기자 특별 강조 지시: {custom_instruction if custom_instruction else "없음 (원문의 중요 팩트 우선 배분)"}

[보도자료 원문 텍스트]:
{final_input_text}

[★ 방송 리딩 분량 규격 ★]:
- 아래 글자 수 범위를 정확히 충족하여 방송 초수를 맞추십시오:
  * 30초 선택 시: 3단락 / 본문 전체 글자 수 공백 포함 [185~195자]
  * 40초 선택 시: 3~4단락 / 본문 전체 글자 수 공백 포함 [245~255자]
  * 50초 선택 시: 4단락 / 본문 전체 글자 수 공백 포함 [310~325자]

[★ 문체 및 바자막 규칙 ★]:
1. 모든 문장은 방송용 단문 구어체(~했습니다, ~밝혔습니다, ~강조했습니다, ~전했습니다, ~방침입니다)로 작성하십시오.
2. 바자막(CG 자막 1행):
   - 대괄호 `[]`는 절대 쓰지 마십시오.
   - 보고서명, 저작물, 조례명 등 고유명사는 작은따옴표(`' '`)로 표기하십시오. (예: '세계복음주의 전망 연구조사 보고서' 발간)
   - 현장 행사는 슬래시로 표기하십시오. (예: 새문안교회 2026 광복주일찬양예배 // 9일 / 서울 종로구)

[출력 양식]:
{article_num} [기사 메인 타이틀] ({rep_name})

[기사 본문 ({para_count} 및 지정된 글자 수 엄수)]

'특정보고서명' 발간 (또는 행사명 // 일시 / 장소)
핵심 요약 자막 1줄 (주요 통계 또는 인용구)
{staff_str}

교단 키워드 : [예장통합, 기감 등 / 없으면 X]
인물 키워드 : [주요 인물 쉼표 구분 / 없으면 X]
장소 키워드 : [행사 장소 / 없으면 X]
조이고 키워드 : [핵심 검색 키워드 쉼표 구분 5~8개]

썸네일
[썸네일 메인 카피 1~2줄]

썸네일 사진
[대표 이미지 가이드]

해시태그
#CTS #CTS뉴스 #[키워드1] #[키워드2] #[키워드3]
"""
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                headers = {"Content-Type": "application/json"}
                
                available_models = []
                try:
                    list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={clean_key}"
                    list_res = requests.get(list_url, timeout=10)
                    if list_res.status_code == 200:
                        models_data = list_res.json().get('models', [])
                        for m in models_data:
                            if "generateContent" in m.get("supportedGenerationMethods", []):
                                available_models.append(m.get("name"))
                except Exception:
                    pass

                flash_models = [m for m in available_models if 'flash' in m.lower()]
                other_models = [m for m in available_models if 'flash' not in m.lower()]
                candidate_models = flash_models + other_models

                if not candidate_models:
                    candidate_models = ["models/gemini-2.5-flash", "models/gemini-2.0-flash"]

                success = False
                last_err = ""
                for model_name in candidate_models:
                    target = model_name if model_name.startswith("models/") else f"models/{model_name}"
                    endpoint = f"https://generativelanguage.googleapis.com/v1beta/{target}:generateContent?key={clean_key}"
                    
                    try:
                        res = requests.post(endpoint, headers=headers, json=payload, timeout=30)
                        if res.status_code == 200:
                            res_json = res.json()
                            result_text = res_json['candidates'][0]['content']['parts'][0]['text']
                            st.text_area("완성된 단신 기사 (복사하여 전체 원고에 사용)", value=result_text, height=470)
                            display_name = target.replace('models/', '')
                            st.success(f"원고 생성 완료 (엔진: {display_name})")
                            success = True
                            break
                        else:
                            last_err = f"{res.status_code}: {res.text}"
                    except Exception as ex:
                        last_err = str(ex)
                        continue
                
                if not success:
                    st.error(f"생성 실패: {last_err}")
    else:
        st.info("보도자료를 첨부하거나 원문을 넣은 후 [CTS 맞춤형 단신 원고 생성]을 누르면 즉시 작성됩니다.")
