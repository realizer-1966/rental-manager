"""
임대관리 앱 DB 모델 및 초기화
SQLite + Flask, Termux 환경
"""
import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rental.db")


@contextmanager
def get_db_ctx():
    """컨텍스트 매니저 버전 (init_db 등에서 사용)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db():
    """Flask 라우트에서 직접 사용하는 버전 (커넥션을 반환)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
-- 관리자 사용자
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT DEFAULT 'admin',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 건물 (원룸건물, 오피스텔, 분양형호텔 등)
CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,          -- 원룸건물 / 오피스텔 / 분양형호텔 / 투룸건물
    address TEXT,
    floors INTEGER,
    total_units INTEGER,
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 호수 (건물 내 개별 방)
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL,
    unit_number TEXT NOT NULL,          -- 호수 (예: 101, 201, A-301)
    floor TEXT,                       -- 층 (숫자 또는 B1, B2 등 반지하)
    room_type TEXT,                     -- 원룸 / 투룸 / 전층 / 오피스
    area_pyeong REAL,                   -- 평수
    area_sqm REAL,                      -- 제곱미터
    deposit INTEGER DEFAULT 0,          -- 기본 보증금
    monthly_rent INTEGER DEFAULT 0,     -- 기본 월세
    management_fee INTEGER DEFAULT 0,   -- 기본 관리비
    status TEXT DEFAULT '공실',          -- 공실 / 임대중 / 수리중
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (building_id) REFERENCES buildings(id) ON DELETE CASCADE,
    UNIQUE(building_id, unit_number)
);

-- 임차인
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    id_number TEXT,             -- 주민/사업자번호
    emergency_contact TEXT,
    address TEXT,
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 임대차 계약
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    tenant_id INTEGER NOT NULL,
    contract_type TEXT DEFAULT '월세',   -- 월세 / 전세 / 반전세
    deposit INTEGER NOT NULL DEFAULT 0,
    monthly_rent INTEGER DEFAULT 0,
    management_fee INTEGER DEFAULT 0,
    cleaning_fee INTEGER DEFAULT 0,       -- 퇴실 청소비 (계약서 기재)
    extra_person_fee INTEGER DEFAULT 0,   -- 1인 추가 비용
    start_date TEXT NOT NULL,            -- 계약시작일
    end_date TEXT NOT NULL,              -- 계약종료일
    payment_day INTEGER DEFAULT 25,      -- 월세 납부일 (매월)
    original_payment_day INTEGER,        -- 최초 납부일 (변경 전, 정산용)
    payment_day_changed_date TEXT,        -- 납부일 변경일
    status TEXT DEFAULT 'active',        -- active / expired / terminated
    terminated_date TEXT,                -- 해지(퇴거)일
    renewal_alert_sent TEXT,             -- 만료 2개월전 알림 발송일 (NULL=미발송)
    deposit_return_alert_sent TEXT,      -- 보증금 반환 6개월전 알림 발송일 (NULL=미발송)
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (unit_id) REFERENCES units(id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- 납부일 변경 이력
CREATE TABLE IF NOT EXISTS payment_day_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    old_day INTEGER NOT NULL,
    new_day INTEGER NOT NULL,
    changed_date TEXT NOT NULL,          -- 변경 적용일
    reason TEXT,                         -- 변경 사유
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 보증금 분할 납부 이력
CREATE TABLE IF NOT EXISTS deposit_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,             -- 납부액
    paid_date TEXT NOT NULL,             -- 납부일
    payment_method TEXT,                -- 계좌이체 / 현금 / 카드
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 계량기 사진 기록 (입주/퇴거 시)
CREATE TABLE IF NOT EXISTS meter_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    type TEXT NOT NULL,                 -- electric / gas
    reading_type TEXT NOT NULL,         -- move_in / move_out
    reading_date TEXT NOT NULL,         -- 계량기 읽은 날짜
    meter_value REAL,                   -- 계량기 수치 (kWh 또는 m³)
    photo_path TEXT,                    -- 사진 파일 경로
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 공과금 청구 (한전/도시가스 문자 기반)
CREATE TABLE IF NOT EXISTS utility_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    type TEXT NOT NULL,                 -- electric / gas
    billing_month TEXT NOT NULL,        -- 'YYYY-MM'
    amount INTEGER NOT NULL DEFAULT 0,  -- 청구 금액
    usage_amount REAL,                  -- 사용량 (kWh 또는 m³)
    sms_text TEXT,                      -- 한전/가스사 문자 원본
    sms_date TEXT,                      -- 문자 수신일
    paid_amount INTEGER DEFAULT 0,      -- 수납액
    paid_date TEXT,                     -- 수납일
    status TEXT DEFAULT 'unpaid',       -- unpaid / paid
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 퇴거 정산
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    move_out_date TEXT NOT NULL,         -- 퇴거일
    last_paid_month TEXT,                -- 마지막 납부 월 (YYYY-MM)
    last_paid_date TEXT,                 -- 마지막 납부일
    days_used INTEGER,                  -- 사용 일수 (정산 대상)
    daily_rent INTEGER,                 -- 일일 월세
    daily_mgmt INTEGER,                 -- 일일 관리비
    prorated_rent INTEGER,              -- 일할 월세
    prorated_mgmt INTEGER,              -- 일할 관리비
    unpaid_rent INTEGER DEFAULT 0,      -- 미납 월세
    unpaid_mgmt INTEGER DEFAULT 0,      -- 미납 관리비
    electric_bill INTEGER DEFAULT 0,    -- 전기요금 정산
    gas_bill INTEGER DEFAULT 0,         -- 가스요금 정산
    cleaning_fee INTEGER DEFAULT 0,     -- 퇴실 청소비
    extra_deduction INTEGER DEFAULT 0,  -- 추가 공제 (수리비 등)
    total_owed INTEGER DEFAULT 0,       -- 총 공제액
    deposit_return INTEGER DEFAULT 0,  -- 보증금 반환액
    deposit_deduction INTEGER DEFAULT 0, -- 보증금 공제액
    deduction_reason TEXT,              -- 공제 사유
    final_settlement INTEGER DEFAULT 0, -- 최종 정산금 (양수=임대인 지급, 음수=임차인 지급)
    settlement_date TEXT,              -- 정산 완료일
    status TEXT DEFAULT 'pending',      -- pending / completed
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 월세 청구/수납 내역
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    billing_month TEXT NOT NULL,         -- 'YYYY-MM'
    due_date TEXT NOT NULL,              -- 납부기한
    amount INTEGER NOT NULL,             -- 청구액 (월세+관리비)
    paid_amount INTEGER DEFAULT 0,       -- 수납액
    paid_date TEXT,                      -- 수납일
    status TEXT DEFAULT 'unpaid',        -- unpaid / partial / paid / overdue
    payment_method TEXT,                 -- 계좌이체 / 카드 / 현금
    sender_name TEXT,                    -- 입금자명 (본인/부모님 등)
    deposit_memo TEXT,                   -- 입금 메모 (호수, 이름 등 계좌 메모)
    memo TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (contract_id) REFERENCES contracts(id),
    UNIQUE(contract_id, billing_month)
);

