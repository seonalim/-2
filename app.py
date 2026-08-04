"""
동종업계 대손충당금 설정률 비교 웹툴 (Streamlit)
==========================================

동종업종 기업들의 대손충당금 관련 지표를 DART(전자공시시스템) Open API로 실시간 수집하여
비교하고, 이상치(동종평균과 크게 차이나는 기업)를 z-score로 찾아내는 웹 도구입니다.
(※ "적정성 확인/판단"까지 내리는 도구가 아니라, 추가 검토가 필요한 대상을 찾아주는
   1차 스크리닝·비교 도구입니다 — 이름도 그에 맞게 "비교"로 정했습니다.)

계정 표시 관행이 기업마다 달라서, 세 가지 방식을 자동으로 시도합니다.
  A) 일반기업형: 대손충당금(잔액) / 매출채권(잔액)  ← 재무상태표에 별도 계정으로 잡히는 경우
  B) 금융회사형: 신용손실충당금 전입액(당기 손익) / 대출채권류(잔액)  ← 은행/금융지주처럼
     충당금 "잔액"은 순액표시라 안 잡히지만, 당기 신규 적립액은 손익계산서에 잡히는 경우
  C) 카드사 등 혼합형: 대손충당금(잔액) / 대출채권류(잔액, 카드자산·할부금융자산 등)
     ← 일부 카드사·캐피탈사는 IFRS9 도입 후에도 계정명은 "대손충당금"을 그대로 쓰면서
       자산 쪽만 매출채권이 아닌 카드자산/할부금융자산 등으로 잡히는 경우 (예: 롯데카드)

이번 버전에 추가된 기능
------------------------
2) 교차검증 지표: ROA, 자기자본비율(모든 회사) + 매출채권회전율(일반기업형만) 을 같이 보여줘서
   "설정률만으로는 판단이 안 서는" 문제를 보완합니다.
3) 연도별 추이(시계열): 최근 N개년의 비율 변화를 선그래프로 보여주고, 전년 대비 급변동을 표시합니다.
4) DART 원문 링크: 각 회사의 실제 공시 원문(사업보고서) 페이지로 바로 이동하는 링크를 제공합니다.
5) 계정 구성 내역 공개: 특히 방식 B(금융회사형)는 회사마다 "대출채권"이 아니라 "카드채권"/
   "할부금융자산"/"리스채권" 등 제각각 다른 계정명을 쓰기 때문에, 여러 계정명을 넓게 훑어
   합산합니다. 어떤 계정이 실제로 합산에 포함됐는지 펼쳐서 바로 확인할 수 있습니다.

배포 방법 (요약)
----------------
1. github.com 에서 새 저장소(repository)를 만들고 이 폴더의 app.py, requirements.txt 를
   웹 화면에서 그대로 드래그 앤 드롭으로 업로드합니다 (git 명령어 필요 없음).
2. share.streamlit.io 에서 GitHub 계정으로 로그인 → "New app" → 방금 만든 저장소 선택 → Deploy.
3. 앱 설정(Settings) → Secrets 에 아래처럼 입력합니다.
       DART_API_KEY = "발급받은_키"
4. 몇 분 뒤 https://내앱이름.streamlit.app 형태의 실제 URL이 생깁니다.
"""

import io
import os
import re
import time
import difflib
import zipfile
import concurrent.futures
import xml.etree.ElementTree as ET

import requests
import pandas as pd
import streamlit as st
import altair as alt

