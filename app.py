"""RentPing v1 — automated rent reminders + late nudges for small landlords.

Run:  uvicorn app:app --reload        (then open http://localhost:8000)

Routes:
  GET  /                        landing page
  GET  /signup  POST /signup    create landlord account
  GET  /login   POST /login     log in
  POST /logout                  log out
  GET  /dashboard               collection status, add property/unit/tenant, message log
  POST /properties/add  /units/add  /tenants/add  /tenants/delete
  POST /tenants/mark-paid       (landlord marks a tenant paid by hand)
  POST /tenants/opt-out         (toggle STOP on/off)
  GET  /settings  POST /settings  edit the 4 message templates
  GET  /billing   POST /billing/subscribe  plans + demo subscribe
  GET/POST /internal/run-reminders   the daily engine (cron hits this)
  POST /webhooks/twilio/sms     inbound texts: PAID / STOP / START / HELP
  POST /webhooks/stripe         billing webhook stub
  GET  /healthz                 200 when alive
"""
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import store
from store import init_db
import reminders
from reminders import run_reminders, today, open_period, tenant_status, period_of
import billing
from sms import DEMO_MODE as SMS_DEMO_MODE

app = FastAPI(title="RentPing")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

SESSION_COOKIE = "rentping_session"

# Protect the cron endpoint in production: set INTERNAL_CRON_TOKEN and the
# cron job must pass ?token=...  (Locally it is wide open for easy testing.)
INTERNAL_CRON_TOKEN = os.getenv("INTERNAL_CRON_TOKEN", "")

init_db()  # create tables on startup if they don't exist yet


# ------------------------------------------------------------------ helpers --
def current_landlord(request: Request):
    return store.get_landlord_by_session(request.cookies.get(SESSION_COOKIE))


def require_login(request: Request):
    landlord = current_landlord(request)
    if not landlord:
        return None, RedirectResponse("/login", status_code=303)
    return landlord, None


def login_response(landlord_id: int):
    token = store.create_session(landlord_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


def twiml(message: str) -> Response:
    """Twilio expects an XML (TwiML) answer to an inbound SMS webhook."""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
           f"<Message>{message}</Message></Response>")
    return Response(content=xml, media_type="application/xml")


# ------------------------------------------------------------------ landing --
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    landlord = current_landlord(request)
    return templates.TemplateResponse(request, "landing.html", {"request": request, "landlord": landlord,
                                       "plans": billing.PLANS})


# --------------------------------------------------------------------- auth --
@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"request": request, "error": None})


@app.post("/signup")
def signup(name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if store.get_landlord_by_email(email):
        return HTMLResponse("<p>That email is already registered. <a href='/login'>Log in</a></p>",
                            status_code=400)
    landlord_id = store.create_landlord(name, email, password)
    # Demo mode: everyone starts on a 14-day Starter trial instantly.
    # Real mode: no subscription until they complete Stripe Checkout.
    if billing.DEMO_MODE:
        billing.activate_demo_subscription(landlord_id, "starter")
    return login_response(landlord_id)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    landlord = store.get_landlord_by_email(email)
    if not landlord or not store.verify_password(password, landlord["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Wrong email or password."}, status_code=401)
    return login_response(landlord["id"])


@app.post("/logout")
def logout(request: Request):
    store.delete_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard --
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, ran: str = ""):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    day = today()
    month_period = period_of(day)

    properties = []
    total_tenants = 0
    paid_this_month = 0
    total_rent = 0.0
    collected_rent = 0.0
    for prop in store.list_properties(landlord["id"]):
        units = []
        for unit in store.list_units(prop["id"]):
            tenants = []
            for t in store.list_tenants(unit["id"]):
                total_tenants += 1
                total_rent += float(t["rent_amount"] or 0)
                # "On-time rate" counts tenants paid for the calendar month.
                if t["paid_period"] == month_period:
                    paid_this_month += 1
                    collected_rent += float(t["rent_amount"] or 0)
                tenants.append({**dict(t), "status": tenant_status(t, day),
                                "period": open_period(t, day)})
            units.append({**dict(prop_unit(unit)), "tenants": tenants})
        properties.append({**dict(prop), "units": units})

    on_time_rate = round(100 * paid_this_month / total_tenants) if total_tenants else 0
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "landlord": landlord, "properties": properties,
        "messages": store.list_messages(landlord["id"], limit=30),
        "subscription": billing.subscription_summary(landlord["id"]),
        "plans": billing.PLANS,
        "total_tenants": total_tenants, "paid_this_month": paid_this_month,
        "total_rent": total_rent, "collected_rent": collected_rent,
        "on_time_rate": on_time_rate, "month": day.strftime("%B %Y"),
        "demo": SMS_DEMO_MODE, "ran": ran,
    })


