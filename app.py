"""
임대관리 앱 - Flask 메인 애플리케이션
건물/호수 관리, 임대차 계약, 월세 수납, 연체 알림
"""
import os
import sqlite3
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from db import get_db, init_db

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "rental-manager-dev-key-2026")
app.config["UPLOAD_DIR"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB


# get_db를 g 기반으로 오버라이드: 같은 요청 내에서 커넥션 재사용
_orig_get_db = get_db


def get_db():
    """같은 요청 컨텍스트 내에서는 동일 커넥션 재사용, 아니면 새로 생성"""
    if g.get("db") is not None:
        return g.db
    db = _orig_get_db()
    g.db = db
    return db


# ============================================================
# 인증 데코레이터
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.before_request
def load_user():
    g.user = None
    g.db = None
    if "user_id" in session:
        db = get_db()  # g.db에 자동 저장됨
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


@app.teardown_appcontext
def close_db(exception=None):
    """요청 종료 시 DB 커넥션 닫기 (database is locked 방지)"""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ============================================================
# 컨텍스트: 연체 카운트 (네비게이션 배지용)
# ============================================================
@app.context_processor
def inject_overdue_count():
    try:
        db = g.get("db") if g else None
        if db is None:
            db = get_db()
        count = db.execute(
            "SELECT COUNT(*) FROM payments WHERE status = 'overdue'"
        ).fetchone()[0]
        # 계약 알림 카운트 (만료 2개월전 + 보증금반환 6개월전)
        today = today_str()
        alert_count = db.execute(
            """SELECT COUNT(*) FROM contracts WHERE status = 'active'
               AND end_date IS NOT NULL
               AND date(end_date, '-2 months') <= date(?)""",
            (today,)
        ).fetchone()[0]
        return {"overdue_count": count, "alert_count": alert_count}
    except Exception:
        return {"overdue_count": 0, "alert_count": 0}


# ============================================================
# 유틸: 오늘 날짜, 연체 상태 업데이트
# ============================================================
def today_str():
    return date.today().isoformat()


def update_overdue_status():
    """납부기한이 지났지만 미납인 청구를 연체로 변경"""
    db = get_db()
    today = today_str()
    db.execute(
        """UPDATE payments 
           SET status = 'overdue' 
           WHERE status = 'unpaid' AND due_date < ?""",
        (today,)
    )
    db.commit()


def check_contract_alerts():
    """계약 만료 2개월전 알림 + 보증금 반환 6개월전 알림 체크"""
    db = get_db()
    today = date.today()
    active_contracts = db.execute(
        "SELECT * FROM contracts WHERE status = 'active'"
    ).fetchall()

    renewal_alerts = []      # 만료 2개월전
    deposit_alerts = []      # 보증금 반환 6개월전
    renewal_urgent = []      # 만료 1개월 이내

    for c in active_contracts:
        if not c["end_date"]:
            continue
        end_date = date.fromisoformat(c["end_date"])
        days_until_end = (end_date - today).days

        # 계약 만료 2개월(60일) 전 알림
        if days_until_end <= 60 and days_until_end > 0:
            if not c["renewal_alert_sent"]:
                db.execute(
                    "UPDATE contracts SET renewal_alert_sent = ? WHERE id = ?",
                    (today_str(), c["id"])
                )
            renewal_alerts.append(c)
            if days_until_end <= 30:
                renewal_urgent.append(c)

        # 보증금 반환 6개월(180일) 전 알림
        if days_until_end <= 180 and days_until_end > 0:
            if not c["deposit_return_alert_sent"]:
                db.execute(
                    "UPDATE contracts SET deposit_return_alert_sent = ? WHERE id = ?",
                    (today_str(), c["id"])
                )
            # 보증금이 있는 경우만
            if c["deposit"] > 0:
                deposit_alerts.append(c)

    db.commit()
    return {
        "renewal_alerts": renewal_alerts,
        "renewal_urgent": renewal_urgent,
        "deposit_alerts": deposit_alerts,
        "total_alerts": len(renewal_alerts) + len(deposit_alerts),
    }


def fmt_money(n):
    """천 단위 콤마"""
    if n is None:
        return "0"
    return f"{n:,}"


app.jinja_env.filters["money"] = fmt_money


# ============================================================
# 유틸: 일할 계산 (월세/30일 기준)
# ============================================================
from calendar import monthrange


def calc_daily_amount(monthly_amount):
    """월액을 일할로 변환 (30일 기준, 월세 계약의 표준 관행)"""
    if not monthly_amount:
        return 0
    return monthly_amount // 30


def calc_prorated(monthly_rent, monthly_mgmt, start_date_str, end_date_str):
    """
    지정된 기간에 대한 일할 월세/관리비 계산.
    start_date_str ~ end_date_str (양 끝 포함) 사용 일수.
    월세/30일 기준.
    """
    if not monthly_rent and not monthly_mgmt:
        return 0, 0, 0

    start = date.fromisoformat(start_date_str)
    end = date.fromisoformat(end_date_str)
    if end < start:
        return 0, 0, 0

    days_used = (end - start).days + 1  # 양 끝 포함
    daily_rent = calc_daily_amount(monthly_rent)
    daily_mgmt = calc_daily_amount(monthly_mgmt)
    prorated_rent = daily_rent * days_used
    prorated_mgmt = daily_mgmt * days_used
    return prorated_rent, prorated_mgmt, days_used


def calc_moveout_settlement(contract, move_out_date_str, db):
    """
    퇴거 정산 계산.
    - 마지막 완납 월의 다음 날부터 퇴거일까지 일할 계산
    - 미납 잔액 확인
    - 보증금 공제/반환 계산
    """
    rent = contract["monthly_rent"]
    mgmt = contract["management_fee"]
    deposit = contract["deposit"]

    # 해당 계약의 모든 수납 내역 (완납 + 부분납)
    payments = db.execute(
        """SELECT * FROM payments WHERE contract_id = ? ORDER BY billing_month""",
        (contract["id"],)
    ).fetchall()

    # 마지막 납부 월 찾기
    last_paid_month = None
    last_paid_date = None
    unpaid_amount = 0  # 미납액

    for p in payments:
        if p["status"] == "paid":
            last_paid_month = p["billing_month"]
            last_paid_date = p["paid_date"]
        elif p["status"] in ("partial", "unpaid", "overdue"):
            unpaid_amount += (p["amount"] - p["paid_amount"])

    move_out = date.fromisoformat(move_out_date_str)

    # 일할 계산: 마지막 완납월의 다음월 1일부터 퇴거일까지
    prorated_rent = 0
    prorated_mgmt = 0
    days_used = 0

    if last_paid_month:
        # 마지막 완납월의 다음월 1일
        last_year, last_month = map(int, last_paid_month.split("-"))
        next_month = last_month + 1 if last_month < 12 else 1
        next_year = last_year if last_month < 12 else last_year + 1
        prorate_start = date(next_year, next_month, 1)
    else:
        # 완납된 월이 없으면 계약 시작일부터
        prorate_start = date.fromisoformat(contract["start_date"])

    if move_out >= prorate_start:
        prorated_rent, prorated_mgmt, days_used = calc_prorated(
            rent, mgmt, prorate_start.isoformat(), move_out_date_str
        )

    # 보증금 정산
    total_owed = prorated_rent + prorated_mgmt + unpaid_amount
    deposit_deduction = total_owed
    deposit_return = deposit - total_owed
    if deposit_return < 0:
        deposit_return = 0
        # 보증금으로도 부족한 경우
        final_settlement = -(total_owed - deposit)  # 임차인이 추가 지급
    else:
        final_settlement = deposit_return  # 임대인이 임차인에게 반환

    return {
        "move_out_date": move_out_date_str,
        "last_paid_month": last_paid_month,
        "last_paid_date": last_paid_date,
        "days_used": days_used,
        "daily_rent": calc_daily_amount(rent),
        "daily_mgmt": calc_daily_amount(mgmt),
        "prorated_rent": prorated_rent,
        "prorated_mgmt": prorated_mgmt,
        "unpaid_amount": unpaid_amount,
        "total_owed": total_owed,
        "deposit": deposit,
        "deposit_deduction": deposit_deduction if total_owed <= deposit else deposit,
        "deposit_return": deposit_return,
        "final_settlement": final_settlement,
    }

# 템플릿에서 today_str() 사용 가능
@app.context_processor
def inject_today():
    return {"today_str": today_str()}


# ============================================================
# 라우트: 인증
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash(f"{user['name']}님 환영합니다.", "success")
            return redirect(url_for("dashboard"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "info")
    return redirect(url_for("login"))


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old_pw = request.form.get("old_password", "")
        new_pw = request.form.get("new_password", "")
        if not check_password_hash(g.user["password_hash"], old_pw):
            flash("현재 비밀번호가 올바르지 않습니다.", "danger")
            return redirect(url_for("change_password"))
        if len(new_pw) < 4:
            flash("새 비밀번호는 4자 이상이어야 합니다.", "danger")
            return redirect(url_for("change_password"))
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_pw), g.user["id"])
        )
        db.commit()
        flash("비밀번호가 변경되었습니다.", "success")
        return redirect(url_for("dashboard"))
    return render_template("change_password.html")