-- 호수 수리 내역
CREATE TABLE IF NOT EXISTS repair_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    repair_date TEXT NOT NULL,            -- 수리일
    title TEXT NOT NULL,                 -- 수리명 (예: 실내 전등 수리)
    category TEXT,                       -- 분류 (전등/에어컨/보일러/도어록/배관/도배/기타)
    cost INTEGER DEFAULT 0,              -- 수리 비용
    contractor TEXT,                     -- 수리업체/담당자
    status TEXT DEFAULT '완료',           -- 접수 / 진행중 / 완료
    photo_path TEXT,                     -- 수리 전후 사진 경로
    memo TEXT,                           -- 상세 내용
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (unit_id) REFERENCES units(id) ON DELETE CASCADE
);

-- 계약서 OCR 기록
CREATE TABLE IF NOT EXISTS contract_ocrs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER,
    photo_path TEXT NOT NULL,             -- 업로드된 계약서 사진
    raw_text TEXT,                       -- OCR 추출 전문
    parsed_data TEXT,                    -- 파싱 결과 (JSON)
    ocr_date TEXT DEFAULT (datetime('now', 'localtime')),
    memo TEXT,
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE
);

-- 건물 수리 내역 (공동구, 외벽, 옥상, 엘리베이터 등)
CREATE TABLE IF NOT EXISTS building_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL,
    repair_date TEXT NOT NULL,            -- 수리일
    title TEXT NOT NULL,                 -- 수리명 (예: 옥상 방수공사)
    category TEXT,                       -- 분류 (지붕/외벽/엘리베이터/공동구/보일러실/소방/기타)
    location TEXT,                       -- 위치 (옥상, 지하, 계단실 등)
    cost INTEGER DEFAULT 0,              -- 수리 비용
    contractor TEXT,                     -- 수리업체/담당자
    status TEXT DEFAULT '완료',           -- 접수 / 진행중 / 완료
    photo_path TEXT,                     -- 수리 전후 사진 경로
    memo TEXT,                           -- 상세 내용
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (building_id) REFERENCES buildings(id) ON DELETE CASCADE
);