def prop_unit(unit):
    return unit  # tiny helper so the dict() spread reads clearly above


@app.post("/properties/add")
def add_property(request: Request, name: str = Form(...), address: str = Form("")):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    store.create_property(landlord["id"], name, address)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/units/add")
def add_unit(request: Request, property_id: int = Form(...), label: str = Form(...)):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    prop = store.get_property(property_id, landlord["id"])
    if not prop:
        return HTMLResponse("Property not found.", status_code=404)
    store.create_unit(property_id, label)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/tenants/add")
def add_tenant(request: Request, unit_id: int = Form(...), name: str = Form(...),
               phone: str = Form(...), rent_amount: float = Form(...),
               due_day: int = Form(...)):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    if not (1 <= due_day <= 31):
        return HTMLResponse("Due day must be between 1 and 31.", status_code=400)
    if rent_amount <= 0 or not phone.strip() or not name.strip():
        return HTMLResponse("Name, phone and a positive rent amount are required.",
                            status_code=400)
    if not store.get_unit_for_landlord(unit_id, landlord["id"]):
        return HTMLResponse("Unit not found.", status_code=404)
    store.create_tenant(unit_id, name, phone, rent_amount, due_day)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/tenants/delete")
def delete_tenant(request: Request, tenant_id: int = Form(...)):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    tenant = store.get_tenant_for_landlord(tenant_id, landlord["id"])
    if not tenant:
        return HTMLResponse("Tenant not found.", status_code=404)
    store.delete_tenant(tenant_id)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/tenants/mark-paid")
def mark_paid(request: Request, tenant_id: int = Form(...)):
    """Landlord manually marks a tenant paid (e.g. cash/check received)."""
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    tenant = store.get_tenant_for_landlord(tenant_id, landlord["id"])
    if tenant:
        store.mark_tenant_paid(tenant_id, open_period(tenant, today()))
    else:
        return HTMLResponse("Tenant not found.", status_code=404)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/tenants/opt-out")
def toggle_opt_out(request: Request, tenant_id: int = Form(...)):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    tenant = store.get_tenant_for_landlord(tenant_id, landlord["id"])
    if tenant:
        store.set_tenant_opt_out(tenant_id, not tenant["opted_out"])
    else:
        return HTMLResponse("Tenant not found.", status_code=404)
    return RedirectResponse("/dashboard", status_code=303)


# ----------------------------------------------------------------- settings --
@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "landlord": landlord,
        "tpl": store.get_templates(landlord["id"]), "saved": False})


@app.post("/settings", response_class=HTMLResponse)
def save_settings(request: Request,
                  tpl_before: str = Form(...), tpl_due: str = Form(...),
                  tpl_late3: str = Form(...), tpl_late7: str = Form(...)):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    # Quick sanity check: every template must keep the {placeholders} it needs.
    for tpl in (tpl_before, tpl_due, tpl_late3, tpl_late7):
        for ph in ("{name}", "{amount}", "{property}", "{due_date}"):
            if ph not in tpl:
                return HTMLResponse(
                    f"Each template must include {ph}. <a href='/settings'>Go back</a>",
                    status_code=400)
    store.save_templates(landlord["id"], tpl_before, tpl_due, tpl_late3, tpl_late7)
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "landlord": landlord,
        "tpl": store.get_templates(landlord["id"]), "saved": True})


# ------------------------------------------------------------------ billing --
@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    checkout = request.query_params.get("checkout", "")
    if checkout == "success" and billing.stripe_configured():
        # Webhooks can lag; sync straight from Stripe so the page is accurate.
        try:
            billing.sync_subscription_from_stripe(landlord["id"])
        except Exception as e:
            print(f"[stripe] return-sync failed: {e}", flush=True)
    return templates.TemplateResponse(request, "billing.html", {
        "request": request, "landlord": landlord,
        "plans": billing.PLANS,
        "subscription": billing.subscription_summary(landlord["id"]),
        "demo": billing.DEMO_MODE,
        "checkout": checkout,
        "stripe_live": billing.stripe_configured()})