# ============================================================
# 라우트: 대시보드
# ============================================================
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    update_overdue_status()

    # 건물 요약
    buildings = db.execute("SELECT * FROM buildings ORDER BY name").fetchall()

    stats = {
        "total_buildings": 0,
        "total_units": 0,
        "occupied": 0,
        "vacant": 0,
        "repair": 0,
        "total_rent_expected": 0,
        "total_collected_this_month": 0,
        "total_overdue_amount": 0,
        "overdue_count": 0,
    }
    stats["total_buildings"] = len(buildings)

    current_month = datetime.now().strftime("%Y-%m")

    for b in buildings:
        units = db.execute("SELECT * FROM units WHERE building_id = ?", (b["id"],)).fetchall()
        stats["total_units"] += len(units)
        for u in units:
            if u["status"] == "임대중":
                stats["occupied"] += 1
            elif u["status"] == "공실":
                stats["vacant"] += 1
            elif u["status"] == "수리중":
                stats["repair"] += 1

    # 이번달 수납 현황
    month_payments = db.execute(
        """SELECT * FROM payments WHERE billing_month = ?""", (current_month,)
    ).fetchall()
    for p in month_payments:
        stats["total_collected_this_month"] += p["paid_amount"]

    # 연체
    overdue = db.execute(
        """SELECT * FROM payments WHERE status = 'overdue' ORDER BY due_date"""
    ).fetchall()
    stats["overdue_count"] = len(overdue)
    for p in overdue:
        stats["total_overdue_amount"] += (p["amount"] - p["paid_amount"])

    # 활성 계약 수
    active_contracts = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE status = 'active'"
    ).fetchone()[0]

    # 계약 알림 체크
    alerts = check_contract_alerts()

    # 알림 상세 데이터 (건물/호수/임차인 정보 포함)
    renewal_alerts_detail = db.execute(
        """SELECT c.id, c.end_date, c.deposit, c.renewal_alert_sent,
                  u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.status = 'active' AND c.end_date IS NOT NULL
             AND date(c.end_date, '-2 months') <= date(?)
             AND date(c.end_date) > date(?)
           ORDER BY c.end_date""",
        (today_str(), today_str())
    ).fetchall()

    deposit_alerts_detail = db.execute(
        """SELECT c.id, c.end_date, c.deposit, c.deposit_return_alert_sent,
                  u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.status = 'active' AND c.end_date IS NOT NULL
             AND date(c.end_date, '-6 months') <= date(?)
             AND date(c.end_date) > date(?)
             AND c.deposit > 0
           ORDER BY c.end_date""",
        (today_str(), today_str())
    ).fetchall()

    occupancy_rate = 0
    if stats["total_units"] > 0:
        occupancy_rate = round(stats["occupied"] / stats["total_units"] * 100, 1)

    # 최신 공지 (고정공지 + 최근 5건)
    recent_notices = db.execute(
        """SELECT n.*, b.name as building_name
           FROM notices n
           LEFT JOIN buildings b ON b.id = n.target_building_id
           ORDER BY n.is_pinned DESC, n.created_at DESC
           LIMIT 5"""
    ).fetchall()

    # 안 읽은 공지 수
    user_id = session.get("user_id")
    unread_notices = db.execute(
        """SELECT COUNT(*) FROM notices n
           WHERE NOT EXISTS (
               SELECT 1 FROM notice_reads nr WHERE nr.notice_id = n.id AND nr.user_id = ?
           )""",
        (user_id,),
    ).fetchone()[0]

    return render_template(
        "dashboard.html",
        stats=stats, buildings=buildings, overdue=overdue,
        active_contracts=active_contracts,
        occupancy_rate=occupancy_rate,
        current_month=current_month,
        alerts=alerts,
        renewal_alerts=renewal_alerts_detail,
        deposit_alerts=deposit_alerts_detail,
        recent_notices=recent_notices,
        unread_notices=unread_notices,
    )


# ============================================================
# 라우트: 건물 관리
# ============================================================
@app.route("/buildings")
@login_required
def building_list():
    db = get_db()
    buildings = db.execute(
        """SELECT b.*, 
                  COUNT(u.id) as unit_count,
                  SUM(CASE WHEN u.status='임대중' THEN 1 ELSE 0 END) as occupied,
                  SUM(CASE WHEN u.status='공실' THEN 1 ELSE 0 END) as vacant
           FROM buildings b
           LEFT JOIN units u ON u.building_id = b.id
           GROUP BY b.id
           ORDER BY b.name"""
    ).fetchall()
    return render_template("building_list.html", buildings=buildings)


@app.route("/buildings/add", methods=["GET", "POST"])
@login_required
def building_add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        btype = request.form.get("type", "").strip()
        address = request.form.get("address", "").strip()
        floors = request.form.get("floors", type=int) or 0
        total_units = request.form.get("total_units", type=int) or 0
        memo = request.form.get("memo", "").strip()
        if not name:
            flash("건물명은 필수입니다.", "danger")
            return redirect(url_for("building_add"))
        db = get_db()
        db.execute(
            """INSERT INTO buildings (name, type, address, floors, total_units, memo)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, btype, address, floors, total_units, memo)
        )
        db.commit()
        flash("건물이 등록되었습니다.", "success")
        return redirect(url_for("building_list"))
    return render_template("building_form.html", building=None)


@app.route("/buildings/<int:bid>/edit", methods=["GET", "POST"])
@login_required
def building_edit(bid):
    db = get_db()
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if not building:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        btype = request.form.get("type", "").strip()
        address = request.form.get("address", "").strip()
        floors = request.form.get("floors", type=int) or 0
        total_units = request.form.get("total_units", type=int) or 0
        memo = request.form.get("memo", "").strip()
        db.execute(
            """UPDATE buildings SET name=?, type=?, address=?, floors=?, total_units=?, memo=?
               WHERE id=?""",
            (name, btype, address, floors, total_units, memo, bid)
        )
        db.commit()
        flash("건물 정보가 수정되었습니다.", "success")
        return redirect(url_for("building_list"))
    return render_template("building_form.html", building=building)


@app.route("/buildings/<int:bid>/delete", methods=["POST"])
@login_required
def building_delete(bid):
    db = get_db()
    # 하위 데이터를 역순으로 삭제: payments -> contracts -> units -> building
    # (contracts/payments의 외래키가 NO ACTION이므로 수동 삭제 필요)
    unit_ids = [r["id"] for r in db.execute(
        "SELECT id FROM units WHERE building_id = ?", (bid,)
    ).fetchall()]
    if unit_ids:
        placeholders = ",".join("?" * len(unit_ids))
        # 해당 호수들의 계약에 연결된 수납 내역 삭제
        contract_ids = [r["id"] for r in db.execute(
            f"SELECT id FROM contracts WHERE unit_id IN ({placeholders})", unit_ids
        ).fetchall()]
        if contract_ids:
            c_ph = ",".join("?" * len(contract_ids))
            db.execute(f"DELETE FROM payments WHERE contract_id IN ({c_ph})", contract_ids)
            db.execute(f"DELETE FROM contracts WHERE unit_id IN ({placeholders})", unit_ids)
        else:
            db.execute(f"DELETE FROM contracts WHERE unit_id IN ({placeholders})", unit_ids)
        db.execute("DELETE FROM units WHERE building_id = ?", (bid,))
    db.execute("DELETE FROM buildings WHERE id = ?", (bid,))
    db.commit()
    flash("건물이 삭제되었습니다. (관련 호수/계약/수납 내역도 함께 삭제됨)", "info")
    return redirect(url_for("building_list"))


# ============================================================
# 라우트: 호수 관리
# ============================================================
@app.route("/buildings/<int:bid>/units")
@login_required
def unit_list(bid):
    db = get_db()
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if not building:
        abort(404)
    units = db.execute(
        """SELECT u.*, c.tenant_id, t.name as tenant_name
           FROM units u
           LEFT JOIN contracts c ON c.unit_id = u.id AND c.status = 'active'
           LEFT JOIN tenants t ON t.id = c.tenant_id
           WHERE u.building_id = ?
           ORDER BY u.unit_number""",
        (bid,)
    ).fetchall()
    return render_template("unit_list.html", building=building, units=units)


@app.route("/buildings/<int:bid>/units/add", methods=["GET", "POST"])
@login_required
def unit_add(bid):
    db = get_db()
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if not building:
        abort(404)
    if request.method == "POST":
        unit_number = request.form.get("unit_number", "").strip()
        floor = request.form.get("floor", "").strip()
        room_type = request.form.get("room_type", "").strip()
        area_pyeong = request.form.get("area_pyeong", type=float)
        area_sqm = request.form.get("area_sqm", type=float)
        deposit = request.form.get("deposit", type=int) or 0
        monthly_rent = request.form.get("monthly_rent", type=int) or 0
        management_fee = request.form.get("management_fee", type=int) or 0
        status = request.form.get("status", "공실")
        memo = request.form.get("memo", "").strip()
        if not unit_number:
            flash("호수는 필수입니다.", "danger")
            return redirect(url_for("unit_add", bid=bid))
        try:
            db.execute(
                """INSERT INTO units 
                   (building_id, unit_number, floor, room_type, area_pyeong, area_sqm,
                    deposit, monthly_rent, management_fee, status, memo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bid, unit_number, floor, room_type, area_pyeong, area_sqm,
                 deposit, monthly_rent, management_fee, status, memo)
            )
            db.commit()
            flash("호수가 등록되었습니다.", "success")
        except sqlite3.IntegrityError:
            flash("이미 존재하는 호수입니다.", "danger")
        return redirect(url_for("unit_list", bid=bid))
    return render_template("unit_form.html", building=building, unit=None)


@app.route("/units/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def unit_edit(uid):
    db = get_db()
    unit = db.execute("SELECT * FROM units WHERE id = ?", (uid,)).fetchone()
    if not unit:
        abort(404)
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (unit["building_id"],)).fetchone()
    if request.method == "POST":
        unit_number = request.form.get("unit_number", "").strip()
        floor = request.form.get("floor", "").strip()
        room_type = request.form.get("room_type", "").strip()
        area_pyeong = request.form.get("area_pyeong", type=float)
        area_sqm = request.form.get("area_sqm", type=float)
        deposit = request.form.get("deposit", type=int) or 0
        monthly_rent = request.form.get("monthly_rent", type=int) or 0
        management_fee = request.form.get("management_fee", type=int) or 0
        status = request.form.get("status", "공실")
        memo = request.form.get("memo", "").strip()
        try:
            db.execute(
                """UPDATE units SET unit_number=?, floor=?, room_type=?, area_pyeong=?, area_sqm=?,
                   deposit=?, monthly_rent=?, management_fee=?, status=?, memo=? WHERE id=?""",
                (unit_number, floor, room_type, area_pyeong, area_sqm,
                 deposit, monthly_rent, management_fee, status, memo, uid)
            )
            db.commit()
            flash("호수 정보가 수정되었습니다.", "success")
        except sqlite3.IntegrityError:
            flash("이미 존재하는 호수입니다. 다른 호수 번호를 사용하세요.", "danger")
        return redirect(url_for("unit_list", bid=unit["building_id"]))
    return render_template("unit_form.html", building=building, unit=unit)