-- 장부 (은행 입출금 내역)
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_date TEXT NOT NULL,              -- 거래일시
    tx_type TEXT NOT NULL,               -- deposit(입금) / withdraw(출금)
    amount INTEGER NOT NULL,             -- 금액
    sender_name TEXT,                   -- 입금자명 (또는 출금 대상)
    memo TEXT,                           -- 메모/적요
    bank_name TEXT,                      -- 은행명 (국민, 신한, 카카오 등)
    account_last4 TEXT,                  -- 계좌 끝 4자리
    raw_sms TEXT,                        -- 원본 SMS/알림톡 전문
    matched_payment_id INTEGER,         -- 매칭된 월세 수납 ID (NULL=미매칭)
    matched_contract_id INTEGER,        -- 매칭된 계약 ID
    matched_status TEXT DEFAULT 'unmatched', -- unmatched / matched / ignored
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 공지사항
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                       -- 제목
    body TEXT NOT NULL,                        -- 본문
    category TEXT DEFAULT 'general',           -- general(일반) / urgent(긴급) / maintenance(점검) / rent(수납)
    is_pinned INTEGER DEFAULT 0,               -- 고정 여부 (1=고정)
    target_building_id INTEGER,                -- 특정 건물 지정 (NULL=전체)
    created_by TEXT,                           -- 작성자
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (target_building_id) REFERENCES buildings(id) ON DELETE SET NULL
);

-- 공지 읽음 기록
CREATE TABLE IF NOT EXISTS notice_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    read_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (notice_id) REFERENCES notices(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(notice_id, user_id)
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_units_building ON units(building_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_month ON payments(billing_month);
CREATE INDEX IF NOT EXISTS idx_contracts_unit ON contracts(unit_id);
CREATE INDEX IF NOT EXISTS idx_contracts_tenant ON contracts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_repairs_unit ON repair_records(unit_id);
CREATE INDEX IF NOT EXISTS idx_building_repairs ON building_repairs(building_id);
CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(tx_date);
CREATE INDEX IF NOT EXISTS idx_ledger_matched ON ledger(matched_status);
CREATE INDEX IF NOT EXISTS idx_contract_ocrs ON contract_ocrs(contract_id);
CREATE INDEX IF NOT EXISTS idx_notices_created ON notices(created_at);
"""


def init_db():
    """DB 초기화 (테이블 생성 + 기존 DB 마이그레이션)"""
    with get_db_ctx() as conn:
        conn.executescript(SCHEMA)
        # 기존 contracts 테이블에 새 컬럼 추가 (이미 있으면 무시)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()]
        if "original_payment_day" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN original_payment_day INTEGER")
        if "payment_day_changed_date" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN payment_day_changed_date TEXT")
        if "terminated_date" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN terminated_date TEXT")
        if "cleaning_fee" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN cleaning_fee INTEGER DEFAULT 0")
        if "extra_person_fee" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN extra_person_fee INTEGER DEFAULT 0")
        if "renewal_alert_sent" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN renewal_alert_sent TEXT")
        if "deposit_return_alert_sent" not in cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN deposit_return_alert_sent TEXT")
        # 기존 계약의 original_payment_day 채우기
        conn.execute(
            """UPDATE contracts SET original_payment_day = payment_day
               WHERE original_payment_day IS NULL"""
        )
        # settlements 테이블에 새 컬럼 추가
        s_cols = [r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()]
        for col, coltype in [
            ("electric_bill", "INTEGER DEFAULT 0"),
            ("gas_bill", "INTEGER DEFAULT 0"),
            ("cleaning_fee", "INTEGER DEFAULT 0"),
            ("extra_deduction", "INTEGER DEFAULT 0"),
            ("total_owed", "INTEGER DEFAULT 0"),
        ]:
            if col not in s_cols:
                conn.execute(f"ALTER TABLE settlements ADD COLUMN {col} {coltype}")
        # payments 테이블에 새 컬럼 추가
        p_cols = [r[1] for r in conn.execute("PRAGMA table_info(payments)").fetchall()]
        for col, coltype in [
            ("sender_name", "TEXT"),
            ("deposit_memo", "TEXT"),
        ]:
            if col not in p_cols:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {coltype}")
    # 기본 관리자 생성 (admin / admin123)
    from werkzeug.security import generate_password_hash
    with get_db_ctx() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (username, password_hash, name, role) VALUES (?, ?, ?, ?)",
                ("admin", generate_password_hash("admin123"), "관리자", "admin"),
            )
    print(f"DB 초기화 완료: {DB_PATH}")


if __name__ == "__main__":
    init_db()