@app.post("/billing/subscribe")
def subscribe(request: Request, plan: str = Form(...), cycle: str = Form("monthly")):
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    if billing.DEMO_MODE:
        billing.activate_demo_subscription(landlord["id"], plan)
        return RedirectResponse("/dashboard", status_code=303)
    if not billing.stripe_configured():
        return HTMLResponse(
            "Online payments aren't configured yet. Please contact support.", status_code=501)
    try:
        base_url = str(request.base_url).rstrip("/")
        checkout_url = billing.create_checkout_session(landlord, plan, cycle, base_url)
    except Exception as e:
        print(f"[stripe] checkout error: {e}", flush=True)
        return HTMLResponse("Couldn't start checkout. Please try again.", status_code=502)
    return RedirectResponse(checkout_url, status_code=303)


# ------------------------------------------------------- reminder engine ----
@app.get("/internal/run-reminders")
def internal_run_reminders_get(request: Request, token: str = ""):
    """The daily cron hits this (GET -> JSON)."""
    if INTERNAL_CRON_TOKEN and token != INTERNAL_CRON_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sent = run_reminders()
    return {"date": today().isoformat(), "sent": len(sent), "details": sent}


@app.post("/internal/run-reminders")
def internal_run_reminders_post(request: Request):
    """Dashboard 'Run reminders now' button -> runs, then redirects back to
    the dashboard with a human-readable result banner (never a raw JSON page)."""
    landlord, redirect = require_login(request)
    if redirect:
        return redirect
    sent = run_reminders(landlord_id=landlord["id"])
    n = sum(1 for s in sent if "error" not in s)
    return RedirectResponse(f"/dashboard?ran={n}", status_code=303)


# ------------------------------------------------------------ twilio webhook --
@app.post("/webhooks/twilio/sms")
async def twilio_sms(request: Request):
    """Inbound texts from tenants. Twilio POSTs form fields From and Body."""
    form = await request.form()
    from_number = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()
    keyword = body.upper()

    # In demo mode there is no Twilio number to match on, so attribute the
    # text to the most recently created landlord (the person currently
    # testing). In production, match the Twilio number (To) to its owner.
    landlords = store.all_landlords()
    if not landlords:
        return twiml("Thanks!")
    landlord = landlords[-1]
    tenant = store.get_tenant_by_phone(landlord["id"], from_number)

    store.log_message(landlord["id"], tenant["id"] if tenant else None,
                      "in", body or "(empty)", "received")

    if not tenant:
        return twiml("Thanks for your message — your landlord will follow up.")

    if keyword == "PAID":
        store.mark_tenant_paid(tenant["id"], open_period(tenant, today()))
        reply = (f"Thanks {tenant['name'].split()[0]}! We've marked your rent "
                 f"as paid. 🎉")
    elif keyword == "STOP":
        store.set_tenant_opt_out(tenant["id"], True)
        reply = ("You've been unsubscribed from rent reminders and won't be "
                 "texted again. Reply START to resubscribe.")
    elif keyword == "START":
        store.set_tenant_opt_out(tenant["id"], False)
        reply = "You're resubscribed to rent reminders. Thanks!"
    elif keyword == "HELP":
        reply = ("Rent reminders from your landlord. Reply PAID when you've "
                 "paid rent, STOP to unsubscribe.")
    else:
        reply = (f"Thanks {tenant['name'].split()[0]} — we've passed your message "
                 f"to your landlord. Reply PAID once rent is sent.")

    store.log_message(landlord["id"], tenant["id"], "out", reply,
                      "sent (demo)" if SMS_DEMO_MODE else "sent")
    if not SMS_DEMO_MODE:
        # Real mode: send the reply through Twilio instead of just logging it.
        from sms import send_sms
        send_sms(from_number, reply, landlord["id"], tenant["id"])
    return twiml(reply)


# ------------------------------------------------------------ stripe webhook --
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if billing.DEMO_MODE or not billing.STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"error": "webhook not configured"}, status_code=501)
    try:
        event = billing.verify_webhook(payload, sig)
    except Exception as e:
        print(f"[stripe webhook] signature verification failed: {e}", flush=True)
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    try:
        result = billing.handle_stripe_event(event)
    except Exception as e:
        print(f"[stripe webhook] handler error: {e}", flush=True)
        return JSONResponse({"error": "handler failed"}, status_code=500)
    return result


# ------------------------------------------------------------------ health ----
@app.get("/healthz")
def healthz():
    return {"ok": True, "db": "postgres" if store.USE_PG else "sqlite"}
