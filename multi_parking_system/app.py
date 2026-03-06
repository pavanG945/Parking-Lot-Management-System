print("APP FILE IS RUNNING")
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import time

from parking.vehicle import Car, Bike
from parking.floor import ParkingFloor
from parking.parking_lot import ParkingLot

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///parking.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class TicketRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vehicle_number = db.Column(db.String(64), nullable=False, index=True)
    vehicle_type = db.Column(db.String(16), nullable=False)
    floor_no = db.Column(db.Integer, nullable=False)
    slot_id = db.Column(db.String(16), nullable=False)
    entry_time = db.Column(db.Float, nullable=False)
    exit_time = db.Column(db.Float)
    fee = db.Column(db.Integer)
    is_active = db.Column(db.Boolean, default=True, index=True)


class SystemMeta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    last_reset_time = db.Column(db.Float)


# Parking lot for Theeta Parking – fixed layout:
# - Floor 1: CAR slots only
# - Floor 2: BIKE slots only
parking_lot = None


def build_fixed_parking_lot():
    floors_list = []

    # Floor 1 – cars only
    floors_list.append(ParkingFloor(1, {"CAR": 20}))

    # Floor 2 – bikes only
    floors_list.append(ParkingFloor(2, {"BIKE": 40}))

    return ParkingLot(floors_list)


def get_or_create_system_meta():
    meta = SystemMeta.query.first()
    if not meta:
        meta = SystemMeta(last_reset_time=None)
        db.session.add(meta)
        db.session.commit()
    return meta


def load_state_from_db():
    """
    Initialize parking_lot from fixed layout and active tickets.
    """
    global parking_lot

    parking_lot = build_fixed_parking_lot()

    # Ensure metadata row exists
    get_or_create_system_meta()

    active_records = TicketRecord.query.filter_by(is_active=True).all()
    for record in active_records:
        if record.vehicle_type == "CAR":
            vehicle = Car(record.vehicle_number)
        else:
            vehicle = Bike(record.vehicle_number)

        slot = parking_lot.find_slot(record.floor_no, record.slot_id)
        if not slot:
            continue

        slot.park(vehicle)
        ticket = parking_lot.active_tickets.get(vehicle.number)
        if ticket:
            # If already created somehow, skip
            continue

        from parking.ticket import ParkingTicket

        restored_ticket = ParkingTicket(
            vehicle, record.floor_no, slot, entry_time=record.entry_time
        )
        parking_lot.active_tickets[vehicle.number] = restored_ticket


with app.app_context():
    db.create_all()
    load_state_from_db()


@app.template_filter("datetimeformat")
def datetimeformat_filter(value):
    if not value:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    except (TypeError, ValueError):
        return "-"


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global parking_lot

    # Always show the fixed configuration for Theeta Parking.
    return render_template("setup.html")


@app.route("/park", methods=["GET", "POST"])
def park():
    if request.method == "POST":
        number = request.form["vehicle_number"]
        vehicle_type = request.form["vehicle_type"]

        if vehicle_type == "CAR":
            vehicle = Car(number)
        else:
            vehicle = Bike(number)

        ticket = parking_lot.park_vehicle(vehicle)

        if ticket:
            # Persist ticket in DB
            record = TicketRecord(
                vehicle_number=vehicle.number,
                vehicle_type=vehicle.get_type(),
                floor_no=ticket.floor_no,
                slot_id=ticket.slot.slot_id,
                entry_time=ticket.entry_time,
                is_active=True,
            )
            db.session.add(record)
            db.session.commit()
            return render_template("park_result.html", ticket=ticket)

        return render_template("park_result.html", ticket=None, error="Parking is full")

    return render_template("park.html")


@app.route("/exit", methods=["GET", "POST"])
def exit_vehicle():
    if request.method == "POST":
        number = request.form["vehicle_number"]

        ticket, fee = parking_lot.exit_vehicle(number)

        if ticket:
            # Update DB record
            record = TicketRecord.query.filter_by(
                vehicle_number=number, is_active=True
            ).first()
            if record:
                record.exit_time = time.time()
                record.fee = fee
                record.is_active = False
                db.session.commit()

            duration = ticket.get_human_readable_duration()
            return render_template(
                "exit_result.html",
                ticket=ticket,
                fee=fee,
                duration=duration,
                rate_per_hour=ticket.vehicle.get_rate_per_hour(),
            )

        return render_template(
            "exit_result.html", ticket=None, fee=None, error="Invalid vehicle number"
        )

    return render_template("exit.html")


@app.route("/")
def home():
    summary = parking_lot.get_occupancy_summary() if parking_lot else None
    return render_template("home.html", summary=summary)


@app.route("/dashboard")
def dashboard():
    summary = parking_lot.get_occupancy_summary() if parking_lot else None
    meta = SystemMeta.query.first()
    last_reset_display = None
    if meta and meta.last_reset_time:
        last_reset_display = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(meta.last_reset_time)
        )
    return render_template(
        "dashboard.html", summary=summary, last_reset_time=last_reset_display
    )


@app.route("/floors")
def floors_view():
    """
    Visual view of each floor and slot.
    """
    if not parking_lot:
        return redirect(url_for("home"))
    return render_template("floors.html", floors=parking_lot.floors)


@app.route("/search", methods=["GET", "POST"])
def search_vehicle():
    """
    Search active and past tickets by vehicle number.
    """
    vehicle_number = None
    active_records = []
    history_records = []

    if request.method == "POST":
        vehicle_number = request.form.get("vehicle_number", "").strip()
        if vehicle_number:
            active_records = TicketRecord.query.filter_by(
                vehicle_number=vehicle_number, is_active=True
            ).all()
            history_records = (
                TicketRecord.query.filter_by(
                    vehicle_number=vehicle_number, is_active=False
                )
                .order_by(TicketRecord.entry_time.desc())
                .all()
            )

    return render_template(
        "search.html",
        vehicle_number=vehicle_number,
        active_records=active_records,
        history_records=history_records,
    )


@app.route("/reset", methods=["POST"])
def reset_system():
    """
    Clear active tickets and rebuild the fixed parking lot layout.
    """
    global parking_lot

    TicketRecord.query.delete()
    db.session.commit()

    # Update reset time and rebuild parking lot
    meta = get_or_create_system_meta()
    meta.last_reset_time = time.time()
    db.session.commit()

    load_state_from_db()

    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=True)