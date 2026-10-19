-- 코스닥 상장사 심사참고 DB (SQLite)
-- 원칙: 원천(raw) 테이블은 출처 그대로, 파생(kpi/summary) 테이블은 재계산 가능하게 분리


-- 1. 개요 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company (
    corp_code     TEXT PRIMARY KEY,        -- DART 고유번호(8자리)
    stock_code    TEXT UNIQUE,             -- 종목코드(6자리)
    corp_name     TEXT NOT NULL,
    market        TEXT,                    -- KOSDAQ / KOSPI / KONEX
    sector_krx    TEXT,                    -- KRX 업종분류
    induty_code   TEXT,                    -- DART 업종코드(KSIC)
    ceo_nm        TEXT,
    est_dt        TEXT,                    -- 설립일 YYYYMMDD
    listed_dt     TEXT,                    -- 상장일
    acc_mt        TEXT,                    -- 결산월
    adres         TEXT,
    hm_url        TEXT,
    phn_no        TEXT,
    jurir_no      TEXT,                    -- 법인등록번호
    bizr_no       TEXT,                    -- 사업자등록번호
    updated_at    TEXT,
    sect_tp       TEXT,                    -- KRX 소속부(우량/벤처/중견/기술성장 등)
    corp_name_full TEXT,                   -- DART 정식 회사명
    parval        REAL                     -- 액면가(원), KRX
);

-- 감사의견(사업보고서 "회계감사인의 명칭 및 감사의견")
CREATE TABLE IF NOT EXISTS audit_opinion (
    corp_code     TEXT,
    bsns_year     TEXT,
    adtor         TEXT,                    -- 감사인
    adt_opinion   TEXT,                    -- 적정/한정/부적정/의견거절
    adt_reprt_spcmnt_matter TEXT,          -- 강조사항 등
    emphs_matter  TEXT,
    core_adt_matter TEXT,                  -- 핵심감사사항
    PRIMARY KEY (corp_code, bsns_year)
);

-- 최대주주 및 특수관계인(사업보고서 기준)
CREATE TABLE IF NOT EXISTS shareholder (
    corp_code     TEXT,
    bsns_year     TEXT,
    nm            TEXT,
    relate        TEXT,
    stock_knd     TEXT,
    bsis_posesn_stock_co  INTEGER,
    trmend_posesn_stock_co INTEGER,
    trmend_posesn_stock_qota_rt REAL,
    PRIMARY KEY (corp_code, bsns_year, nm, stock_knd)
);

-- 5% 이상 주주(사업보고서 원문 '주식의 분포' 표)
CREATE TABLE IF NOT EXISTS major_holder (
    corp_code TEXT, bsns_year TEXT, nm TEXT, stock_co REAL, rate REAL,
    PRIMARY KEY (corp_code, bsns_year, nm)
);

-- 미상환 CB·BW (사업보고서)
CREATE TABLE IF NOT EXISTS conv_bond (
    corp_code TEXT, bsns_year TEXT, kind TEXT, name TEXT, isu_dd TEXT, exprd_dd TEXT,
    remndr_amt REAL,                      -- 미상환 잔액(원)
    cnvrs_prc REAL,                       -- 전환(행사)가액
    cnvrs_pd TEXT,                        -- 전환청구기간
    remndr_stock REAL,                    -- 전환가능 주식수
    PRIMARY KEY (corp_code, bsns_year, kind, name, isu_dd)
);

-- 타법인 출자현황 (사업보고서)
CREATE TABLE IF NOT EXISTS other_invest (
    corp_code TEXT, bsns_year TEXT, name TEXT, purpose TEXT, acqs_de TEXT,
    trmend_qota_rt REAL, trmend_book_amt REAL, recent_rev REAL, recent_ni REAL,
    PRIMARY KEY (corp_code, bsns_year, name)
);

-- 소액주주 현황(사업보고서)
CREATE TABLE IF NOT EXISTS minor_shareholder (
    corp_code     TEXT,
    bsns_year     TEXT,
    shrholdr_co   REAL,                    -- 소액주주 수
    hold_stock_co REAL,                    -- 보유주식수
    hold_stock_rate REAL,                  -- 지분율 %
    shrholdr_rate REAL,                    -- 주주수 비율 %
    PRIMARY KEY (corp_code, bsns_year)
);

