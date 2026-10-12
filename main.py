import os
import json
import asyncio
import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse
from playwright.async_api import async_playwright

app = FastAPI()

os.makedirs("sessions", exist_ok=True)

LOGIN_URL = "https://omninashvillehotel.humanity.com/app/"
SCHEDULE_URL = "https://omninashvillehotel.humanity.com/fe/schedule"
NASHVILLE_TZ = ZoneInfo("America/Chicago")

# 🛑 Global tracking for active tasks and logs
active_tasks = {}
user_logs = {}

def add_log(user_id: str, message: str):
    if user_id not in user_logs:
        user_logs[user_id] = []
    
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    user_logs[user_id].append(log_entry)
    
    # Store up to 50 logs per user
    if len(user_logs[user_id]) > 50:
        user_logs[user_id].pop(0)
        
    print(f"[{user_id}] {log_entry}")

class HumanityBot:
    def __init__(self, user_config: dict, schedule_config: dict, max_shifts: int):
        self.user_id = user_config["user_id"]
        self.name = user_config["name"]
        self.email = user_config["email"]
        self.password = user_config["password"]
        self.employee_id = user_config["employee_id"]
        
        self.max_shifts_to_grab = max_shifts
        self.state_file = f"sessions/session_{self.user_id}.json"
        
        self.target_shifts = {}
        for day, times in schedule_config.items():
            times = times.strip()
            if times:
                try:
                    start_str, end_str = times.split('-')
                    start_h, start_m = map(int, start_str.strip().split(':'))
                    end_h, end_m = map(int, end_str.strip().split(':'))
                    start_dec = start_h + (start_m / 60.0)
                    end_dec = end_h + (end_m / 60.0)
                    self.target_shifts[day] = (start_dec, end_dec)
                except Exception as e:
                    add_log(self.user_id, f"⚠️ Error parsing time for {day}: {e}")

        self.auth_headers = {"accept": "application/json", "content-type": "application/json"}
        self.tokens_acquired = asyncio.Event()
        self.attempted_shifts = set()
        self.shifts_secured = 0

    def is_shift_allowed(self, start_dt, end_dt):
        try:
            day_name = start_dt.strftime("%A")
            if day_name not in self.target_shifts:
                return False
                
            allowed_start, allowed_end = self.target_shifts[day_name]
            start_hour = start_dt.hour + (start_dt.minute / 60.0)
            end_hour = end_dt.hour + (end_dt.minute / 60.0)
            
            return start_hour >= allowed_start and end_hour <= allowed_end
        except Exception as e:
            add_log(self.user_id, f"⚠️ Error reading shift times: {e}")
            return False

    async def capture_headers(self, request):
        if "/fe/api/" in request.url and "h-authorization" in request.headers:
            self.auth_headers["h-authorization"] = request.headers["h-authorization"]
            if "x-schedule-version" in request.headers:
                self.auth_headers["x-schedule-version"] = request.headers["x-schedule-version"]
            self.tokens_acquired.set()

    def get_scan_url(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        scan_from = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        scan_to = (now + datetime.timedelta(days=60)).strftime("%Y-%m-%dT23:59:59.000Z")
        return f"https://omninashvillehotel.humanity.com/fe/api/shifts/open?from={scan_from}&to={scan_to}&offset=0&rows=100&isApproval=false"

    async def run(self):
        add_log(self.user_id, f"🚀 Bot Engine started for {self.name} (Target: {self.max_shifts_to_grab} shifts)")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            
            try:
                if os.path.exists(self.state_file):
                    add_log(self.user_id, f"Loading cached cookies ({self.state_file})...")
                    context = await browser.new_context(storage_state=self.state_file)
                    page = await context.new_page()
                else:
                    add_log(self.user_id, f"No cached cookies. Logging in for {self.name}...")
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(LOGIN_URL)
                    await page.fill("input#email", self.email)
                    await page.fill("input#password", self.password)
                    await page.click("button#loginBtn")
                    await asyncio.sleep(5)
                    
                    try:
                        await page.wait_for_url("**/app/**", timeout=15000)
                        await context.storage_state(path=self.state_file)
                        add_log(self.user_id, "✅ Login successful! Session saved.")
                    except Exception as login_error:
                        add_log(self.user_id, f"❌ Login failed: {login_error}")
                        await browser.close()
                        return

                add_log(self.user_id, "🕵️ Capturing authorization tokens...")
                page.on("request", self.capture_headers)
                await page.goto(SCHEDULE_URL, wait_until="networkidle")
                
                try:
                    await asyncio.wait_for(self.tokens_acquired.wait(), timeout=10.0)
                    add_log(self.user_id, "🔐 Security tokens captured!")
                except asyncio.TimeoutError:
                    add_log(self.user_id, f"❌ Token capture timed out. Delete {self.state_file} & restart.")
                    await browser.close()
                    return

                add_log(self.user_id, "🚀 SCANNER ACTIVE - Scanning for open shifts...")
                
                while self.shifts_secured < self.max_shifts_to_grab:
                    scan_url = self.get_scan_url()
                    scan_response = await context.request.get(scan_url, headers=self.auth_headers)
                    
                    if scan_response.ok:
                        raw_data = await scan_response.json()
                        shifts = raw_data if isinstance(raw_data, list) else raw_data.get("data", raw_data.get("shifts", []))
                        new_shifts = [s for s in shifts if str(s.get("id")) not in self.attempted_shifts]
                        
                        if len(new_shifts) > 0:
                            add_log(self.user_id, f"🔥 {len(new_shifts)} NEW SHIFT(S) FOUND!")
                            
                            for shift in new_shifts:
                                # Stop immediately if max shifts reached mid-loop
                                if self.shifts_secured >= self.max_shifts_to_grab:
                                    break
                                    
                                shift_id = str(shift.get("id"))
                                self.attempted_shifts.add(shift_id)
                                
                                start_raw = str(shift.get("startISO") or shift.get("start_timestamp") or shift.get("start_date") or shift.get("start_time") or "")
                                end_raw = str(shift.get("endISO") or shift.get("end_timestamp") or shift.get("end_date") or shift.get("end_time") or "")
                                
                                if not start_raw or not end_raw:
                                    continue

                                try:
                                    start_utc = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                                    end_utc = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                                    start_local = start_utc.astimezone(NASHVILLE_TZ)
                                    end_local = end_utc.astimezone(NASHVILLE_TZ)
                                    start_time = start_local.strftime("%Y-%m-%d %H:%M")
                                    end_time = end_local.strftime("%Y-%m-%d %H:%M")
                                except Exception:
                                    continue
                                    
                                title = str(shift.get("title", "")).upper()
                                notes = str(shift.get("notes", "")).upper()
                                if "TRAINING" in title or "TRAINING" in notes:
                                    add_log(self.user_id, f"⏭️ Skipped Shift {shift_id} ('TRAINING' keyword).")
                                    continue 
                                    
                                if not self.is_shift_allowed(start_local, end_local):
                                    add_log(self.user_id, f"⏭️ Skipped Shift {shift_id} (Outside hours: {start_local.strftime('%A %H:%M')}).")
                                    continue
                                    
                                add_log(self.user_id, f"⚡ Lock acquired on Shift {shift_id} ({start_time} to {end_time})")
                                sniper_url = f"https://omninashvillehotel.humanity.com/fe/api/shift-requests/shift/{shift_id}?type=1"
                                payload = {
                                    "from": start_time,
                                    "to": end_time,
                                    "notes": "",
                                    "employeeId": self.employee_id,
                                    "autoApproval": True
                                }
                                
                                sniper_response = await context.request.post(sniper_url, headers=self.auth_headers, data=payload)
                                if sniper_response.ok:
                                    self.shifts_secured += 1
                                    add_log(self.user_id, f"🎉 SHIFT {shift_id} SECURED! ({start_time} to {end_time}) [{self.shifts_secured}/{self.max_shifts_to_grab}]")
                                else:
                                    add_log(self.user_id, f"❌ Request failed for shift {shift_id}. Status: {sniper_response.status}")
                            
                            await asyncio.sleep(5)
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                add_log(self.user_id, f"🛑 Bot process forcibly STOPPED via dashboard.")
            finally:
                add_log(self.user_id, f"🎉 TARGET REACHED! Secured {self.shifts_secured}/{self.max_shifts_to_grab} shifts. Shutting down.")
                await browser.close()
                active_tasks.pop(self.user_id, None)

async def run_bot_wrapper(user_config: dict, schedule_config: dict, max_shifts: int):
    user_id = user_config["user_id"]
    bot = HumanityBot(user_config, schedule_config, max_shifts)
    active_tasks[user_id] = asyncio.current_task()
    await bot.run()

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("users_db.json") as f:
        users = json.load(f)
        
    user_options = "".join([f'<option value="{uid}">{u["name"]}</option>' for uid, u in users.items()])

    running_status = ""
    if active_tasks:
        running_names = ", ".join([users[uid]["name"] for uid in active_tasks.keys() if uid in users])
        running_status = f'<div style="background: #e7f3fe; border-left: 6px solid #2196F3; padding: 10px; margin-bottom: 15px; border-radius: 4px;"><strong>⚡ Active Bot Running for:</strong> {running_names}</div>'

    logs_html = ""
    for uid, logs in user_logs.items():
        uname = users.get(uid, {}).get("name", uid)
        logs_text = "\n".join(logs[-20:]) if logs else "No logs available yet."
        logs_html += f"<strong>User: {uname}</strong>\n{logs_text}\n\n"

    if not logs_html:
        logs_html = "No logs yet. Start a bot to see live updates here."

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Shift Sniper Dashboard</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 15px; background: #f0f2f5; margin: 0; }}
            .card {{ background: white; padding: 20px; border-radius: 16px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); max-width: 450px; margin: 0 auto; }}
            h2 {{ margin-top: 0; color: #1a1a1a; text-align: center; }}
            label {{ font-weight: bold; color: #444; font-size: 14px; margin-top: 10px; display: block; }}
            select, input, button {{ width: 100%; padding: 10px; margin-top: 5px; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; font-size: 15px; }}
            .day-row {{ display: flex; align-items: center; justify-content: space-between; margin-top: 6px; }}
            .day-row label {{ width: 100px; margin: 0; }}
            .day-row input {{ flex: 1; margin: 0; }}
            .btn-start {{ background: #007bff; color: white; border: none; font-weight: bold; margin-top: 15px; font-size: 18px; cursor: pointer; padding: 12px; }}
            .btn-stop {{ background: #dc3545; color: white; border: none; font-weight: bold; margin-top: 10px; font-size: 18px; cursor: pointer; padding: 12px; }}
            .console-box {{ background: #1e1e1e; color: #00ff66; padding: 12px; border-radius: 8px; font-family: monospace; font-size: 12px; height: 180px; overflow-y: auto; white-space: pre-wrap; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>⚙️ Shift Sniper Dashboard</h2>
            {running_status}
            
            <form action="/start" method="post">
                <label>Select User:</label>
                <select name="user_id" required>
                    {user_options}
                </select>

                <label>Max Shifts to Grab:</label>
                <input type="number" name="max_shifts" value="3" min="1" max="10" required>

                <hr style="margin-top: 15px; border: 0; border-top: 1px solid #eee;">
                <label style="font-size: 16px; color: #007bff;">📅 Weekly Schedule (24h Format)</label>

                <div class="day-row"><label>Monday</label><input type="text" name="Monday" value="05:00-14:00"></div>
                <div class="day-row"><label>Tuesday</label><input type="text" name="Tuesday" value="05:00-14:00"></div>
                <div class="day-row"><label>Wednesday</label><input type="text" name="Wednesday" value=""></div>
                <div class="day-row"><label>Thursday</label><input type="text" name="Thursday" value="05:00-14:00"></div>
                <div class="day-row"><label>Friday</label><input type="text" name="Friday" value="05:00-14:00"></div>
                <div class="day-row"><label>Saturday</label><input type="text" name="Saturday" value=""></div>
                <div class="day-row"><label>Sunday</label><input type="text" name="Sunday" value=""></div>

                <button type="submit" class="btn-start">🚀 Start Sniper</button>
            </form>

            <form action="/stop" method="post">
                <label style="margin-top:20px;">Stop Active Bot:</label>
                <select name="user_id" required>
                    {user_options}
                </select>
                <button type="submit" class="btn-stop">🛑 Stop Selected Bot</button>
            </form>

            <hr style="margin-top: 20px; border: 0; border-top: 1px solid #eee;">
            <label style="font-size: 16px; color: #333;">📟 Live Console / Grabbed Details</label>
            <div class="console-box">{logs_html}</div>
            <a href="/" style="display: block; text-align: center; font-size: 13px; color: #007bff; margin-top: 8px; text-decoration: none;">🔄 Refresh Logs</a>
        </div>
    </body>
    </html>
    """

@app.post("/start")
async def start(
    background_tasks: BackgroundTasks, 
    user_id: str = Form(...), 
    max_shifts: int = Form(...),
    Monday: str = Form(""), Tuesday: str = Form(""), Wednesday: str = Form(""),
    Thursday: str = Form(""), Friday: str = Form(""), Saturday: str = Form(""), Sunday: str = Form("")
):
    with open("users_db.json") as f:
        users = json.load(f)
    
    user_config = users.get(user_id)
    schedule_config = {
        "Monday": Monday, "Tuesday": Tuesday, "Wednesday": Wednesday,
        "Thursday": Thursday, "Friday": Friday, "Saturday": Saturday, "Sunday": Sunday
    }

    user_logs[user_id] = []

    if user_id in active_tasks:
        active_tasks[user_id].cancel()

    background_tasks.add_task(run_bot_wrapper, user_config, schedule_config, max_shifts)
    
    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding: 40px; background: #f0f2f5;">
            <div style="background: white; padding: 30px; border-radius: 12px; display: inline-block;">
                <h2 style="color: #28a745;">✅ Sniper Launched!</h2>
                <p>Scanning shifts for <b>{user_config['name']}</b>. You can close this window now.</p>
                <a href="/" style="display: inline-block; margin-top: 15px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 6px;">View Dashboard & Logs</a>
            </div>
        </body>
    </html>
    """)

@app.post("/stop")
async def stop(user_id: str = Form(...)):
    with open("users_db.json") as f:
        users = json.load(f)
    
    user_name = users.get(user_id, {}).get("name", user_id)

    if user_id in active_tasks:
        active_tasks[user_id].cancel()
        active_tasks.pop(user_id, None)
        add_log(user_id, "🛑 Manual stop command executed.")
        msg = f"🛑 Bot for <b>{user_name}</b> has been stopped!"
    else:
        msg = f"ℹ️ No active bot running for <b>{user_name}</b>."

    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding: 40px; background: #f0f2f5;">
            <div style="background: white; padding: 30px; border-radius: 12px; display: inline-block;">
                <h2>{msg}</h2>
                <a href="/" style="display: inline-block; margin-top: 15px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 6px;">Back to Dashboard</a>
            </div>
        </body>
    </html>
    """)