@app.route("/units/<int:uid>/delete", methods=["POST"])
@login_required
def unit_delete(uid):
    db = get_db()
    unit = db.execute("SELECT building_id FROM units WHERE id = ?", (uid,)).fetchone()
    if not unit:
        abort(404)
    # 하위 데이터 역순 삭제: payments -> contracts -> units
    contract_ids = [r["id"] for r in db.execute(
        "SELECT id FROM contracts WHERE unit_id = ?", (uid,)
    ).fetchall()]
    if contract_ids:
        c_ph = ",".join("?" * len(contract_ids))
        db.execute(f"DELETE FROM payments WHERE contract_id IN ({c_ph})", contract_ids)
        db.execute("DELETE FROM contracts WHERE unit_id = ?", (uid,))
    db.execute("DELETE FROM units WHERE id = ?", (uid,))
    db.commit()
    flash("호수가 삭제되었습니다. (관련 계약/수납 내역도 함께 삭제됨)", "info")
    return redirect(url_for("unit_list", bid=unit["building_id"]))


# ============================================================
# 라우트: 호수 수리 내역
# ============================================================
@app.route("/units/<int:uid>/repairs")
@login_required
def repair_list(uid):
    db = get_db()
    unit = db.execute(
        """SELECT u.*, b.name as building_name
           FROM units u JOIN buildings b ON b.id = u.building_id
           WHERE u.id = ?""",
        (uid,)
    ).fetchone()
    if not unit:
        abort(404)
    repairs = db.execute(
        "SELECT * FROM repair_records WHERE unit_id = ? ORDER BY repair_date DESC",
        (uid,)
    ).fetchall()
    total_cost = sum(r["cost"] or 0 for r in repairs)
    return render_template("repair_list.html", unit=unit, repairs=repairs, total_cost=total_cost)


@app.route("/units/<int:uid>/repairs/add", methods=["GET", "POST"])
@login_required
def repair_add(uid):
    db = get_db()
    unit = db.execute(
        """SELECT u.*, b.name as building_name
           FROM units u JOIN buildings b ON b.id = u.building_id
           WHERE u.id = ?""",
        (uid,)
    ).fetchone()
    if not unit:
        abort(404)

    if request.method == "POST":
        repair_date = request.form.get("repair_date", "").strip()
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        cost = request.form.get("cost", type=int) or 0
        contractor = request.form.get("contractor", "").strip()
        status = request.form.get("status", "완료")
        memo = request.form.get("memo", "").strip()

        if not repair_date or not title:
            flash("수리일과 수리명은 필수입니다.", "danger")
            return redirect(url_for("repair_add", uid=uid))

        # 사진 업로드
        photo_path = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            if allowed_file(photo.filename):
                ext = os.path.splitext(photo.filename)[1].lower()
                safe_name = f"repair_{uid}_{repair_date}_{title}{ext}"
                safe_name = secure_filename(safe_name)
                save_path = os.path.join(UPLOAD_DIR, safe_name)
                photo.save(save_path)
                photo_path = f"uploads/{safe_name}"
            else:
                flash("지원하지 않는 파일 형식입니다. (jpg, png, gif, webp)", "danger")
                return redirect(url_for("repair_add", uid=uid))

        db.execute(
            """INSERT INTO repair_records
               (unit_id, repair_date, title, category, cost, contractor, status, photo_path, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, repair_date, title, category, cost, contractor, status, photo_path, memo)
        )
        db.commit()
        flash("수리 내역이 등록되었습니다.", "success")
        return redirect(url_for("repair_list", uid=uid))

    return render_template("repair_form.html", unit=unit, repair=None)


@app.route("/units/<int:uid>/repairs/<int:rid>/delete", methods=["POST"])
@login_required
def repair_delete(uid, rid):
    db = get_db()
    repair = db.execute("SELECT * FROM repair_records WHERE id = ?", (rid,)).fetchone()
    if not repair:
        abort(404)
    if repair["photo_path"]:
        full_path = os.path.join(app.config["UPLOAD_DIR"],
                                 os.path.basename(repair["photo_path"]))
        if os.path.exists(full_path):
            os.remove(full_path)
    db.execute("DELETE FROM repair_records WHERE id = ?", (rid,))
    db.commit()
    flash("수리 내역이 삭제되었습니다.", "info")
    return redirect(url_for("repair_list", uid=uid))


# ============================================================
# 라우트: 건물 수리 내역
# ============================================================
@app.route("/buildings/<int:bid>/repairs")
@login_required
def building_repair_list(bid):
    db = get_db()
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if not building:
        abort(404)
    repairs = db.execute(
        "SELECT * FROM building_repairs WHERE building_id = ? ORDER BY repair_date DESC",
        (bid,)
    ).fetchall()
    total_cost = sum(r["cost"] or 0 for r in repairs)
    return render_template("building_repair_list.html", building=building,
                           repairs=repairs, total_cost=total_cost)


@app.route("/buildings/<int:bid>/repairs/add", methods=["GET", "POST"])
@login_required
def building_repair_add(bid):
    db = get_db()
    building = db.execute("SELECT * FROM buildings WHERE id = ?", (bid,)).fetchone()
    if not building:
        abort(404)

    if request.method == "POST":
        repair_date = request.form.get("repair_date", "").strip()
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        location = request.form.get("location", "").strip()
        cost = request.form.get("cost", type=int) or 0
        contractor = request.form.get("contractor", "").strip()
        status = request.form.get("status", "완료")
        memo = request.form.get("memo", "").strip()

        if not repair_date or not title:
            flash("수리일과 수리명은 필수입니다.", "danger")
            return redirect(url_for("building_repair_add", bid=bid))

        photo_path = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            if allowed_file(photo.filename):
                ext = os.path.splitext(photo.filename)[1].lower()
                safe_name = f"brepair_{bid}_{repair_date}_{title}{ext}"
                safe_name = secure_filename(safe_name)
                save_path = os.path.join(UPLOAD_DIR, safe_name)
                photo.save(save_path)
                photo_path = f"uploads/{safe_name}"
            else:
                flash("지원하지 않는 파일 형식입니다.", "danger")
                return redirect(url_for("building_repair_add", bid=bid))

        db.execute(
            """INSERT INTO building_repairs
               (building_id, repair_date, title, category, location, cost, contractor, status, photo_path, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bid, repair_date, title, category, location, cost, contractor, status, photo_path, memo)
        )
        db.commit()
        flash("건물 수리 내역이 등록되었습니다.", "success")
        return redirect(url_for("building_repair_list", bid=bid))

    return render_template("building_repair_form.html", building=building, repair=None)


@app.route("/buildings/<int:bid>/repairs/<int:rid>/delete", methods=["POST"])
@login_required
def building_repair_delete(bid, rid):
    db = get_db()
    repair = db.execute("SELECT * FROM building_repairs WHERE id = ?", (rid,)).fetchone()
    if not repair:
        abort(404)
    if repair["photo_path"]:
        full_path = os.path.join(app.config["UPLOAD_DIR"],
                                 os.path.basename(repair["photo_path"]))
        if os.path.exists(full_path):
            os.remove(full_path)
    db.execute("DELETE FROM building_repairs WHERE id = ?", (rid,))
    db.commit()
    flash("건물 수리 내역이 삭제되었습니다.", "info")
    return redirect(url_for("building_repair_list", bid=bid))


