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

class HumanityBot:
    def __init__(self, user_config: dict, schedule_config: dict, max_shifts: int):
        self.user_id = user_config["user_id"]
        self.name = user_config["name"]
        self.email = user_config["email"]
        self.password = user_config["password"]
        self.employee_id = user_config["employee_id"]
        
        self.max_shifts_to_grab = max_shifts
        self.state_file = f"sessions/session_{self.user_id}.json"
        
        # Parse the weekly schedule (exactly like original config.txt logic)
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
                    print(f"⚠️ Error parsing time for {day}: {e}")

        self.auth_headers = {"accept": "application/json", "content-type": "application/json"}
        self.tokens_acquired = asyncio.Event()
        self.attempted_shifts = set()
        self.shifts_secured = 0

    def is_shift_allowed(self, start_dt, end_dt):
        try:
            day_name = start_dt.strftime("%A")  # Gets 'Monday', 'Tuesday', etc.
            if day_name not in self.target_shifts:
                return False
                
            allowed_start, allowed_end = self.target_shifts[day_name]
            start_hour = start_dt.hour + (start_dt.minute / 60.0)
            end_hour = end_dt.hour + (end_dt.minute / 60.0)
            
            if start_hour >= allowed_start and end_hour <= allowed_end:
                return True
                
            return False
        except Exception as e:
            print(f"⚠️ Error reading shift times: {e}")
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
        print(f"\n🚀 Bot Engine running for: {self.name}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            
            if os.path.exists(self.state_file):
                print(f"Found existing session ({self.state_file}). Loading cookies...")
                context = await browser.new_context(storage_state=self.state_file)
                page = await context.new_page()
            else:
                print(f"No saved session for {self.name}. Logging in...")
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
                    print("✅ Login successful! Cookies cached.")
                except Exception as login_error:
                    print(f"❌ Login failed: {login_error}")
                    await browser.close()
                    return

            print("🕵️ Navigating to schedule to steal JWT tokens...")
            page.on("request", self.capture_headers)
            await page.goto(SCHEDULE_URL, wait_until="networkidle")
            
            try:
                await asyncio.wait_for(self.tokens_acquired.wait(), timeout=10.0)
                print("🔐 Security tokens successfully stolen! Arming the Sniper...")
            except asyncio.TimeoutError:
                print(f"❌ Failed to steal tokens. Delete {self.state_file} and try again.")
                await browser.close()
                return

            print("\n🚀 PURE API MODE ACTIVE - SCANNING THE MAINFRAME...")
            
            while self.shifts_secured < self.max_shifts_to_grab:
                try:
                    scan_url = self.get_scan_url()
                    scan_response = await context.request.get(scan_url, headers=self.auth_headers)
                    
                    if scan_response.ok:
                        raw_data = await scan_response.json()
                        shifts = raw_data if isinstance(raw_data, list) else raw_data.get("data", raw_data.get("shifts", []))
                        new_shifts = [s for s in shifts if str(s.get("id")) not in self.attempted_shifts]
                        
                        if len(new_shifts) > 0:
                            print(f"🔥 {len(new_shifts)} NEW SHIFT(S) DETECTED!")
                            
                            for shift in new_shifts:
                                if self.shifts_secured >= self.max_shifts_to_grab:
                                    break
                                    
                                shift_id = str(shift.get("id"))
                                self.attempted_shifts.add(shift_id)
                                
                                start_raw = str(shift.get("startISO") or shift.get("start_timestamp") or shift.get("start_date") or shift.get("start_time") or "")
                                end_raw = str(shift.get("endISO") or shift.get("end_timestamp") or shift.get("end_date") or shift.get("end_time") or "")
                                
                                if not start_raw or not end_raw:
                                    print(f"⏭️ Skipping Shift {shift_id}: Could not find timestamp.")
                                    continue

                                # 🕒 TIMEZONE TRANSLATION
                                try:
                                    start_utc = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                                    end_utc = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                                    start_local = start_utc.astimezone(NASHVILLE_TZ)
                                    end_local = end_utc.astimezone(NASHVILLE_TZ)
                                    start_time = start_local.strftime("%Y-%m-%d %H:%M")
                                    end_time = end_local.strftime("%Y-%m-%d %H:%M")
                                except Exception as e:
                                    print(f"⏭️ Skipping Shift {shift_id}: Time parsing failed: {e}")
                                    continue
                                    
                                # 🛡️ FILTER: Training
                                title = str(shift.get("title", "")).upper()
                                notes = str(shift.get("notes", "")).upper()
                                if "TRAINING" in title or "TRAINING" in notes:
                                    print(f"⏭️ Skipping Shift {shift_id}: Ignored keyword 'TRAINING'.")
                                    continue 
                                    
                                # 🛡️ FILTER: Schedule Check
                                if not self.is_shift_allowed(start_local, end_local):
                                    print(f"⏭️ Skipping Shift {shift_id}: Outside availability ({start_local.strftime('%A %H:%M')}).")
                                    continue
                                    
                                print(f"⚡ Lock acquired on Shift {shift_id} ({start_time} to {end_time} Nashville time)")
                                
                                sniper_url = f"https://omninashvillehotel.humanity.com/fe/api/shift-requests/shift/{shift_id}?type=1"
                                payload = {
                                    "from": start_time,
                                    "to": end_time,
                                    "notes": "",
                                    "employeeId": self.employee_id,
                                    "autoApproval": True
                                }
                                
                                print("🎯 FIRING THE BULLET...")
                                sniper_response = await context.request.post(sniper_url, headers=self.auth_headers, data=payload)
                                
                                if sniper_response.ok:
                                    self.shifts_secured += 1
                                    print(f"✅ SHIFT {shift_id} SECURED! ({self.shifts_secured}/{self.max_shifts_to_grab})")
                                else:
                                    print(f"❌ SHIFT FAILED. Status: {sniper_response.status}")
                            
                            await asyncio.sleep(5)
                    else:
                        print(f"Server rejected scan: {scan_response.status}")
                        if scan_response.status == 401:
                            print(f"🚨 401 Unauthorized. Deleting {self.state_file}...")
                            if os.path.exists(self.state_file):
                                os.remove(self.state_file)
                            break
                except Exception as e:
                    print(f"⚠️ FATAL LOOP ERROR: {e}")

                await asyncio.sleep(2)

            print(f"\n🎉 TARGET ACQUIRED! {self.shifts_secured} shifts successfully secured. Shutting down gracefully.")
            await browser.close()

async def run_bot_background(user_config: dict, schedule_config: dict, max_shifts: int):
    bot = HumanityBot(user_config, schedule_config, max_shifts)
    await bot.run()

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("users_db.json") as f:
        users = json.load(f)
        
    user_options = "".join([f'<option value="{uid}">{u["name"]}</option>' for uid, u in users.items()])

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
            button {{ background: #007bff; color: white; border: none; font-weight: bold; margin-top: 20px; font-size: 18px; cursor: pointer; padding: 12px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>⚙️ Shift Sniper Dashboard</h2>
            <form action="/start" method="post">
                <label>Select User:</label>
                <select name="user_id" required>
                    {user_options}
                </select>

                <label>Max Shifts to Grab:</label>
                <input type="number" name="max_shifts" value="3" min="1" max="10" required>

                <hr style="margin-top: 15px; border: 0; border-top: 1px solid #eee;">
                <label style="font-size: 16px; color: #007bff;">📅 Weekly Schedule (24h Format)</label>
                <p style="font-size: 12px; color: #666; margin: 2px 0 10px 0;">Leave blank for days you don't want to work.</p>

                <div class="day-row"><label>Monday</label><input type="text" name="Monday" value="05:00-14:00" placeholder="05:00-14:00"></div>
                <div class="day-row"><label>Tuesday</label><input type="text" name="Tuesday" value="05:00-14:00" placeholder="05:00-14:00"></div>
                <div class="day-row"><label>Wednesday</label><input type="text" name="Wednesday" value="" placeholder="Blank = Off"></div>
                <div class="day-row"><label>Thursday</label><input type="text" name="Thursday" value="05:00-14:00" placeholder="05:00-14:00"></div>
                <div class="day-row"><label>Friday</label><input type="text" name="Friday" value="05:00-14:00" placeholder="05:00-14:00"></div>
                <div class="day-row"><label>Saturday</label><input type="text" name="Saturday" value="" placeholder="Blank = Off"></div>
                <div class="day-row"><label>Sunday</label><input type="text" name="Sunday" value="" placeholder="Blank = Off"></div>

                <button type="submit">🚀 Start Sniper</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/start")
async def start(
    background_tasks: BackgroundTasks, 
    request: Request,
    user_id: str = Form(...), 
    max_shifts: int = Form(...),
    Monday: str = Form(""),
    Tuesday: str = Form(""),
    Wednesday: str = Form(""),
    Thursday: str = Form(""),
    Friday: str = Form(""),
    Saturday: str = Form(""),
    Sunday: str = Form("")
):
    with open("users_db.json") as f:
        users = json.load(f)
    
    user_config = users.get(user_id)
    if not user_config:
        return {"status": "Error", "message": "User profile not found"}

    schedule_config = {
        "Monday": Monday,
        "Tuesday": Tuesday,
        "Wednesday": Wednesday,
        "Thursday": Thursday,
        "Friday": Friday,
        "Saturday": Saturday,
        "Sunday": Sunday
    }

    background_tasks.add_task(run_bot_background, user_config, schedule_config, max_shifts)
    
    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding: 40px; background: #f0f2f5;">
            <div style="background: white; padding: 30px; border-radius: 12px; display: inline-block;">
                <h2 style="color: #28a745;">✅ Sniper Launched!</h2>
                <p>Bot is now scanning shifts for <b>{user_config['name']}</b>.</p>
                <p>Max Shifts Target: <b>{max_shifts}</b></p>
                <a href="/" style="display: inline-block; margin-top: 15px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 6px;">Back to Dashboard</a>
            </div>
        </body>
    </html>
    """)