-- 2. 사업내용 -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS business (
    corp_code     TEXT,
    bsns_year     TEXT,
    rcept_no      TEXT,                    -- 사업보고서 접수번호
    reprt_code    TEXT,                    -- 11011 사업 / 11012 반기 / 11013 1Q / 11014 3Q
    overview_text TEXT,                    -- "1. 사업의 개요" 원문
    full_text     TEXT,                    -- "II. 사업의 내용" 전체 원문
    char_len      INTEGER,
    summary_json  TEXT,                    -- LLM 정형요약(JSON) - 아래 summary 필드 참조
    summary_md    TEXT,                    -- 심사용 서술 요약
    summarized_at TEXT,
    fetched_at    TEXT,
    rating_text   TEXT,                    -- 사업보고서 '신용평가에 관한 사항' 원문(표 평탄화)
    PRIMARY KEY (corp_code, bsns_year)
);

-- 3. 재무현황 -----------------------------------------------------------
-- 원천: DART 단일회사 전체 재무제표(fnlttSinglAcntAll) long format
CREATE TABLE IF NOT EXISTS fin_raw (
    corp_code     TEXT,
    bsns_year     TEXT,
    reprt_code    TEXT,
    fs_div        TEXT,                    -- CFS 연결 / OFS 별도
    sj_div        TEXT,                    -- BS/IS/CIS/CF/SCE
    account_id    TEXT,
    account_nm    TEXT,
    thstrm_amount REAL,                    -- 당기
    frmtrm_amount REAL,                    -- 전기
    bfefrmtrm_amount REAL,                 -- 전전기
    ord           INTEGER,
    rcept_no      TEXT,                    -- 해당 보고서 접수번호
    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_nm)
);

-- 파생: 심사용 핵심계정(연도별 1행). fin_raw에서 재계산
CREATE TABLE IF NOT EXISTS fin_kpi (
    corp_code     TEXT,
    bsns_year     TEXT,
    reprt_code    TEXT,
    fs_div        TEXT,
    revenue       REAL,
    op_income     REAL,
    net_income    REAL,
    total_assets  REAL,
    total_liab    REAL,
    equity        REAL,
    cash          REAL,
    borrowings    REAL,                    -- 차입금(단기+장기+사채+리스) 근사
    cfo           REAL,                    -- 영업활동현금흐름
    debt_ratio    REAL,                    -- 부채비율 %
    op_margin     REAL,
    net_borrow_ratio REAL,                 -- 순차입금/자기자본 %
    net_borrowings REAL,                   -- 순차입금 = 총차입금 - 현금성자산
    da            REAL,                    -- 감가상각비+무형자산상각비(+사용권자산상각비), CF 간접법 조정항목
    ebitda        REAL,                    -- 영업이익 + da
    int_exp       REAL,                    -- 이자비용(금융비용)
    interest_cover REAL,                   -- EBITDA/이자비용 (배)
    borrow_dep    REAL,                    -- 차입금의존도 = 총차입금/총자산 %
    rcept_no      TEXT,                    -- 보고서 접수번호 (DART 링크용)
    cur_assets REAL, cur_liab REAL, receivables REAL, inventory REAL, capital REAL,
    cur_ratio     REAL,                    -- 유동비율 %
    recv_turn     REAL,                    -- 매출채권회전율(회, 기간매출/기말채권)
    inv_turn      REAL,                    -- 재고자산회전율(회)
    nd_ebitda     REAL,                    -- 순차입금/EBITDA (배)
    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div)
);

-- 4. 주가추이 -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_daily (
    stock_code    TEXT,
    dt            TEXT,                    -- YYYY-MM-DD
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (stock_code, dt)
);

-- 파생: 담보가치 판단용 요약(기준일 1행)
CREATE TABLE IF NOT EXISTS price_summary (
    stock_code    TEXT,
    asof          TEXT,
    close         REAL,
    shares_out    INTEGER,                 -- 발행주식수(DART stockTotqySttus)
    mcap          REAL,                    -- 시가총액
    high_52w REAL, low_52w REAL,
    chg_1m REAL, chg_3m REAL, chg_1y REAL, -- 수익률 %
    chg_1w REAL, chg_6m REAL, chg_2y REAL, chg_3y REAL,
    avg_vol_3m    REAL,                    -- 3개월 평균 거래량
    avg_value_3m  REAL,                    -- 3개월 평균 거래대금(원) - 담보처분 유동성
    vol_60d       REAL,                    -- 60일 변동성(연율 %)
    PRIMARY KEY (stock_code, asof)
);