# ============================================================
# 라우트: 임차인 관리
# ============================================================
@app.route("/tenants")
@login_required
def tenant_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        tenants = db.execute(
            """SELECT t.*, COUNT(c.id) as active_contracts
               FROM tenants t
               LEFT JOIN contracts c ON c.tenant_id = t.id AND c.status = 'active'
               WHERE t.name LIKE ? OR t.phone LIKE ?
               GROUP BY t.id
               ORDER BY t.name""",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
    else:
        tenants = db.execute(
            """SELECT t.*, COUNT(c.id) as active_contracts
               FROM tenants t
               LEFT JOIN contracts c ON c.tenant_id = t.id AND c.status = 'active'
               GROUP BY t.id
               ORDER BY t.name"""
        ).fetchall()
    return render_template("tenant_list.html", tenants=tenants, q=q)


@app.route("/tenants/add", methods=["GET", "POST"])
@login_required
def tenant_add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        id_number = request.form.get("id_number", "").strip()
        emergency_contact = request.form.get("emergency_contact", "").strip()
        address = request.form.get("address", "").strip()
        memo = request.form.get("memo", "").strip()
        if not name:
            flash("임차인 이름은 필수입니다.", "danger")
            return redirect(url_for("tenant_add"))
        db = get_db()
        db.execute(
            """INSERT INTO tenants (name, phone, email, id_number, emergency_contact, address, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, phone, email, id_number, emergency_contact, address, memo)
        )
        db.commit()
        flash("임차인이 등록되었습니다.", "success")
        return redirect(url_for("tenant_list"))
    return render_template("tenant_form.html", tenant=None)


@app.route("/tenants/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def tenant_edit(tid):
    db = get_db()
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()
    if not tenant:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        id_number = request.form.get("id_number", "").strip()
        emergency_contact = request.form.get("emergency_contact", "").strip()
        address = request.form.get("address", "").strip()
        memo = request.form.get("memo", "").strip()
        db.execute(
            """UPDATE tenants SET name=?, phone=?, email=?, id_number=?, 
               emergency_contact=?, address=?, memo=? WHERE id=?""",
            (name, phone, email, id_number, emergency_contact, address, memo, tid)
        )
        db.commit()
        flash("임차인 정보가 수정되었습니다.", "success")
        return redirect(url_for("tenant_list"))
    return render_template("tenant_form.html", tenant=tenant)


@app.route("/tenants/<int:tid>/delete", methods=["POST"])
@login_required
def tenant_delete(tid):
    db = get_db()
    # 활성 계약이 있으면 삭제 불가
    active = db.execute(
        "SELECT COUNT(*) FROM contracts WHERE tenant_id = ? AND status = 'active'", (tid,)
    ).fetchone()[0]
    if active > 0:
        flash("활성 계약이 있어 삭제할 수 없습니다.", "danger")
        return redirect(url_for("tenant_list"))
    # 비활성 계약(terminated/expired)과 관련 수납 내역도 함께 삭제
    contract_ids = [r["id"] for r in db.execute(
        "SELECT id FROM contracts WHERE tenant_id = ?", (tid,)
    ).fetchall()]
    if contract_ids:
        c_ph = ",".join("?" * len(contract_ids))
        db.execute(f"DELETE FROM payments WHERE contract_id IN ({c_ph})", contract_ids)
        db.execute("DELETE FROM contracts WHERE tenant_id = ?", (tid,))
    db.execute("DELETE FROM tenants WHERE id = ?", (tid,))
    db.commit()
    flash("임차인이 삭제되었습니다. (관련 계약/수납 내역도 함께 삭제됨)", "info")
    return redirect(url_for("tenant_list"))


# ============================================================
# 라우트: 계약 관리
# ============================================================
@app.route("/contracts")
@login_required
def contract_list():
    db = get_db()
    status_filter = request.args.get("status", "")
    if status_filter:
        contracts = db.execute(
            """SELECT c.*, u.unit_number, u.building_id, b.name as building_name,
                      t.name as tenant_name, t.phone as tenant_phone
               FROM contracts c
               JOIN units u ON u.id = c.unit_id
               JOIN buildings b ON b.id = u.building_id
               JOIN tenants t ON t.id = c.tenant_id
               WHERE c.status = ?
               ORDER BY c.start_date DESC""",
            (status_filter,)
        ).fetchall()
    else:
        contracts = db.execute(
            """SELECT c.*, u.unit_number, u.building_id, b.name as building_name,
                      t.name as tenant_name, t.phone as tenant_phone
               FROM contracts c
               JOIN units u ON u.id = c.unit_id
               JOIN buildings b ON b.id = u.building_id
               JOIN tenants t ON t.id = c.tenant_id
               ORDER BY c.start_date DESC"""
        ).fetchall()
    return render_template("contract_list.html", contracts=contracts, status_filter=status_filter)


@app.route("/contracts/add", methods=["GET", "POST"])
@login_required
def contract_add():
    db = get_db()
    if request.method == "POST":
        unit_id = request.form.get("unit_id", type=int)
        tenant_id = request.form.get("tenant_id", type=int)
        contract_type = request.form.get("contract_type", "월세")
        deposit = request.form.get("deposit", type=int) or 0
        monthly_rent = request.form.get("monthly_rent", type=int) or 0
        management_fee = request.form.get("management_fee", type=int) or 0
        cleaning_fee = request.form.get("cleaning_fee", type=int) or 0
        extra_person_fee = request.form.get("extra_person_fee", type=int) or 0
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        payment_day = request.form.get("payment_day", type=int) or 25
        memo = request.form.get("memo", "").strip()

        if not unit_id or not tenant_id or not start_date or not end_date:
            flash("호수, 임차인, 계약기간은 필수입니다.", "danger")
            return redirect(url_for("contract_add"))

        # 호수 상태 확인
        unit = db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        if unit and unit["status"] == "임대중":
            flash("이미 임대 중인 호수입니다.", "danger")
            return redirect(url_for("contract_add"))

        db.execute(
            """INSERT INTO contracts
               (unit_id, tenant_id, contract_type, deposit, monthly_rent, management_fee,
                cleaning_fee, extra_person_fee, start_date, end_date, payment_day, status, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (unit_id, tenant_id, contract_type, deposit, monthly_rent, management_fee,
             cleaning_fee, extra_person_fee, start_date, end_date, payment_day, memo)
        )
        # 호수 상태를 임대중으로
        db.execute("UPDATE units SET status = '임대중' WHERE id = ?", (unit_id,))
        db.commit()
        flash("계약이 등록되었습니다.", "success")
        return redirect(url_for("contract_list"))

    # GET: 폼 표시
    units = db.execute(
        """SELECT u.*, b.name as building_name
           FROM units u JOIN buildings b ON b.id = u.building_id
           WHERE u.status IN ('공실', '수리중')
           ORDER BY b.name, u.unit_number"""
    ).fetchall()
    tenants = db.execute("SELECT * FROM tenants ORDER BY name").fetchall()
    return render_template("contract_form.html", contract=None, units=units, tenants=tenants)


@app.route("/contracts/<int:cid>/view")
@login_required
def contract_view(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, u.building_id, b.name as building_name,
                  t.name as tenant_name, t.phone, t.email, t.address, t.id_number
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)
    payments = db.execute(
        "SELECT * FROM payments WHERE contract_id = ? ORDER BY billing_month DESC",
        (cid,)
    ).fetchall()
    # 납부일 변경 이력
    day_changes = db.execute(
        "SELECT * FROM payment_day_changes WHERE contract_id = ? ORDER BY changed_date DESC",
        (cid,)
    ).fetchall()
    # 정산 내역
    settlement = db.execute(
        "SELECT * FROM settlements WHERE contract_id = ? ORDER BY id DESC LIMIT 1",
        (cid,)
    ).fetchone()
    # 계량기 기록
    meters = db.execute(
        "SELECT * FROM meter_readings WHERE contract_id = ? ORDER BY reading_type, type, reading_date",
        (cid,)
    ).fetchall()
    # 공과금
    utilities = db.execute(
        "SELECT * FROM utility_bills WHERE contract_id = ? ORDER BY billing_month DESC, type",
        (cid,)
    ).fetchall()
    # 보증금 납부 이력
    deposit_payments = db.execute(
        "SELECT * FROM deposit_payments WHERE contract_id = ? ORDER BY paid_date DESC",
        (cid,)
    ).fetchall()
    deposit_paid = sum(d["amount"] for d in deposit_payments)
    deposit_balance = contract["deposit"] - deposit_paid
    # OCR 기록
    ocr_records = db.execute(
        "SELECT * FROM contract_ocrs WHERE contract_id = ? ORDER BY ocr_date DESC LIMIT 5",
        (cid,)
    ).fetchall()
    return render_template("contract_view.html", contract=contract, payments=payments,
                           day_changes=day_changes, settlement=settlement,
                           meters=meters, utilities=utilities,
                           deposit_payments=deposit_payments,
                           deposit_paid=deposit_paid, deposit_balance=deposit_balance,
                           ocr_records=ocr_records)


# ============================================================
# 라우트: 계약 수정 (납부일 변경 포함)
# ============================================================
@app.route("/contracts/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def contract_edit(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)
    if request.method == "POST":
        deposit = request.form.get("deposit", type=int) or 0
        monthly_rent = request.form.get("monthly_rent", type=int) or 0
        management_fee = request.form.get("management_fee", type=int) or 0
        cleaning_fee = request.form.get("cleaning_fee", type=int) or 0
        extra_person_fee = request.form.get("extra_person_fee", type=int) or 0
        end_date = request.form.get("end_date", "").strip()
        payment_day = request.form.get("payment_day", type=int) or 25
        memo = request.form.get("memo", "").strip()

        # 납부일 변경 처리
        old_day = contract["payment_day"]
        if payment_day != old_day:
            change_date = request.form.get("change_date", "").strip() or today_str()
            reason = request.form.get("change_reason", "").strip()
            # 변경 이력 기록
            db.execute(
                """INSERT INTO payment_day_changes
                   (contract_id, old_day, new_day, changed_date, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (cid, old_day, payment_day, change_date, reason)
            )
            # 계약에 반영
            db.execute(
                """UPDATE contracts SET payment_day=?, payment_day_changed_date=?,
                   original_payment_day=COALESCE(original_payment_day, ?) WHERE id=?""",
                (payment_day, change_date, old_day, cid)
            )
            flash(f"납부일이 {old_day}일 → {payment_day}일로 변경되었습니다. (변경일: {change_date})", "success")
        else:
            flash("계약 정보가 수정되었습니다.", "success")

        db.execute(
            """UPDATE contracts SET deposit=?, monthly_rent=?, management_fee=?, cleaning_fee=?,
               extra_person_fee=?, end_date=?, memo=? WHERE id=?""",
            (deposit, monthly_rent, management_fee, cleaning_fee, extra_person_fee, end_date, memo, cid)
        )
        db.commit()
        return redirect(url_for("contract_view", cid=cid))

    return render_template("contract_edit.html", contract=contract,
                           day_changes=db.execute(
                               "SELECT * FROM payment_day_changes WHERE contract_id = ? ORDER BY changed_date DESC",
                               (cid,)
                           ).fetchall())


# ============================================================
# 라우트: 보증금 분할 납부
# ============================================================
@app.route("/contracts/<int:cid>/deposit/add", methods=["GET", "POST"])
@login_required
def deposit_add(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)

    # 기납부 보증금
    deposit_paid = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM deposit_payments WHERE contract_id = ?",
        (cid,)
    ).fetchone()["total"]
    deposit_balance = contract["deposit"] - deposit_paid

    if request.method == "POST":
        amount = request.form.get("amount", type=int) or 0
        paid_date = request.form.get("paid_date", "").strip() or today_str()
        payment_method = request.form.get("payment_method", "").strip()
        memo = request.form.get("memo", "").strip()

        if amount <= 0:
            flash("납부액은 0보다 커야 합니다.", "danger")
            return redirect(url_for("deposit_add", cid=cid))

        db.execute(
            """INSERT INTO deposit_payments (contract_id, amount, paid_date, payment_method, memo)
               VALUES (?, ?, ?, ?, ?)""",
            (cid, amount, paid_date, payment_method, memo)
        )
        db.commit()

        new_balance = deposit_balance - amount
        if new_balance <= 0:
            flash(f"보증금 완납! ({amount:,}원 납부, 잔액 0원)", "success")
        else:
            flash(f"보증금 납부 등록: {amount:,}원 (잔액 {new_balance:,}원)", "success")
        return redirect(url_for("contract_view", cid=cid))

    return render_template("deposit_form.html", contract=contract,
                           deposit_paid=deposit_paid, deposit_balance=deposit_balance)


@app.route("/contracts/<int:cid>/deposit/<int:did>/delete", methods=["POST"])
@login_required
def deposit_delete(cid, did):
    db = get_db()
    db.execute("DELETE FROM deposit_payments WHERE id = ?", (did,))
    db.commit()
    flash("보증금 납부 내역이 삭제되었습니다.", "info")
    return redirect(url_for("contract_view", cid=cid))


# ============================================================
# 라우트: 계약서 OCR
# ============================================================
import re as re_module
import json


def parse_contract_ocr(text):
    """OCR 추출 텍스트에서 계약서 주요 항목 파싱"""
    result = {}

    # 보증금: "보증금", "보증 금" 뒤의 숫자
    m = re_module.search(r'보증\s*금[^\d]*(\d[\d,]*)', text)
    result["deposit"] = int(m.group(1).replace(",", "")) if m else None

    # 월세: "월세", "차임" 뒤의 숫자
    m = re_module.search(r'(?:월세|차임)[^\d]*(\d[\d,]*)', text)
    result["monthly_rent"] = int(m.group(1).replace(",", "")) if m else None

    # 관리비/관리费
    m = re_module.search(r'관리\s*(?:비|費)[^\d]*(\d[\d,]*)', text)
    result["management_fee"] = int(m.group(1).replace(",", "")) if m else None

    # 퇴실 청소비
    m = re_module.search(r'(?:퇴실\s*청소\s*비|청소\s*비)[^\d]*(\d[\d,]*)', text)
    result["cleaning_fee"] = int(m.group(1).replace(",", "")) if m else None

    # 1인 추가 비용
    m = re_module.search(r'(?:1인\s*추가|추가\s*인원|인원\s*추가)[^\d]*(\d[\d,]*)', text)
    result["extra_person_fee"] = int(m.group(1).replace(",", "")) if m else None

    # 계약기간: 날짜 패턴 (YYYY-MM-DD 또는 YYYY.MM.DD 또는 YYYY년 MM월 DD일)
    dates = re_module.findall(r'(\d{4})[.\-년]+\s*(\d{1,2})[.\-월]+\s*(\d{1,2})', text)
    if dates:
        result["start_date"] = f"{dates[0][0]}-{int(dates[0][1]):02d}-{int(dates[0][2]):02d}"
        if len(dates) > 1:
            result["end_date"] = f"{dates[1][0]}-{int(dates[1][1]):02d}-{int(dates[1][2]):02d}"

    # 계약자/임차인 이름: "임차인", "계약자", "세입자" 뒤의 한글
    m = re_module.search(r'(?:임차인|계약자|세입자)\s*[:：]?\s*([가-힣]{2,4})', text)
    result["tenant_name"] = m.group(1) if m else None

    # 납부일: "납부일", "납입일" 뒤의 숫자
    m = re_module.search(r'(?:납부일|납입일)[^\d]*(\d{1,2})', text)
    result["payment_day"] = int(m.group(1)) if m else None

    return result


@app.route("/contracts/<int:cid>/ocr", methods=["GET", "POST"])
@login_required
def contract_ocr(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action", "ocr")
        photo = request.files.get("photo")

        if not photo or not photo.filename:
            if action == "apply":
                # 파일 없이 apply 요청 — 최근 OCR 기록에서 파싱 데이터 가져오기
                recent = db.execute(
                    "SELECT parsed_data FROM contract_ocrs WHERE contract_id = ? ORDER BY id DESC LIMIT 1",
                    (cid,)
                ).fetchone()
                if recent and recent["parsed_data"]:
                    parsed = json.loads(recent["parsed_data"])
                    updates = []
                    params = []
                    field_map = {
                        "deposit": "deposit", "monthly_rent": "monthly_rent",
                        "management_fee": "management_fee", "cleaning_fee": "cleaning_fee",
                        "extra_person_fee": "extra_person_fee", "start_date": "start_date",
                        "end_date": "end_date", "payment_day": "payment_day",
                    }
                    for key, col in field_map.items():
                        if parsed.get(key) is not None:
                            updates.append(f"{col}=?")
                            params.append(parsed[key])
                    if updates:
                        params.append(cid)
                        db.execute(f"UPDATE contracts SET {', '.join(updates)} WHERE id=?", params)
                        db.commit()
                        flash("OCR 추출 결과가 계약에 반영되었습니다.", "success")
                    else:
                        flash("추출된 항목이 없습니다.", "warning")
                    return redirect(url_for("contract_view", cid=cid))
                flash("OCR 기록이 없습니다. 먼저 사진을 업로드해 주세요.", "danger")
                return redirect(url_for("contract_ocr", cid=cid))
            flash("계약서 사진을 업로드해 주세요.", "danger")
            return redirect(url_for("contract_ocr", cid=cid))

        if not allowed_file(photo.filename):
            flash("지원하지 않는 파일 형식입니다. (jpg, png, webp)", "danger")
            return redirect(url_for("contract_ocr", cid=cid))

        # 파일 저장
        ext = os.path.splitext(photo.filename)[1].lower()
        safe_name = f"contract_ocr_{cid}_{today_str()}{ext}"
        safe_name = secure_filename(safe_name)
        save_path = os.path.join(UPLOAD_DIR, safe_name)
        photo.save(save_path)
        photo_path = f"uploads/{safe_name}"

        # OCR 수행
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(os.path.join(UPLOAD_DIR, safe_name))
            raw_text = pytesseract.image_to_string(img, lang='kor+eng')
        except Exception as e:
            flash(f"OCR 처리 중 오류: {e}", "danger")
            return redirect(url_for("contract_ocr", cid=cid))

        # 파싱
        parsed = parse_contract_ocr(raw_text)

        # OCR 기록 저장
        db.execute(
            """INSERT INTO contract_ocrs (contract_id, photo_path, raw_text, parsed_data)
               VALUES (?, ?, ?, ?)""",
            (cid, photo_path, raw_text, json.dumps(parsed, ensure_ascii=False))
        )
        db.commit()

        if action == "apply":
            # 파싱 결과를 계약에 반영
            updates = []
            params = []
            field_map = {
                "deposit": "deposit", "monthly_rent": "monthly_rent",
                "management_fee": "management_fee", "cleaning_fee": "cleaning_fee",
                "extra_person_fee": "extra_person_fee", "start_date": "start_date",
                "end_date": "end_date", "payment_day": "payment_day",
            }
            for key, col in field_map.items():
                if parsed.get(key) is not None:
                    updates.append(f"{col}=?")
                    params.append(parsed[key])
            if updates:
                params.append(cid)
                db.execute(f"UPDATE contracts SET {', '.join(updates)} WHERE id=?", params)
                db.commit()
                flash("OCR 추출 결과가 계약에 반영되었습니다.", "success")
            else:
                flash("추출된 항목이 없습니다.", "warning")
            return redirect(url_for("contract_view", cid=cid))

        # 결과 표시
        ocr_records = db.execute(
            "SELECT * FROM contract_ocrs WHERE contract_id = ? ORDER BY ocr_date DESC",
            (cid,)
        ).fetchall()
        return render_template("contract_ocr.html", contract=contract,
                               raw_text=raw_text, parsed=parsed,
                               photo_path=photo_path, ocr_records=ocr_records)

    # GET: OCR 페이지
    ocr_records = db.execute(
        "SELECT * FROM contract_ocrs WHERE contract_id = ? ORDER BY ocr_date DESC",
        (cid,)
    ).fetchall()
    return render_template("contract_ocr.html", contract=contract,
                           raw_text=None, parsed=None,
                           photo_path=None, ocr_records=ocr_records)


# ============================================================
# 라우트: 계량기 기록 (입주/퇴거 시 사진 업로드)
# ============================================================
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}


def allowed_file(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


@app.route("/contracts/<int:cid>/meters/add", methods=["GET", "POST"])
@login_required
def meter_add(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)

    if request.method == "POST":
        meter_type = request.form.get("type", "")
        reading_type = request.form.get("reading_type", "")
        reading_date = request.form.get("reading_date", "").strip()
        meter_value = request.form.get("meter_value", type=float)
        memo = request.form.get("memo", "").strip()

        if not meter_type or not reading_type or not reading_date:
            flash("계량기 종류, 구분(입주/퇴거), 날짜는 필수입니다.", "danger")
            return redirect(url_for("meter_add", cid=cid))

        photo_path = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            if allowed_file(photo.filename):
                # 파일 저장: contract_id/type/reading_type_날짜.ext
                ext = os.path.splitext(photo.filename)[1].lower()
                safe_name = f"meter_{cid}_{meter_type}_{reading_type}_{reading_date}{ext}"
                safe_name = secure_filename(safe_name)
                save_path = os.path.join(UPLOAD_DIR, safe_name)
                photo.save(save_path)
                photo_path = f"uploads/{safe_name}"
            else:
                flash("지원하지 않는 파일 형식입니다. (jpg, png, gif, webp)", "danger")
                return redirect(url_for("meter_add", cid=cid))

        db.execute(
            """INSERT INTO meter_readings
               (contract_id, type, reading_type, reading_date, meter_value, photo_path, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cid, meter_type, reading_type, reading_date, meter_value, photo_path, memo)
        )
        db.commit()
        flash("계량기 기록이 저장되었습니다.", "success")
        return redirect(url_for("contract_view", cid=cid))

    return render_template("meter_form.html", contract=contract, meter=None)


@app.route("/contracts/<int:cid>/meters/<int:mid>/delete", methods=["POST"])
@login_required
def meter_delete(cid, mid):
    db = get_db()
    meter = db.execute("SELECT * FROM meter_readings WHERE id = ?", (mid,)).fetchone()
    if not meter:
        abort(404)
    # 사진 파일 삭제
    if meter["photo_path"]:
        full_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "static", meter["photo_path"])
        if os.path.exists(full_path):
            os.remove(full_path)
    db.execute("DELETE FROM meter_readings WHERE id = ?", (mid,))
    db.commit()
    flash("계량기 기록이 삭제되었습니다.", "info")
    return redirect(url_for("contract_view", cid=cid))


# ============================================================
# 라우트: 공과금 기록 (한전/도시가스 문자)
# ============================================================
@app.route("/contracts/<int:cid>/utilities/add", methods=["GET", "POST"])
@login_required
def utility_add(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)

    if request.method == "POST":
        util_type = request.form.get("type", "")
        billing_month = request.form.get("billing_month", "").strip()
        amount = request.form.get("amount", type=int) or 0
        usage_amount = request.form.get("usage_amount", type=float)
        sms_text = request.form.get("sms_text", "").strip()
        sms_date = request.form.get("sms_date", "").strip()
        memo = request.form.get("memo", "").strip()

        if not util_type or not billing_month or not amount:
            flash("종류, 청구월, 금액은 필수입니다.", "danger")
            return redirect(url_for("utility_add", cid=cid))

        db.execute(
            """INSERT INTO utility_bills
               (contract_id, type, billing_month, amount, usage_amount, sms_text, sms_date, status, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'unpaid', ?)""",
            (cid, util_type, billing_month, amount, usage_amount, sms_text, sms_date, memo)
        )
        db.commit()
        flash("공과금 기록이 저장되었습니다.", "success")
        return redirect(url_for("contract_view", cid=cid))

    return render_template("utility_form.html", contract=contract, utility=None)


@app.route("/contracts/<int:cid>/utilities/<int:uid>/pay", methods=["POST"])
@login_required
def utility_pay(cid, uid):
    db = get_db()
    paid_amount = request.form.get("paid_amount", type=int) or 0
    paid_date = request.form.get("paid_date", "").strip() or today_str()
    utility = db.execute("SELECT * FROM utility_bills WHERE id = ?", (uid,)).fetchone()
    if not utility:
        abort(404)
    status = "paid" if paid_amount >= utility["amount"] else "unpaid"
    db.execute(
        "UPDATE utility_bills SET paid_amount=?, paid_date=?, status=? WHERE id=?",
        (paid_amount, paid_date, status, uid)
    )
    db.commit()
    flash("공과금 수납이 등록되었습니다.", "success")
    return redirect(url_for("contract_view", cid=cid))


@app.route("/contracts/<int:cid>/utilities/<int:uid>/delete", methods=["POST"])
@login_required
def utility_delete(cid, uid):
    db = get_db()
    db.execute("DELETE FROM utility_bills WHERE id = ?", (uid,))
    db.commit()
    flash("공과금 기록이 삭제되었습니다.", "info")
    return redirect(url_for("contract_view", cid=cid))


# ============================================================
# 라우트: 퇴거 정산 (미리보기 + 확정)
# ============================================================
@app.route("/contracts/<int:cid>/settlement", methods=["GET", "POST"])
@login_required
def contract_settlement(cid):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name,
                  t.phone as tenant_phone
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = ?""",
        (cid,)
    ).fetchone()
    if not contract:
        abort(404)
    if contract["status"] != "active":
        # 이미 해지된 경우 기존 정산 내역 표시
        settlement = db.execute(
            "SELECT * FROM settlements WHERE contract_id = ? ORDER BY id DESC LIMIT 1",
            (cid,)
        ).fetchone()
        if settlement:
            return render_template("settlement_view.html", contract=contract,
                                   settlement=settlement)
        flash("이미 종료된 계약이며 정산 내역이 없습니다.", "warning")
        return redirect(url_for("contract_view", cid=cid))

    if request.method == "POST":
        action = request.form.get("action", "preview")
        move_out_date = request.form.get("move_out_date", "").strip() or today_str()
        deduction_reason = request.form.get("deduction_reason", "").strip()
        extra_deduction = request.form.get("extra_deduction", type=int) or 0
        electric_bill = request.form.get("electric_bill", type=int) or 0
        gas_bill = request.form.get("gas_bill", type=int) or 0
        cleaning_fee = request.form.get("cleaning_fee", type=int) or contract["cleaning_fee"] or 0
        memo = request.form.get("memo", "").strip()

        # 기본 정산 (일할 월세 + 미납액)
        result = calc_moveout_settlement(contract, move_out_date, db)

        # 공과금 미납액 합산
        unpaid_utilities = db.execute(
            "SELECT COALESCE(SUM(amount - paid_amount), 0) as total FROM utility_bills WHERE contract_id = ? AND status = 'unpaid'",
            (cid,)
        ).fetchone()["total"]

        total_owed = result["total_owed"] + extra_deduction + electric_bill + gas_bill + cleaning_fee + unpaid_utilities

        # 실제 납부된 보증금 계산
        deposit_paid = db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM deposit_payments WHERE contract_id = ?",
            (cid,)
        ).fetchone()["total"]
        deposit = deposit_paid  # 실제 받은 보증금만 반환 대상
        deposit_contract = contract["deposit"]  # 계약상 보증금 (참고용)

        deposit_deduction = min(total_owed, deposit)
        deposit_return = deposit - total_owed
        if deposit_return < 0:
            deposit_return = 0
            final_settlement = -(total_owed - deposit)  # 임차인이 추가 지급
        else:
            final_settlement = deposit_return

        if action == "confirm":
            db.execute(
                """INSERT INTO settlements
                   (contract_id, move_out_date, last_paid_month, last_paid_date,
                    days_used, daily_rent, daily_mgmt, prorated_rent, prorated_mgmt,
                    unpaid_rent, unpaid_mgmt, electric_bill, gas_bill, cleaning_fee,
                    extra_deduction, total_owed, deposit_return, deposit_deduction,
                    deduction_reason, final_settlement, settlement_date, status, memo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)""",
                (cid, move_out_date, result["last_paid_month"], result["last_paid_date"],
                 result["days_used"], result["daily_rent"], result["daily_mgmt"],
                 result["prorated_rent"], result["prorated_mgmt"],
                 result["unpaid_amount"], 0,
                 electric_bill, gas_bill, cleaning_fee,
                 extra_deduction, total_owed, deposit_return, deposit_deduction,
                 deduction_reason, final_settlement, today_str(), memo)
            )
            db.execute(
                "UPDATE contracts SET status='terminated', terminated_date=? WHERE id=?",
                (move_out_date, cid)
            )
            db.execute("UPDATE units SET status='공실' WHERE id=?", (contract["unit_id"],))
            db.commit()
            flash(f"퇴거 정산이 완료되었습니다. 정산금: {final_settlement:,}원", "success")
            return redirect(url_for("contract_view", cid=cid))

        # preview
        preview = {
            "move_out_date": move_out_date,
            "last_paid_month": result["last_paid_month"],
            "last_paid_date": result["last_paid_date"],
            "days_used": result["days_used"],
            "daily_rent": result["daily_rent"],
            "daily_mgmt": result["daily_mgmt"],
            "prorated_rent": result["prorated_rent"],
            "prorated_mgmt": result["prorated_mgmt"],
            "unpaid_amount": result["unpaid_amount"],
            "unpaid_utilities": unpaid_utilities,
            "electric_bill": electric_bill,
            "gas_bill": gas_bill,
            "cleaning_fee": cleaning_fee,
            "extra_deduction": extra_deduction,
            "total_owed": total_owed,
            "deposit": deposit,
            "deposit_paid": deposit_paid,
            "deposit_contract": deposit_contract,
            "deposit_deduction": deposit_deduction,
            "deposit_return": deposit_return,
            "final_settlement": final_settlement,
            "deduction_reason": deduction_reason,
            "memo": memo,
        }
        return render_template("settlement_form.html", contract=contract, preview=preview)

    # GET: 정산 입력 폼
    payments = db.execute(
        "SELECT * FROM payments WHERE contract_id = ? ORDER BY billing_month DESC",
        (cid,)
    ).fetchall()
    # 계량기 기록
    meters = db.execute(
        "SELECT * FROM meter_readings WHERE contract_id = ? ORDER BY reading_type, type, reading_date",
        (cid,)
    ).fetchall()
    # 공과금
    utilities = db.execute(
        "SELECT * FROM utility_bills WHERE contract_id = ? ORDER BY billing_month DESC, type",
        (cid,)
    ).fetchall()
    unpaid_utility_total = db.execute(
        "SELECT COALESCE(SUM(amount - paid_amount), 0) as total FROM utility_bills WHERE contract_id = ? AND status = 'unpaid'",
        (cid,)
    ).fetchone()["total"]
    # 보증금 납부 이력
    deposit_paid = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM deposit_payments WHERE contract_id = ?",
        (cid,)
    ).fetchone()["total"]
    deposit_balance = contract["deposit"] - deposit_paid
    return render_template("settlement_form.html", contract=contract,
                           preview=None, payments=payments, meters=meters,
                           utilities=utilities, unpaid_utility_total=unpaid_utility_total,
                           deposit_paid=deposit_paid, deposit_balance=deposit_balance)


# 기존 단순 해지 (정산 없이) — 정산 페이지로 유도
@app.route("/contracts/<int:cid>/terminate", methods=["POST"])
@login_required
def contract_terminate(cid):
    db = get_db()
    contract = db.execute("SELECT * FROM contracts WHERE id = ?", (cid,)).fetchone()
    if not contract:
        abort(404)
    if contract["status"] != "active":
        flash("이미 종료된 계약입니다.", "warning")
        return redirect(url_for("contract_list"))
    flash("퇴거 시 정산을 먼저 진행해 주세요. 아래 버튼을 눌러 정산 페이지로 이동합니다.", "info")
    return redirect(url_for("contract_settlement", cid=cid))


# ============================================================
# 라우트: 월세 청구 및 수납
# ============================================================
@app.route("/payments")
@login_required
def payment_list():
    db = get_db()
    update_overdue_status()

    status_filter = request.args.get("status", "")
    month_filter = request.args.get("month", "")

    query = """
        SELECT p.*, c.id as contract_id, u.unit_number, b.name as building_name,
               t.name as tenant_name
        FROM payments p
        JOIN contracts c ON c.id = p.contract_id
        JOIN units u ON u.id = c.unit_id
        JOIN buildings b ON b.id = u.building_id
        JOIN tenants t ON t.id = c.tenant_id
    """
    conditions = []
    params = []
    if status_filter:
        conditions.append("p.status = ?")
        params.append(status_filter)
    if month_filter:
        conditions.append("p.billing_month = ?")
        params.append(month_filter)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY p.billing_month DESC, p.due_date"

    payments = db.execute(query, params).fetchall()

    # 통계
    total_billed = sum(p["amount"] for p in payments)
    total_paid = sum(p["paid_amount"] for p in payments)
    total_unpaid = total_billed - total_paid

    return render_template(
        "payment_list.html",
        payments=payments, status_filter=status_filter, month_filter=month_filter,
        total_billed=total_billed, total_paid=total_paid, total_unpaid=total_unpaid
    )


@app.route("/payments/generate", methods=["GET", "POST"])
@login_required
def payment_generate():
    """특정 월의 청구를 일괄 생성"""
    db = get_db()
    if request.method == "POST":
        billing_month = request.form.get("billing_month", "").strip()  # YYYY-MM
        if not billing_month:
            flash("청구월을 입력하세요.", "danger")
            return redirect(url_for("payment_generate"))

        # 활성 계약 중 이미 해당 월 청구가 없는 것만 생성
        active_contracts = db.execute(
            "SELECT * FROM contracts WHERE status = 'active'"
        ).fetchall()

        created = 0
        skipped = 0
        for c in active_contracts:
            existing = db.execute(
                "SELECT id FROM payments WHERE contract_id = ? AND billing_month = ?",
                (c["id"], billing_month)
            ).fetchone()
            if existing:
                skipped += 1
                continue

            amount = c["monthly_rent"] + c["management_fee"]
            # 납부기한 = 청구월의 payment_day
            due_date = f"{billing_month}-{c['payment_day']:02d}"

            db.execute(
                """INSERT INTO payments (contract_id, billing_month, due_date, amount, status)
                   VALUES (?, ?, ?, ?, 'unpaid')""",
                (c["id"], billing_month, due_date, amount)
            )
            created += 1

        db.commit()
        flash(f"청구 생성 완료: {created}건 생성, {skipped}건 스킵(이미 존재).", "success")
        return redirect(url_for("payment_list", month=billing_month))

    # GET: 미리보기
    active_contracts = db.execute(
        """SELECT c.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM contracts c
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE c.status = 'active'
           ORDER BY b.name, u.unit_number"""
    ).fetchall()
    return render_template("payment_generate.html", contracts=active_contracts)


@app.route("/payments/<int:pid>/collect", methods=["GET", "POST"])
@login_required
def payment_collect(pid):
    """수납 등록"""
    db = get_db()
    payment = db.execute(
        """SELECT p.*, c.id as contract_id, u.unit_number, b.name as building_name,
                  t.name as tenant_name
           FROM payments p
           JOIN contracts c ON c.id = p.contract_id
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE p.id = ?""",
        (pid,)
    ).fetchone()
    if not payment:
        abort(404)

    if request.method == "POST":
        paid_amount = request.form.get("paid_amount", type=int) or 0
        paid_date = request.form.get("paid_date", "").strip() or today_str()
        payment_method = request.form.get("payment_method", "").strip()
        sender_name = request.form.get("sender_name", "").strip()
        deposit_memo = request.form.get("deposit_memo", "").strip()
        memo = request.form.get("memo", "").strip()

        new_total = payment["paid_amount"] + paid_amount
        if new_total >= payment["amount"]:
            status = "paid"
        else:
            status = "partial"

        db.execute(
            """UPDATE payments 
               SET paid_amount = ?, paid_date = ?, status = ?, payment_method = ?,
                   sender_name = ?, deposit_memo = ?, memo = ?
               WHERE id = ?""",
            (new_total, paid_date, status, payment_method, sender_name, deposit_memo, memo, pid)
        )
        db.commit()
        flash(f"수납 등록: {paid_amount:,}원 (상태: {status})", "success")
        return redirect(url_for("payment_list"))

    return render_template("payment_collect.html", payment=payment)


@app.route("/payments/overdue")
@login_required
def overdue_list():
    """연체 목록"""
    db = get_db()
    update_overdue_status()
    overdue = db.execute(
        """SELECT p.*, u.unit_number, b.name as building_name, t.name as tenant_name,
                  t.phone as tenant_phone
           FROM payments p
           JOIN contracts c ON c.id = p.contract_id
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE p.status = 'overdue'
           ORDER BY p.due_date"""
    ).fetchall()
    total_overdue = sum(p["amount"] - p["paid_amount"] for p in overdue)
    return render_template("overdue_list.html", overdue=overdue, total_overdue=total_overdue)


# ============================================================
# 라우트: 장부 (은행 입출금 내역)
# ============================================================

# 은행별 SMS 패턴 정의
BANK_PATTERNS = [
    # 국민은행: "KB국민은행 05/27 13:20 승인 [입금] 550,000원 홍길동 101호월세 잔액 3,200,000원"
    {"bank": "국민", "patterns": [
        r'(?:KB)?국민[^0-9]*(\d{2}/\d{2})\s*(\d{2}:\d{2})[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'(?:KB)?국민[^0-9]*(\d{2}/\d{2})\s*(\d{2}:\d{2})[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"], "order": ["date", "time", "amount", "sender"]},
    # 신한은행: "신한은행 [입금] 550,000원 05/27 13:20 홍길동 잔액 3,200,000원"
    {"bank": "신한", "patterns": [
        r'신한[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+잔액|\s*$)',
        r'신한[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
    # 카카오뱅크: "카카오뱅크 [입금] 550,000원 05/27 13:20 홍길동 101호"
    {"bank": "카카오", "patterns": [
        r'카카오[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'카카오[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
    # 하나은행/KEB: "하나은행 [입금] 550,000원 05/27 13:20 홍길동"
    {"bank": "하나", "patterns": [
        r'하나[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'하나[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
    # 우리은행: "우리은행 [입금] 550,000원 05/27 13:20 홍길동"
    {"bank": "우리", "patterns": [
        r'우리[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'우리[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
    # NH농협: "농협 [입금] 550,000원 05/27 13:20 홍길동"
    {"bank": "농협", "patterns": [
        r'(?:NH)?농협[^0-9]*\[?입금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'(?:NH)?농협[^0-9]*\[?출금\]?\s*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
    # 일반 패턴: 입금/출금 키워드 + 금액 + 날짜/시간
    {"bank": "기타", "patterns": [
        r'입금[^0-9]*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
        r'출금[^0-9]*([\d,]+)\s*원?\s*(\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(.+?)(?:\s+\d[\d,]*\s*원|\s+잔액|\s*$)',
    ], "type": ["deposit", "withdraw"]},
]


def parse_sms(sms_text):
    """은행 SMS/알림톡에서 입출금 정보 추출"""
    sms_text = sms_text.strip()
    for bank_info in BANK_PATTERNS:
        order = bank_info.get("order", ["amount", "date", "time", "sender"])
        for idx, pattern in enumerate(bank_info["patterns"]):
            m = re_module.search(pattern, sms_text)
            if m:
                groups = m.groups()
                # order에 따라 groups 매핑
                parts = {"amount": None, "date": None, "time": None, "sender": ""}
                for i, key in enumerate(order):
                    if i < len(groups):
                        parts[key] = groups[i]

                amount_str = (parts["amount"] or "0").replace(",", "")
                amount = int(amount_str)
                date_str = parts["date"] or ""
                time_str = parts["time"] or "00:00"
                sender = (parts["sender"] or "").strip()

                from datetime import datetime as dt
                year = dt.now().year
                tx_date = f"{year}-{date_str.replace('/', '-')}" if "/" in date_str else date_str
                tx_date = f"{tx_date} {time_str}"

                bal_match = re_module.search(r'잔액\s*([\d,]+)\s*원', sms_text)
                balance = int(bal_match.group(1).replace(",", "")) if bal_match else None

                acct_match = re_module.search(r'(\d{4})\s*으로', sms_text)
                acct_last4 = acct_match.group(1) if acct_match else None

                return {
                    "tx_type": bank_info["type"][idx],
                    "amount": amount,
                    "tx_date": tx_date,
                    "sender_name": sender,
                    "memo": sender,
                    "bank_name": bank_info["bank"],
                    "account_last4": acct_last4,
                    "balance": balance,
                    "raw_sms": sms_text,
                }
    return None


@app.route("/ledger")
@login_required
def ledger_list():
    db = get_db()
    status_filter = request.args.get("status", "")
    type_filter = request.args.get("type", "")

    query = "SELECT * FROM ledger"
    conditions = []
    params = []
    if status_filter:
        conditions.append("matched_status = ?")
        params.append(status_filter)
    if type_filter:
        conditions.append("tx_type = ?")
        params.append(type_filter)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY tx_date DESC"

    entries = db.execute(query, params).fetchall()

    # 통계
    total_deposit = sum(e["amount"] for e in entries if e["tx_type"] == "deposit")
    total_withdraw = sum(e["amount"] for e in entries if e["tx_type"] == "withdraw")
    unmatched = sum(1 for e in entries if e["matched_status"] == "unmatched")

    return render_template("ledger_list.html", entries=entries,
                           total_deposit=total_deposit, total_withdraw=total_withdraw,
                           unmatched=unmatched, status_filter=status_filter, type_filter=type_filter)


@app.route("/ledger/add", methods=["GET", "POST"])
@login_required
def ledger_add():
    db = get_db()
    if request.method == "POST":
        sms_text = request.form.get("sms_text", "").strip()
        manual_mode = request.form.get("manual_mode") == "1"

        if manual_mode:
            # 수동 입력
            tx_type = request.form.get("tx_type", "deposit")
            amount = request.form.get("amount", type=int) or 0
            tx_date = request.form.get("tx_date", "").strip() or today_str()
            sender_name = request.form.get("sender_name", "").strip()
            memo = request.form.get("memo", "").strip()
            bank_name = request.form.get("bank_name", "").strip()

            if amount <= 0:
                flash("금액을 입력해 주세요.", "danger")
                return redirect(url_for("ledger_add"))

            db.execute(
                """INSERT INTO ledger (tx_date, tx_type, amount, sender_name, memo, bank_name, raw_sms, matched_status)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, 'unmatched')""",
                (tx_date, tx_type, amount, sender_name, memo, bank_name)
            )
            db.commit()
            flash(f"장부에 등록: {tx_type} {amount:,}원", "success")
            return redirect(url_for("ledger_list"))

        # SMS 파싱
        if not sms_text:
            flash("SMS/알림톡 내용을 붙여넣어 주세요.", "danger")
            return redirect(url_for("ledger_add"))

        parsed = parse_sms(sms_text)
        if not parsed:
            flash("입출금 내용을 인식하지 못했습니다. 수동으로 입력해 주세요.", "warning")
            return redirect(url_for("ledger_add"))

        db.execute(
            """INSERT INTO ledger (tx_date, tx_type, amount, sender_name, memo, bank_name,
               account_last4, raw_sms, matched_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unmatched')""",
            (parsed["tx_date"], parsed["tx_type"], parsed["amount"],
             parsed["sender_name"], parsed["memo"], parsed["bank_name"],
             parsed.get("account_last4"), parsed["raw_sms"])
        )
        db.commit()
        flash(f"파싱 완료: {parsed['tx_type']} {parsed['amount']:,}원 / {parsed['sender_name']} / {parsed['bank_name']}", "success")
        return redirect(url_for("ledger_list"))

    return render_template("ledger_form.html", parsed=None)


@app.route("/ledger/<int:lid>/delete", methods=["POST"])
@login_required
def ledger_delete(lid):
    db = get_db()
    entry = db.execute("SELECT * FROM ledger WHERE id = ?", (lid,)).fetchone()
    if not entry:
        abort(404)
    # 매칭된 수납이 있으면 매칭 해제
    if entry["matched_payment_id"]:
        db.execute("UPDATE payments SET status='unpaid' WHERE id = ?",
                   (entry["matched_payment_id"],))
    db.execute("DELETE FROM ledger WHERE id = ?", (lid,))
    db.commit()
    flash("장부 내역이 삭제되었습니다.", "info")
    return redirect(url_for("ledger_list"))


@app.route("/ledger/<int:lid>/ignore", methods=["POST"])
@login_required
def ledger_ignore(lid):
    """매칭 제외 처리"""
    db = get_db()
    db.execute("UPDATE ledger SET matched_status='ignored' WHERE id=?", (lid,))
    db.commit()
    flash("매칭 제외 처리되었습니다.", "info")
    return redirect(url_for("ledger_list"))


@app.route("/ledger/<int:lid>/match", methods=["GET", "POST"])
@login_required
def ledger_match(lid):
    """장부 내역을 월세 수납에 수동 매칭"""
    db = get_db()
    entry = db.execute("SELECT * FROM ledger WHERE id = ?", (lid,)).fetchone()
    if not entry:
        abort(404)

    if request.method == "POST":
        payment_id = request.form.get("payment_id", type=int)
        if payment_id:
            payment = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if payment:
                # 수납 처리
                new_paid = payment["paid_amount"] + entry["amount"]
                status = "paid" if new_paid >= payment["amount"] else "partial"
                db.execute(
                    "UPDATE payments SET paid_amount=?, paid_date=?, status=?, "
                    "payment_method='장부매칭', sender_name=?, deposit_memo=? WHERE id=?",
                    (new_paid, entry["tx_date"][:10], status,
                     entry["sender_name"], entry["memo"], payment_id)
                )
                db.execute(
                    "UPDATE ledger SET matched_status='matched', matched_payment_id=?, matched_contract_id=? WHERE id=?",
                    (payment_id, payment["contract_id"], lid)
                )
                db.commit()
                flash(f"수납 매칭 완료: {entry['amount']:,}원 → {payment['billing_month']}월세", "success")
        return redirect(url_for("ledger_list"))

    # GET: 매칭 가능한 수납 목록 표시
    # 입금액과 비슷한 미납 청구 찾기
    candidates = db.execute(
        """SELECT p.*, u.unit_number, b.name as building_name, t.name as tenant_name
           FROM payments p
           JOIN contracts c ON c.id = p.contract_id
           JOIN units u ON u.id = c.unit_id
           JOIN buildings b ON b.id = u.building_id
           JOIN tenants t ON t.id = c.tenant_id
           WHERE p.status IN ('unpaid', 'partial', 'overdue')
           ORDER BY CASE WHEN ABS(p.amount - p.paid_amount - ?) < 50000 THEN 0 ELSE 1 END,
                    p.due_date""",
        (entry["amount"],)
    ).fetchall()

    return render_template("ledger_match.html", entry=entry, candidates=candidates)


# ============================================================
# 라우트: 공지사항
# ============================================================
NOTICE_CATEGORIES = {
    "general": ("일반", "secondary"),
    "urgent": ("긴급", "danger"),
    "maintenance": ("점검", "warning"),
    "rent": ("수납", "info"),
}


@app.route("/notices")
@login_required
def notice_list():
    db = get_db()
    category = request.args.get("category", "")
    building_id = request.args.get("building", "")

    query = """
        SELECT n.*, b.name as building_name,
               (SELECT COUNT(*) FROM notice_reads nr WHERE nr.notice_id = n.id AND nr.user_id = ?) as is_read
        FROM notices n
        LEFT JOIN buildings b ON b.id = n.target_building_id
    """
    params = [session.get("user_id")]
    where = []
    if category:
        where.append("n.category = ?")
        params.append(category)
    if building_id:
        where.append("n.target_building_id = ?")
        params.append(building_id)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY n.is_pinned DESC, n.created_at DESC"

    notices = db.execute(query, params).fetchall()
    buildings = db.execute("SELECT id, name FROM buildings ORDER BY name").fetchall()
    return render_template(
        "notice_list.html", notices=notices, buildings=buildings,
        categories=NOTICE_CATEGORIES,
        sel_category=category, sel_building=building_id,
    )


@app.route("/notices/add", methods=["GET", "POST"])
@login_required
def notice_add():
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "general")
        is_pinned = 1 if request.form.get("is_pinned") else 0
        target_building_id = request.form.get("target_building_id") or None

        if not title or not body:
            flash("제목과 내용을 입력하세요.", "danger")
            return redirect(url_for("notice_add"))

        db.execute(
            """INSERT INTO notices (title, body, category, is_pinned, target_building_id, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, body, category, is_pinned, target_building_id, g.user["name"]),
        )
        db.commit()
        flash("공지가 등록되었습니다.", "success")
        return redirect(url_for("notice_list"))

    buildings = db.execute("SELECT id, name FROM buildings ORDER BY name").fetchall()
    return render_template("notice_form.html", buildings=buildings,
                           categories=NOTICE_CATEGORIES, notice=None)


@app.route("/notices/<int:nid>/view")
@login_required
def notice_view(nid):
    db = get_db()
    notice = db.execute(
        """SELECT n.*, b.name as building_name
           FROM notices n
           LEFT JOIN buildings b ON b.id = n.target_building_id
           WHERE n.id = ?""",
        (nid,),
    ).fetchone()
    if not notice:
        abort(404)

    # 읽음 처리
    user_id = session.get("user_id")
    existing = db.execute(
        "SELECT id FROM notice_reads WHERE notice_id = ? AND user_id = ?",
        (nid, user_id),
    ).fetchone()
    if not existing:
        db.execute(
            "INSERT INTO notice_reads (notice_id, user_id) VALUES (?, ?)",
            (nid, user_id),
        )
        db.commit()

    return render_template("notice_view.html", notice=notice,
                            categories=NOTICE_CATEGORIES)


@app.route("/notices/<int:nid>/edit", methods=["GET", "POST"])
@login_required
def notice_edit(nid):
    db = get_db()
    notice = db.execute("SELECT * FROM notices WHERE id = ?", (nid,)).fetchone()
    if not notice:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "general")
        is_pinned = 1 if request.form.get("is_pinned") else 0
        target_building_id = request.form.get("target_building_id") or None

        if not title or not body:
            flash("제목과 내용을 입력하세요.", "danger")
            return redirect(url_for("notice_edit", nid=nid))

        db.execute(
            """UPDATE notices SET title=?, body=?, category=?, is_pinned=?,
               target_building_id=?, updated_at=datetime('now','localtime')
               WHERE id=?""",
            (title, body, category, is_pinned, target_building_id, nid),
        )
        db.commit()
        flash("공지가 수정되었습니다.", "success")
        return redirect(url_for("notice_view", nid=nid))

    buildings = db.execute("SELECT id, name FROM buildings ORDER BY name").fetchall()
    return render_template("notice_form.html", buildings=buildings,
                           categories=NOTICE_CATEGORIES, notice=notice)


@app.route("/notices/<int:nid>/delete", methods=["POST"])
@login_required
def notice_delete(nid):
    db = get_db()
    db.execute("DELETE FROM notice_reads WHERE notice_id = ?", (nid,))
    db.execute("DELETE FROM notices WHERE id = ?", (nid,))
    db.commit()
    flash("공지가 삭제되었습니다.", "success")
    return redirect(url_for("notice_list"))


@app.route("/notices/<int:nid>/pin", methods=["POST"])
@login_required
def notice_toggle_pin(nid):
    db = get_db()
    notice = db.execute("SELECT is_pinned FROM notices WHERE id = ?", (nid,)).fetchone()
    if not notice:
        abort(404)
    new_val = 0 if notice["is_pinned"] else 1
    db.execute("UPDATE notices SET is_pinned=? WHERE id=?", (new_val, nid))
    db.commit()
    flash("고정 상태가 변경되었습니다.", "success")
    return redirect(url_for("notice_list"))


# ============================================================
# 에러 핸들러
# ============================================================
@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="페이지를 찾을 수 없습니다."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="서버 오류가 발생했습니다."), 500


# ============================================================
# 메인
# ============================================================
if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("  임대관리 앱 시작")
    print("  http://localhost:5000")
    print("  기본 관리자: admin / admin123")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5050, debug=True, use_reloader=False)