BASE_URL = "https://opendart.fss.or.kr/api"
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def dart_get(url: str, params: dict, timeout: int = 15, retries: int = 2, backoff: float = 2.0):
    """DART 서버와의 연결이 가끔 일시적으로 끊기는 경우를 대비해 짧게 재시도한다.
    호스팅 환경(Streamlit Cloud 등)에 따라 국내 공공기관 사이트 접속이 간헐적으로
    지연/차단되는 경우가 있어, 완전히 실패하기 전에 몇 번 더 시도해본다."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff)
    raise last_exc

st.set_page_config(page_title="동종업계 대손충당금 설정률 비교", layout="wide")

# ----------------------------------------------------------------------
# 사전 정의된 동종업종 그룹 (필요시 자유롭게 커스텀 입력도 가능)
# ----------------------------------------------------------------------
PRESET_GROUPS = {
    "5대 금융지주": ["KB금융지주", "신한지주", "하나금융지주", "우리금융지주", "농협금융지주"],
    "편의점/유통 3사": ["GS리테일", "BGF리테일", "이마트"],
    "직접 입력": [],
}


def get_api_key():
    """DART API 키를 여러 방식으로 찾아본다.
    - Streamlit Cloud: Secrets 관리 화면에 등록하면 st.secrets로 읽힘
    - Hugging Face Spaces 등: "Repository secrets/Variables"에 등록하면 환경변수로 주입됨
    - 둘 다 없으면 사이드바에서 직접 입력받음
    """
    key = ""
    try:
        if hasattr(st, "secrets"):
            key = st.secrets.get("DART_API_KEY", "")
    except Exception:
        key = ""
    if not key:
        key = os.environ.get("DART_API_KEY", "")
    if not key:
        key = st.sidebar.text_input("DART API 키 (Secrets에 등록 안 했다면 여기 직접 입력)", type="password")
    return key


@st.cache_data(ttl=60 * 60 * 24, show_spinner="기업 코드 목록 불러오는 중...")
def load_corp_code_map(api_key: str):
    """DART에 등록된 모든 회사(corp_name -> corp_code)를 돌려준다.

    이전 버전은 상장사(stock_code가 있는 회사)만 포함했는데, 신한카드·롯데카드처럼
    금융지주·그룹의 100% 자회사라 증권시장에 따로 상장돼 있지 않지만 사채 발행 등으로
    DART에 사업보고서를 내는 회사들이 통째로 빠지는 문제가 있었다. 그래서 이제는
    상장 여부와 무관하게 이름이 있는 모든 회사를 corp_map에 담고, 상장사 목록은
    별도 set(listed_codes)로 같이 돌려준다 — 업종 자동탐색처럼 "상장사만" 필요한
    기능에서는 이 listed_codes로 범위를 좁혀 쓰면 된다."""
    resp = dart_get(f"{BASE_URL}/corpCode.xml", {"crtfc_key": api_key}, timeout=20, retries=2)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)
    name_to_code = {}
    listed_codes = set()
    for item in root.findall("list"):
        corp_name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if corp_name and corp_code:
            name_to_code[corp_name] = corp_code
            if stock_code:
                listed_codes.add(corp_code)
    return name_to_code, listed_codes


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def fetch_full_financials(api_key: str, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str) -> list:
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div,
    }
    resp = dart_get(f"{BASE_URL}/fnlttSinglAcntAll.json", params, timeout=15, retries=2, backoff=1.5)
    data = resp.json()
    if data.get("status") != "000":
        return []
    return data.get("list", [])


def parse_amount(raw):
    if raw is None:
        return None
    cleaned = str(raw).replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def sum_matching(accounts, sj_div, patterns):
    """여러 하위 계정으로 나뉠 수 있는 항목(매출채권, 대손충당금 등)을 모두 더한다.
    두 번째 반환값은 실제로 어떤 계정명·금액이 합산에 포함됐는지 보여주는 상세 내역
    (구성 내역 투명성 확보용 — 감사 관점에서 "왜 이 숫자가 나왔는지" 바로 확인 가능)."""
    total, matched_details = 0, []
    for a in accounts:
        if a.get("sj_div") != sj_div:
            continue
        name = a.get("account_nm", "")
        if any(p in name for p in patterns):
            amt = parse_amount(a.get("thstrm_amount"))
            if amt:
                total += abs(amt)
                matched_details.append((name, amt))
    return (total, matched_details) if matched_details else (None, [])


def get_single(accounts, sj_divs, patterns):
    """자산총계·자본총계·당기순이익·매출액처럼 보통 하나만 존재하는 항목의 당기/전기 금액을 가져온다."""
    for a in accounts:
        if a.get("sj_div") not in sj_divs:
            continue
        name = a.get("account_nm", "")
        if any(p in name for p in patterns):
            cur = parse_amount(a.get("thstrm_amount"))
            prev = parse_amount(a.get("frmtrm_amount"))
            if cur is not None:
                return cur, prev
    return None, None


def get_rcept_no(accounts):
    for a in accounts:
        rn = a.get("rcept_no")
        if rn:
            return rn
    return None


def extract_full_metrics(company: str, accounts: list) -> dict:
    result = {
        "company": company, "method": None, "base_amount": None, "provision_amount": None,
        "ratio": None, "note": "", "rcept_no": None, "dart_link": None,
        "total_assets": None, "total_equity": None, "net_income": None,
        "equity_ratio": None, "roa": None, "receivable_turnover": None,
        "base_components": [], "provision_components": [],
    }
    if not accounts:
        result["note"] = "재무제표 데이터 없음"
        return result

    result["rcept_no"] = get_rcept_no(accounts)
    if result["rcept_no"]:
        result["dart_link"] = DART_VIEWER_URL.format(rcept_no=result["rcept_no"])

    # --- 대손충당금 설정률 (방식 A/B/C 자동 판별) ---
    # "대출채권류" 계정명은 회사마다 제각각이다. 은행은 보통 "대출채권"을 쓰지만,
    # 카드사·캐피탈사는 실제로 확인해보니(예: 롯데카드 2024년 현황 공시) "카드자산"/
    # "할부금융자산"/"리스자산"/"신기술금융자산"/"여신성금융자산" 등으로 잡힌다.
    LOAN_PATTERNS = ["대출채권", "대출금", "카드채권", "신용카드채권", "할부금융자산",
                     "리스채권", "여신금융자산", "여신채권", "카드자산", "리스자산",
                     "신기술금융자산", "여신성금융자산"]
    # "손실충당금"을 넓게 잡아두면 "신용손실충당금"뿐 아니라, 일부 회사가 쓰는
    # "신용" 접두어 없는 단순 "손실충당금" 표기도 놓치지 않는다.
    PROVISION_PATTERNS = ["신용손실충당금", "손실충당금", "신용손실에대한손상차손", "대손상각", "손상차손"]
    ALLOWANCE_PATTERNS = ["대손충당금", "손실충당금"]

    receivable, receivable_detail = sum_matching(accounts, "BS", ["매출채권"])
    allowance, allowance_detail = sum_matching(accounts, "BS", ALLOWANCE_PATTERNS)
    loans, loans_detail = sum_matching(accounts, "BS", LOAN_PATTERNS)
    provision, provision_detail = sum_matching(accounts, "CIS", PROVISION_PATTERNS)

    if receivable and allowance:
        result.update(method="A. 대손충당금(잔액)/매출채권(잔액)", base_amount=receivable,
                       provision_amount=allowance, ratio=round(allowance / receivable * 100, 3),
                       note="정상 산출")
        result["base_components"] = receivable_detail
        result["provision_components"] = allowance_detail
    elif loans and provision:
        result.update(method="B. 신용손실충당금 신규적립액/대출채권(잔액) [금융회사형]",
                       base_amount=loans, provision_amount=provision,
                       ratio=round(provision / loans * 100, 3), note="정상 산출 (은행/금융지주 방식)")
        result["base_components"] = loans_detail
        result["provision_components"] = provision_detail
    elif loans and allowance:
        # 일부 카드사·캐피탈사는 IFRS9(기대신용손실모형) 도입 이후에도 계정과목명 자체는
        # 관행적으로 "대손충당금"을 그대로 쓴다 (신용손실충당금이라는 이름으로 안 바뀜).
        # 이 경우 방식 A(매출채권 기준)도, 방식 B(신용손실충당금 기준)도 안 맞아서 놓치고 있었음.
        result.update(method="C. 대손충당금(잔액)/대출채권류(잔액) [카드사 등 혼합형]",
                       base_amount=loans, provision_amount=allowance,
                       ratio=round(allowance / loans * 100, 3),
                       note="정상 산출 (일부 카드사·캐피탈사는 '대손충당금' 계정명을 그대로 사용)")
        result["base_components"] = loans_detail
        result["provision_components"] = allowance_detail
    elif receivable and not allowance:
        result.update(base_amount=receivable, note="매출채권은 찾았으나 대손충당금 별도 계정 미발견 (주석 원문 확인 필요)")
        result["base_components"] = receivable_detail
    elif loans and not provision:
        result.update(base_amount=loans, note="대출채권(류)은 찾았으나 대손충당금/신용손실충당금 항목 미발견 (주석 원문 확인 필요)")
        result["base_components"] = loans_detail
    else:
        result["note"] = "매출채권/대출채권(류) 계정 자체를 찾지 못함 (업종 특성상 해당 없음 가능)"

    # --- 교차검증 지표 (기능 2) : 모든 회사 공통 ---
    total_assets, _ = get_single(accounts, ["BS"], ["자산총계"])
    total_equity, _ = get_single(accounts, ["BS"], ["자본총계"])
    net_income, _ = get_single(accounts, ["CIS", "IS"], ["당기순이익"])

    result["total_assets"] = total_assets
    result["total_equity"] = total_equity
    result["net_income"] = net_income
    if total_assets:
        if total_equity is not None:
            result["equity_ratio"] = round(total_equity / total_assets * 100, 2)
        if net_income is not None:
            result["roa"] = round(net_income / total_assets * 100, 2)

    # --- 매출채권회전율 (일반기업형에서만, 방식 A인 경우) ---
    if result["method"] and result["method"].startswith("A."):
        revenue, _ = get_single(accounts, ["IS", "CIS"], ["매출액", "수익(매출액)", "영업수익"])
        rec_cur, rec_prev = None, None
        for a in accounts:
            if a.get("sj_div") == "BS" and "매출채권" in a.get("account_nm", ""):
                rec_cur = parse_amount(a.get("thstrm_amount"))
                rec_prev = parse_amount(a.get("frmtrm_amount"))
                break
        if revenue and rec_cur:
            avg_rec = (rec_cur + rec_prev) / 2 if rec_prev else rec_cur
            if avg_rec:
                result["receivable_turnover"] = round(revenue / avg_rec, 2)

    return result


def normalize_company_name(name: str) -> str:
    """비교용으로 이름을 정규화: (주)/㈜/주식회사 표기와 공백·특수문자를 제거하고,
    영문 대소문자 차이(KB vs kb 등)도 같은 것으로 취급한다."""
    name = re.sub(r"\(주\)|㈜|주식회사", "", name)
    name = re.sub(r"[\s\-_.]", "", name)
    return name.strip().casefold()


@st.cache_data(show_spinner=False)
def build_normalized_index(corp_map: dict) -> dict:
    """정규화된 이름 -> 공식 명칭. 정규화 시 이름이 겹치면 더 짧은(더 일반적인) 쪽을 우선한다."""
    norm_index = {}
    for official in corp_map.keys():
        norm = normalize_company_name(official)
        if norm not in norm_index or len(official) < len(norm_index[norm]):
            norm_index[norm] = official
    return norm_index


def resolve_company(user_input: str, corp_map: dict, norm_index: dict):
    """사용자가 입력한 회사명이 DART 정식 명칭과 정확히 같지 않아도 최대한 찾아준다.
    반환: (매칭된 공식 명칭 또는 None, corp_code 또는 None, 매칭 방식 설명)
    """
    norm_input = normalize_company_name(user_input)

    # 1) 정규화 후 완전 일치 (예: "KB금융지주" == "(주)KB금융지주")
    if norm_input in norm_index:
        official = norm_index[norm_input]
        match_type = "정확히 일치" if official == user_input else f"표기 차이만 있음 (정식명: {official})"
        return official, corp_map[official], match_type

    # 2) 부분 일치: 입력이 정식명에 포함되거나, 정식명이 입력에 포함되는 경우
    candidates = [norm for norm in norm_index if norm_input in norm or norm in norm_input]
    if candidates:
        best_norm = min(candidates, key=len)  # 가장 짧은(가장 근접한) 후보 선택
        official = norm_index[best_norm]
        return official, corp_map[official], f"부분 일치 (정식명: {official})"

    # 3) 유사도 기반 퍼지 매칭 (오타 등). "리드" vs "우리카드"처럼 짧은 이름끼리
    #    우연히 글자가 겹쳐 엉뚱하게 매칭되는 걸 막기 위해, 기준을 0.75로 올리고
    #    길이가 너무 차이 나는 후보는 애초에 제외한다.
    length_ok = [n for n in norm_index if min(len(n), len(norm_input)) / max(len(n), len(norm_input), 1) >= 0.6]
    close = difflib.get_close_matches(norm_input, length_ok, n=1, cutoff=0.75)
    if close:
        official = norm_index[close[0]]
        score = difflib.SequenceMatcher(None, norm_input, close[0]).ratio()
        return official, corp_map[official], f"유사 매칭, 유사도 {score:.0%} (정식명: {official}) — 잘못 매칭된 것 같으면 정확한 명칭으로 다시 입력해주세요"

    return None, None, None


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def build_industry_index(api_key: str, corp_map: dict, time_budget_sec: int = 90) -> dict:
    """DART Open API엔 '업종별 회사 목록' 조회가 따로 없어서, 상장사 전체의 company.json을
    한 번 훑어 corp_code -> (corp_name, induty_code) 색인을 직접 만든다.
    결과는 30일간 캐시되므로, 이후 요청부터는 즉시 조회된다.

    주의: 이전 버전은 ThreadPoolExecutor.map()을 써서, 먼저 제출된 요청 하나가 느려지면
    뒤에 이미 끝난 결과까지 전부 막혀서 기다리는(head-of-line blocking) 문제가 있었다.
    as_completed()로 끝나는 대로 바로 처리하고, 시간 예산(기본 90초)을 넘기면 그때까지
    모은 결과만으로 진행한다 — 무한정 기다리지 않는다."""

    def fetch_one(name_code):
        name, code = name_code
        try:
            resp = requests.get(f"{BASE_URL}/company.json",
                                 params={"crtfc_key": api_key, "corp_code": code}, timeout=4)
            data = resp.json()
            if data.get("status") == "000" and data.get("induty_code"):
                return code, name, data.get("induty_code")
        except Exception:
            pass
        return code, name, None

    items = list(corp_map.items())
    index = {}
    progress = st.progress(0.0, text=f"업종 정보 색인 생성 중... (0/{len(items)}건, 처음 1회만 필요해요)")
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=40)
    futures = {ex.submit(fetch_one, item): item for item in items}
    start = time.monotonic()
    done = 0
    try:
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            try:
                code, name, induty = fut.result()
                if induty:
                    index[code] = (name, induty)
            except Exception:
                pass
            elapsed = time.monotonic() - start
            if done % 20 == 0 or done == len(items):
                progress.progress(min(done / len(items), 1.0),
                                   text=f"업종 정보 색인 생성 중... ({done}/{len(items)}건, {elapsed:.0f}초 경과)")
            if elapsed > time_budget_sec:
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        progress.empty()

    if len(index) < len(items):
        st.caption(f"⏱ 시간 제한으로 상장사 {len(items)}곳 중 {len(index)}곳까지만 색인했습니다. "
                    f"동종업계가 안 잡히면 잠시 후 다시 시도하면 캐시가 이어서 채워질 수 있어요.")
    return index


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def fetch_induty_code(api_key: str, corp_code: str):
    """대상 회사 1곳의 업종코드만 조회한다 (상장 여부와 무관하게 동작 — 신한카드처럼
    비상장이라 industry_index 스캔 대상에 없는 회사도 자기 업종코드는 알아낼 수 있다)."""
    try:
        resp = dart_get(f"{BASE_URL}/company.json", {"crtfc_key": api_key, "corp_code": corp_code},
                         timeout=10, retries=1)
        data = resp.json()
        if data.get("status") == "000":
            return data.get("induty_code")
    except Exception:
        pass
    return None


def find_industry_peers(induty_code: str, industry_index: dict, exclude_code: str, max_peers: int = 4):
    """같은 업종코드(표준산업분류)를 쓰는 다른 '상장' 회사들을 찾는다.
    industry_index는 상장사만 스캔해 만들어지므로, 대상 회사 자신이 비상장이어도
    (예: 신한카드) 업종코드만 알면 상장된 동종업계를 찾을 수 있다."""
    if not induty_code:
        return []
    peers = [name for code, (name, ind) in industry_index.items()
             if ind == induty_code and code != exclude_code]
    return sorted(peers)[:max_peers]


def flag_outliers(df: pd.DataFrame, value_col="ratio", z_threshold=1.5):
    df = df.copy()
    valid = df[value_col].notna()
    if valid.sum() < 2:
        df["peer_mean"] = None
        df["z_score"] = None
        df["flag"] = ""
        return df
    mean = df.loc[valid, value_col].mean()
    std = df.loc[valid, value_col].std(ddof=0)
    df["peer_mean"] = round(mean, 3)
    df["z_score"] = df[value_col].apply(lambda r: round((r - mean) / std, 2) if pd.notna(r) and std else None)
    df["flag"] = df["z_score"].apply(
        lambda z: "⚠ 이상치 후보" if z is not None and abs(z) >= z_threshold
        else ("△ 근접" if z is not None and abs(z) >= 1.0 else "")
    )
    return df


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
st.title("동종업계 대손충당금 설정률 비교 웹툴")
st.caption("동종업종 기업의 대손충당금 관련 지표를 DART Open API로 실시간 수집해 비교하고, 이상치를 자동으로 찾아줍니다.")

api_key = get_api_key()

mode = st.radio("입력 방식", ["여러 회사 직접 입력", "회사 1곳 → 동종업계 자동 탐색"], horizontal=True)

single_company, max_peers = None, 4
col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
with col1:
    if mode == "여러 회사 직접 입력":
        group = st.selectbox("동종업종 그룹 선택", list(PRESET_GROUPS.keys()))
        default_companies = ", ".join(PRESET_GROUPS[group])
        companies_input = st.text_area("비교할 회사명 (쉼표로 구분, 자유롭게 수정 가능)", value=default_companies, height=80)
    else:
        single_company = st.text_input("기준 회사명 (1곳만 입력)", value="삼성전자")
        max_peers = st.slider("동종업계 몇 개사와 비교할지", min_value=2, max_value=8, value=4)
        st.caption("같은 업종코드(표준산업분류)를 쓰는 상장사를 자동으로 찾아 비교합니다.")
        companies_input = ""
with col2:
    base_year = st.selectbox("기준 사업연도", ["2025", "2024", "2023", "2022"], index=0)
with col3:
    n_years = st.slider("추이 조회 기간(개년)", min_value=1, max_value=5, value=3)
with col4:
    fs_div = st.selectbox("재무제표 기준", ["CFS(연결)", "OFS(별도)"])
    fs_div_code = "CFS" if fs_div.startswith("CFS") else "OFS"

run = st.button("분석 실행", type="primary", use_container_width=False)

if run:
    if not api_key:
        st.error("DART API 키가 필요합니다. 왼쪽 사이드바에 입력하거나 Secrets에 DART_API_KEY로 등록해주세요.")
        st.stop()

    try:
        corp_map, listed_codes = load_corp_code_map(api_key)
    except requests.exceptions.ConnectTimeout as e:
        st.error(
            "DART 서버 연결이 시간 초과되었습니다 (연결 자체가 안 되는 상태).\n\n"
            "**원인 추정**: DART 서버 자체는 정상 작동 중인 것으로 확인됩니다. "
            "이런 '연결 시간 초과'는 보통 일시적인 네트워크 혼잡이거나, "
            "호스팅 서버(클라우드) 쪽에서 국내 공공기관 사이트로의 접속이 간헐적으로 지연되는 경우에 발생합니다.\n\n"
            "**해결 방법**:\n"
            "1. 아래 '분석 실행' 버튼을 다시 눌러 재시도해보세요 (자동으로 몇 초 간격을 두고 재시도하도록 이미 반영되어 있습니다).\n"
            "2. 계속 실패하면 잠시 후(몇 분~몇십 분 뒤) 다시 시도해보세요.\n"
            "3. 그래도 계속 안 되면 알려주세요 — 다른 호스팅 방식을 검토해볼 수 있습니다."
        )
        st.stop()
    except requests.exceptions.RequestException as e:
        st.error(
            f"기업 코드 목록을 불러오는 중 네트워크 오류가 발생했습니다: {e}\n\n"
            "잠시 후 '분석 실행' 버튼을 다시 눌러 재시도해주세요."
        )
        st.stop()
    except Exception as e:
        st.error(f"기업 코드 목록을 불러오지 못했습니다: {e}")
        st.stop()

    norm_index = build_normalized_index(corp_map)

    if mode == "여러 회사 직접 입력":
        companies = [c.strip() for c in companies_input.split(",") if c.strip()]
        if not companies:
            st.warning("비교할 회사명을 최소 2개 이상 입력해주세요.")
            st.stop()
    else:
        if not single_company or not single_company.strip():
            st.warning("기준 회사명을 입력해주세요.")
            st.stop()
        target_official, target_code, target_match_type = resolve_company(single_company.strip(), corp_map, norm_index)
        if not target_code:
            st.error(f"'{single_company}'와(과) 비슷한 회사를 DART에서 찾지 못했습니다. 표기를 확인해주세요.")
            st.stop()
        if target_official != single_company.strip():
            st.caption(f"🔎 '{single_company}' → '{target_official}'로 인식했습니다 ({target_match_type}).")

        # 대상 회사의 업종코드는 상장 여부와 무관하게 조회 가능 (신한카드·롯데카드 같은
        # 비상장 카드사도 자기 업종코드는 알아낼 수 있음)
        induty_code = fetch_induty_code(api_key, target_code)
        if not induty_code:
            st.warning(f"'{target_official}'의 업종코드 정보를 DART에서 찾지 못해 동종업계 자동 탐색을 할 수 없습니다. "
                       f"'여러 회사 직접 입력' 방식으로 비교해주세요.")
            st.stop()

        # 동종업계 스캔(업종 색인)은 시간이 걸리므로 "상장사"로만 범위를 좁힌다.
        listed_corp_map = {name: code for name, code in corp_map.items() if code in listed_codes}
        industry_index = build_industry_index(api_key, listed_corp_map)
        peer_names = find_industry_peers(induty_code, industry_index, exclude_code=target_code, max_peers=max_peers)

        if not peer_names:
            is_target_listed = target_code in listed_codes
            extra = (
                "이 회사 자체가 비상장이고, 신용카드사처럼 같은 업종의 다른 회사들도 대부분 금융지주의 "
                "비상장 자회사라 '상장사 기준' 자동 탐색 범위에는 동종업계가 안 잡히는 경우가 많아요. "
                if not is_target_listed else
                "시간 제한 때문에 상장사 전체를 다 훑지 못했을 수도 있어요. 다시 실행하면 캐시가 이어서 채워질 수 있습니다. "
            )
            st.warning(f"'{target_official}'과(와) 같은 업종코드({induty_code})의 다른 상장사를 찾지 못했습니다. "
                       + extra +
                       "이런 경우 '여러 회사 직접 입력' 모드에서 비교하고 싶은 회사명을 직접 나열해주세요 "
                       "(예: 신한카드, 삼성카드, KB국민카드, 하나카드, 우리카드, 롯데카드).")
            st.stop()

        companies = [target_official] + peer_names
        st.caption(f"🔍 '{target_official}' 기준 업종코드 {induty_code}의 동종업계(상장사) {len(peer_names)}곳을 찾았습니다: "
                   + ", ".join(peer_names))

    years = [str(int(base_year) - i) for i in range(n_years)]  # 예: 2025,2024,2023

    # 회사명 매칭은 연도와 무관하게 1번만 수행
    resolved = {}
    match_notes = []
    for company in companies:
        official, corp_code, match_type = resolve_company(company, corp_map, norm_index)
        resolved[company] = (official, corp_code)
        if official and official != company:
            match_notes.append(f"'{company}' → '{official}' ({match_type})")
        elif not official:
            match_notes.append(f"'{company}' → 매칭 실패")
    if match_notes:
        st.caption("🔎 회사명 매칭 결과: " + " · ".join(match_notes))

    all_rows = []
    components_map = {}  # (company, year) -> {"base": [...], "provision": [...]} — 구성 내역(방식 B 등 계정명 breakdown)
    total_steps = len(companies) * len(years)
    progress = st.progress(0.0, text="데이터 수집 중...")
    step = 0
    for company in companies:
        official, corp_code = resolved[company]
        for yr in years:
            step += 1
            if not corp_code:
                all_rows.append({"company": company, "year": yr, "method": None, "ratio": None,
                                  "note": "유사한 회사명을 DART에서 찾지 못함 (표기를 다시 확인해주세요)"})
            else:
                try:
                    accounts = fetch_full_financials(api_key, corp_code, yr, "11011", fs_div_code)
                    row = extract_full_metrics(official, accounts)
                except requests.exceptions.RequestException:
                    row = {"company": official, "method": None, "ratio": None,
                           "note": "DART 서버 연결 문제로 이 회사/연도 데이터를 가져오지 못했습니다 (재시도했지만 실패). "
                                   "'분석 실행'을 다시 눌러 재시도해보세요."}
                except Exception as e:
                    row = {"company": official, "method": None, "ratio": None,
                           "note": f"데이터 처리 중 오류: {e}"}
                row["year"] = yr
                row["input_name"] = company
                # 구성 내역은 panel(표/CSV용) 밖으로 따로 빼서 보관 — 리스트가 그대로 셀에 들어가면
                # 표 렌더링/CSV 저장이 지저분해지므로, 화면엔 별도 펼침(expander)으로 보여준다.
                base_comp = row.pop("base_components", [])
                prov_comp = row.pop("provision_components", [])
                if base_comp or prov_comp:
                    components_map[(official or company, yr)] = {"base": base_comp, "provision": prov_comp}
                all_rows.append(row)
            progress.progress(step / total_steps, text=f"{official or company} {yr}년 처리 완료")
    progress.empty()

    panel = pd.DataFrame(all_rows)

    # 최신 연도 스냅샷 (동종업체 비교용)
    latest = panel[panel["year"] == base_year].copy()
    latest = flag_outliers(latest, value_col="ratio")

    st.subheader(f"{base_year}년 동종업체 비교")
    valid_latest = latest[latest["ratio"].notna()].sort_values("ratio", ascending=False)

    if not valid_latest.empty:
        chart_df = valid_latest.copy()
        chart_df["색상"] = chart_df["flag"].apply(
            lambda f: "이상치 후보" if f == "⚠ 이상치 후보" else ("근접" if f == "△ 근접" else "정상")
        )
        bar = alt.Chart(chart_df).mark_bar().encode(
            x=alt.X("company:N", sort="-y", title=None),
            y=alt.Y("ratio:Q", title="비율 (%)"),
            color=alt.Color("색상:N",
                             scale=alt.Scale(domain=["정상", "근접", "이상치 후보"],
                                             range=["#2a78d6", "#fab219", "#ec835a"]),
                             legend=alt.Legend(title=None)),
            tooltip=["company", "method", "ratio", "peer_mean", "z_score",
                     "equity_ratio", "roa", "receivable_turnover", "note"]
        ).properties(height=380)
        st.altair_chart(bar, use_container_width=True)
    else:
        st.info("자동 산출 가능한 회사가 없습니다. 아래 표에서 사유를 확인해주세요.")

    # ---------------- 기능 2: 교차검증 지표 표 ----------------
    st.markdown("**교차검증 지표** — 설정률만으로 판단이 안 설 때, 자기자본비율·ROA·매출채권회전율을 같이 확인하세요.")
    display_cols = ["company", "method", "ratio", "peer_mean", "z_score", "flag",
                     "equity_ratio", "roa", "receivable_turnover", "note"]
    col_config = {
        "company": "회사", "method": "산출 방식", "ratio": st.column_config.NumberColumn("설정률(%)", format="%.3f"),
        "peer_mean": st.column_config.NumberColumn("동종평균(%)", format="%.3f"),
        "z_score": "z-score", "flag": "이상치 표시",
        "equity_ratio": st.column_config.NumberColumn("자기자본비율(%)", format="%.2f"),
        "roa": st.column_config.NumberColumn("ROA(%)", format="%.2f"),
        "receivable_turnover": st.column_config.NumberColumn("매출채권회전율(회)", format="%.2f"),
        "note": "비고",
    }
    if latest["dart_link"].notna().any():
        display_cols.append("dart_link")
        col_config["dart_link"] = st.column_config.LinkColumn("DART 원문", display_text="공시 보기 →")

    st.dataframe(latest[display_cols], use_container_width=True, hide_index=True, column_config=col_config)

    # ---------------- 계정 구성 내역 (특히 금융회사형에서 "대출채권류"가 어떤 계정들의 합인지 투명하게 공개) ----------------
    base_year_components = {name: comp for (name, yr), comp in components_map.items() if yr == base_year}
    if base_year_components:
        with st.expander(f"📋 {base_year}년 계정 구성 내역 보기 (어떤 하위 계정이 합산됐는지)"):
            st.caption(
                "카드사·캐피탈사는 '대출채권'이 아니라 '카드채권'/'할부금융자산'/'리스채권' 등으로 "
                "잡히는 경우가 많아, 여러 계정명을 넓게 훑어 합산합니다. 아래에서 실제로 어떤 계정이 "
                "합산에 포함됐는지 확인해 계산이 타당한지 점검할 수 있습니다."
            )
            for name, comp in base_year_components.items():
                st.markdown(f"**{name}**")
                col_a, col_b = st.columns(2)
                with col_a:
                    st.caption("분모 계정 (매출채권/대출채권류)")
                    if comp["base"]:
                        st.dataframe(
                            pd.DataFrame(comp["base"], columns=["계정명", "금액"]),
                            hide_index=True, use_container_width=True,
                            column_config={"금액": st.column_config.NumberColumn("금액", format="%d")},
                        )
                    else:
                        st.caption("(내역 없음)")
                with col_b:
                    st.caption("분자 계정 (대손충당금/신용손실충당금류)")
                    if comp["provision"]:
                        st.dataframe(
                            pd.DataFrame(comp["provision"], columns=["계정명", "금액"]),
                            hide_index=True, use_container_width=True,
                            column_config={"금액": st.column_config.NumberColumn("금액", format="%d")},
                        )
                    else:
                        st.caption("(내역 없음)")

    # ---------------- 기능 3: 연도별 추이 ----------------
    st.subheader("연도별 추이")
    trend_df = panel[panel["ratio"].notna()].copy()
    if not trend_df.empty and trend_df["year"].nunique() > 1:
        line = alt.Chart(trend_df).mark_line(point=True).encode(
            x=alt.X("year:O", title="사업연도"),
            y=alt.Y("ratio:Q", title="비율 (%)"),
            color=alt.Color("company:N", legend=alt.Legend(title=None)),
            tooltip=["company", "year", "ratio", "method"]
        ).properties(height=340)
        st.altair_chart(line, use_container_width=True)

        # 전년 대비 급변동 탐지
        trend_df = trend_df.sort_values(["company", "year"])
        trend_df["yoy_change"] = trend_df.groupby("company")["ratio"].diff()
        big_moves = trend_df.dropna(subset=["yoy_change"])
        if not big_moves.empty:
            std = big_moves["yoy_change"].std(ddof=0)
            if std:
                big_moves = big_moves.copy()
                big_moves["변동 z-score"] = round((big_moves["yoy_change"] - big_moves["yoy_change"].mean()) / std, 2)
                flagged = big_moves[big_moves["변동 z-score"].abs() >= 1.5]
                if not flagged.empty:
                    st.markdown("**⚠ 전년 대비 급변동 감지된 회사·연도**")
                    st.dataframe(
                        flagged[["company", "year", "ratio", "yoy_change", "변동 z-score"]].rename(
                            columns={"company": "회사", "year": "연도", "ratio": "설정률(%)",
                                     "yoy_change": "전년대비 변화(%p)"}),
                        use_container_width=True, hide_index=True,
                    )
    else:
        st.caption("추이를 그리려면 '추이 조회 기간'을 2개년 이상으로 늘려주세요.")

    csv = panel.to_csv(index=False).encode("utf-8-sig")
    st.download_button("전체 결과(모든 연도) CSV 다운로드", csv, file_name=f"bad_debt_ratio_{base_year}_{n_years}y.csv", mime="text/csv")

    st.caption(
        "⚠ 대손충당금 설정률은 재무제표에 잔액이 별도 계정으로 태깅되지 않은 기업이 많아, 가능한 경우 방식A(잔액/잔액), "
        "불가능하면 방식B(당기 신규 적립액/대출채권, 은행형)를 자동으로 적용합니다. 자기자본비율·ROA·매출채권회전율은 "
        "설정률 하나만으로 판단하기 어려운 부분을 보완하기 위한 교차검증용 지표이며, z-score와 급변동 표시는 결론이 아니라 "
        "추가 검토가 필요한 대상을 찾기 위한 1차 스크리닝 결과입니다."
    )
else:
    st.info("왼쪽에서 비교할 회사·기준연도·조회기간을 선택한 뒤 '분석 실행' 버튼을 눌러주세요.")