-- 5. 리포트·공시 메타 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_meta (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT,
    source        TEXT,                    -- HK(한경컨센서스) / KIND_IR / NAVER ...
    source_id     TEXT,                    -- 출처 내 리포트 ID (HK: REPORT_IDX)
    title         TEXT,
    publisher     TEXT,                    -- 증권사
    analyst       TEXT,
    pub_dt        TEXT,                    -- YYYYMMDD
    opinion       TEXT,                    -- Buy/Hold/Sell 등 출처 표기 그대로
    old_opinion   TEXT,
    target_price  REAL,
    old_target_price REAL,
    eps_json      TEXT,                    -- 출처 제공 EPS 전망 {"2025":..,"2026":..}
    url           TEXT,                    -- 출처 페이지
    pdf_url       TEXT,
    local_path    TEXT,                    -- 개인 PC 보관 경로(사이트에는 미배포)
    fetched_at    TEXT,
    UNIQUE (source, source_id)
);

-- 리포트 LLM 분석 결과: 원문 저장 없이 정형 필드만
CREATE TABLE IF NOT EXISTS report_analysis (
    report_id     INTEGER PRIMARY KEY REFERENCES report_meta(id),
    stock_code    TEXT,
    forecast_json TEXT,                    -- [{"year":"2025","revenue":..,"op_income":..,"net_income":..}] 단위:원
    valuation_json TEXT,                   -- {"method":"PER","multiple":..,"base_eps":..,"base_year":..}
    points_json   TEXT,                    -- 투자포인트 [{"point":"..","evidence":".."}] 자체 재서술
    risks_json    TEXT,                    -- 리스크 ["..."]
    memo_md       TEXT,                    -- 심사용 3~6줄 메모(자체 서술)
    pages         INTEGER,
    analyzed_at   TEXT
);

-- 심사 관점 주요 공시 이벤트(DART 공시목록에서 키워드 필터)
CREATE TABLE IF NOT EXISTS disclosure_event (
    corp_code     TEXT,
    rcept_no      TEXT PRIMARY KEY,
    rcept_dt      TEXT,
    report_nm     TEXT,
    flag          TEXT                     -- 유상증자/CB/BW/감사의견/관리종목/최대주주변경/소송 등
);

-- 신용등급 이력(금감원 신용평가서 공시)
CREATE TABLE IF NOT EXISTS credit_rating (
    corp_code     TEXT,
    agency        TEXT,                    -- 한국신용평가/한국기업평가/나이스신용평가/서울신용평가
    target_type   TEXT,                    -- 기업신용평가 / 제n회 사채 / 전자단기사채 ...
    eval_kind     TEXT,                    -- 본평가/정기평가/수시평가(정정)
    disc_dt       TEXT,                    -- 공시일자 YYYY-MM-DD
    grade         TEXT,
    watch         TEXT,
    outlook       TEXT,
    fetched_at    TEXT,
    file_url      TEXT,                    -- 신용평가서 PDF 다운로드 링크
    PRIMARY KEY (corp_code, agency, target_type, disc_dt, grade)
);

-- 신용평가서 기반 사업 요약 (원문 미저장, 요약만)
CREATE TABLE IF NOT EXISTS rating_summary (
    corp_code     TEXT PRIMARY KEY,
    agency        TEXT,
    disc_dt       TEXT,
    grade         TEXT,
    file_url      TEXT,
    summary_json  TEXT,
    summary_md    TEXT,
    pages         INTEGER,
    summarized_at TEXT
);

-- 뉴스 헤드라인 (제목·언론사·날짜·링크만, 90일 보관)
CREATE TABLE IF NOT EXISTS news (
    corp_code TEXT, dt TEXT, title TEXT, source TEXT, url TEXT, fetched_at TEXT,
    PRIMARY KEY (corp_code, url)
);
CREATE INDEX IF NOT EXISTS idx_news_corp ON news(corp_code, dt DESC);

-- 수집 로그
CREATE TABLE IF NOT EXISTS fetch_log (
    corp_code TEXT, job TEXT, status TEXT, msg TEXT, at TEXT
);

CREATE INDEX IF NOT EXISTS idx_price_stock ON price_daily(stock_code, dt DESC);
CREATE INDEX IF NOT EXISTS idx_event_corp ON disclosure_event(corp_code, rcept_dt DESC);
