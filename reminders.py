"""The reminder engine — the heart of RentPing.

run_reminders() is meant to run once a day (Railway cron job, see README).
For each tenant it figures out where today falls relative to their rent due
date and fires the matching stage:

    -3 days  -> "before" reminder   ("rent is due in 3 days")
     due date -> "due" reminder     ("rent is due today")
    +3 days  -> "late3" nudge       (only if that period is still unpaid)
    +7 days  -> "late7" nudge       (only if that period is still unpaid)
The reminder_log table guarantees each (tenant, period, stage) fires exactly
once — re-running the engine never double-texts anyone.

Set RENTPING_TODAY=YYYY-MM-DD to pretend it is a different date (used by the
automated test, and handy for demos).
"""
import os
from datetime import date, timedelta

from store import (all_landlords, all_tenants, get_templates,
                   mark_reminder_sent, reminder_already_sent)
from sms import send_sms

# (stage name, days until the due date: +3 = three days before it's due,
#  0 = due today, -3 / -7 = three / seven days overdue)
STAGES = (("before", 3), ("due", 0), ("late3", -3), ("late7", -7))
LATE_STAGES = ("late3", "late7")


def today() -> date:
    override = os.getenv("RENT_PING_TODAY", "")
    if override:
        y, m, d = map(int, override.split("-"))
        return date(y, m, d)
    return date.today()


def period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def shift_month(period: str, delta: int) -> str:
    """'2026-09' shifted by delta months -> '2026-10' etc."""
    y, m = map(int, period.split("-"))
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def due_date_for(period: str, due_day: int) -> date:
    """The calendar date rent is due for a period. due_day is the day of the
    month (1-31); in short months it clamps to the last day (e.g. due day 31
    in February -> Feb 28)."""
    import calendar
    y, m = map(int, period.split("-"))
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(due_day, last))


def open_period(tenant, day: date, skip_paid: bool = True) -> str:
    """The rent period the tenant currently owes for.

    Normally that's the period of the most recent due date on or before today —
    unless that period is already paid, in which case it's the next one.
    (Paying early for next month must not make this month look unpaid, and an
    old unpaid month stays "open" until it is actually paid.)

    A tenant can never owe for a period whose due date passed before they were
    added — a tenant created today must not show up as "Late (28d)".

    skip_paid=False returns the period of the most recent due date regardless
    of payment; tenant_status uses it to decide whether to show "Paid".
    """
    if day.day >= tenant["due_day"]:
        candidate = period_of(day)
    else:
        candidate = shift_month(period_of(day), -1)
    created = tenant["created_at"][:10]  # "YYYY-MM-DD" prefix of the ISO timestamp
    while due_date_for(candidate, tenant["due_day"]).isoformat() < created:
        candidate = shift_month(candidate, +1)
    if skip_paid and tenant["paid_period"] == candidate:
        return shift_month(candidate, +1)
    return candidate


def tenant_status(tenant, day: date) -> dict:
    """Human-friendly status for the dashboard: Paid / Due in N days /
    Due today / Late (N days) / Opted out."""
    if tenant["opted_out"]:
        return {"label": "Opted out", "kind": "muted"}
    # "Paid" is judged against the most recent due period even if it is paid
    # (open_period skips paid periods, so it can never report "Paid" itself).
    if tenant["paid_period"] == open_period(tenant, day, skip_paid=False):
        return {"label": "Paid", "kind": "paid"}
    period = open_period(tenant, day)
    due = due_date_for(period, tenant["due_day"])
    delta = (due - day).days
    if delta > 0:
        return {"label": f"Due in {delta}d", "kind": "upcoming"}
    if delta == 0:
        return {"label": "Due today", "kind": "due"}
    return {"label": f"Late ({-delta}d)", "kind": "late"}


def render_template(template: str, tenant, property_name: str, due: date) -> str:
    return template.format(
        name=tenant["name"].split()[0],          # first name feels personal
        amount=f"{tenant['rent_amount']:,.2f}",
        property=property_name,
        due_date=due.strftime("%b %d"),
    )


def run_reminders(day: date | None = None, landlord_id: int | None = None) -> list[dict]:
    """Send every reminder that is due on `day`. Returns what was sent.

    landlord_id scopes the run to one landlord (the dashboard button);
    without it, every landlord is processed (the daily cron).
    """
    day = day or today()
    # Look at last month, this month, next month so late nudges for a due date
    # early in the month (e.g. Sep 27 + 7 = Oct 4) are not missed.
    periods = [shift_month(period_of(day), d) for d in (-1, 0, 1)]
    sent: list[dict] = []

    landlords = [l for l in all_landlords() if landlord_id is None or l["id"] == landlord_id]
    for landlord in landlords:
        templates = get_templates(landlord["id"])
        for tenant in all_tenants(landlord["id"]):
            try:
                _remind_tenant(tenant, landlord["id"], templates, day, periods, sent)
            except Exception as exc:  # one bad tenant must never kill the run
                print(f"[reminders] tenant {tenant['id']} failed: {exc!r}", flush=True)
                sent.append({"tenant": tenant["name"], "phone": tenant["phone"],
                             "error": str(exc)})
    return sent


def _remind_tenant(tenant, landlord_id, templates, day, periods, sent):
    """All reminder logic for a single tenant (may raise; caller catches)."""
    if tenant["opted_out"]:
        return  # STOP was honored — never text them again
    for period in periods:
        due = due_date_for(period, tenant["due_day"])
        delta = (due - day).days
        for stage, offset in STAGES:
            if delta != offset:
                continue
            # Late nudges only make sense while the period is unpaid.
            if stage in LATE_STAGES and tenant["paid_period"] == period:
                continue
            if reminder_already_sent(tenant["id"], period, stage):
                continue
            body = render_template(templates[f"tpl_{stage}"],
                                   tenant, tenant["property_name"], due)
            send_sms(tenant["phone"], body, landlord_id, tenant["id"])
            mark_reminder_sent(tenant["id"], period, stage)
            sent.append({"tenant": tenant["name"], "phone": tenant["phone"],
                         "period": period, "stage": stage})
