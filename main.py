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
NASHVILLE_TZ = ZoneInfo("America/Chicago")

class HumanityBot:
    def __init__(self, user_config: dict, target_date: str, start_time: str, end_time: str, max_shifts: int):
        self.user_id = user_config["user_id"]
        self.name = user_config["name"]
        self.email = user_config["email"]
        self.password = user_config["password"]
        self.employee_id = user_config["employee_id"]
        self.target_date = target_date  # Format: YYYY-MM-DD
        self.start_time = start_time    # Format: HH:MM (24-hr)
        self.end_time = end_time        # Format: HH:MM (24-hr)
        self.max_shifts = max_shifts
        self.state_file = f"sessions/session_{self.user_id}.json"
        
        self.auth_headers = {"accept": "application/json", "content-type": "application/json"}
        self.tokens_acquired = asyncio.Event()
        self.attempted_shifts = set()
        self.shifts_secured = 0

    async def capture_headers(self, request):
        if "/fe/api/" in request.url and "h-authorization" in request.headers:
            self.auth_headers["h-authorization"] = request.headers["h-authorization"]
            if "x-schedule-version" in request.headers:
                self.auth_headers["x-schedule-version"] = request.headers["x-schedule-version"]
            self.tokens_acquired.set()

    def is_shift_matching(self, start_raw, end_raw):
        try:
            start_utc = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_utc = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            
            start_local = start_utc.astimezone(NASHVILLE_TZ)
            end_local = end_utc.astimezone(NASHVILLE_TZ)

            # 1. Match selected Date
            shift_date_str = start_local.strftime("%Y-%m-%d")
            if shift_date_str != self.target_date:
                return False, start_local, end_local

            # 2. Match Time Range
            target_start_h, target_start_m = map(int, self.start_time.split(":"))
            target_end_h, target_end_m = map(int, self.end_time.split(":"))

            allowed_start_dec = target_start_h + (target_start_m / 60.0)
            allowed_end_dec = target_end_h + (target_end_m / 60.0)

            shift_start_dec = start_local.hour + (start_local.minute / 60.0)
            shift_end_dec = end_local.hour + (end_local.minute / 60.0)

            if shift_start_dec >= allowed_start_dec and shift_end_dec <= allowed_end_dec:
                return True, start_local, end_local
                
            return False, start_local, end_local
        except Exception as e:
            print(f"Error checking shift timing: {e}")
            return False, None, None

    async def run(self):
        print(f"🚀 Starting Sniper for {self.name} (Target Date: {self.target_date}, Time: {self.start_time}-{self.end_time})")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            
            if os.path.exists(self.state_file):
                context = await browser.new_context(storage_state=self.state_file)
            else:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto("https://omninashvillehotel.humanity.com/app/")
                await page.fill("input#email", self.email)
                await page.fill("input#password", self.password)
                await page.click("button#loginBtn")
                await page.wait_for_url("**/app/**", timeout=20000)
                await context.storage_state(path=self.state_file)

            page = await context.new_page()
            page.on("request", self.capture_headers)
            await page.goto("https://omninashvillehotel.humanity.com/fe/schedule", wait_until="networkidle")

            try:
                await asyncio.wait_for(self.tokens_acquired.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                print(f"❌ Could not capture authorization tokens for {self.name}")
                await browser.close()
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            scan_from = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            scan_to = (now + datetime.timedelta(days=60)).strftime("%Y-%m-%dT23:59:59.000Z")
            scan_url = f"https://omninashvillehotel.humanity.com/fe/api/shifts/open?from={scan_from}&to={scan_to}&offset=0&rows=100&isApproval=false"

            for _ in range(12):  # Polls every 5s for ~1 minute
                if self.shifts_secured >= self.max_shifts:
                    break
                    
                scan_res = await context.request.get(scan_url, headers=self.auth_headers)
                if scan_res.ok:
                    raw_data = await scan_res.json()
                    shifts = raw_data if isinstance(raw_data, list) else raw_data.get("data", [])
                    
                    for shift in shifts:
                        shift_id = str(shift.get("id"))
                        if shift_id in self.attempted_shifts:
                            continue
                            
                        title = str(shift.get("title", "")).upper()
                        notes = str(shift.get("notes", "")).upper()
                        if "TRAINING" in title or "TRAINING" in notes:
                            continue

                        start_raw = str(shift.get("startISO") or "")
                        end_raw = str(shift.get("endISO") or "")
                        
                        is_allowed, start_local, end_local = self.is_shift_matching(start_raw, end_raw)
                        
                        if not is_allowed:
                            continue

                        self.attempted_shifts.add(shift_id)
                        
                        start_formatted = start_local.strftime("%Y-%m-%d %H:%M")
                        end_formatted = end_local.strftime("%Y-%m-%d %H:%M")
                        
                        sniper_url = f"https://omninashvillehotel.humanity.com/fe/api/shift-requests/shift/{shift_id}?type=1"
                        payload = {
                            "from": start_formatted,
                            "to": end_formatted,
                            "notes": "",
                            "employeeId": self.employee_id,
                            "autoApproval": True
                        }
                        
                        res = await context.request.post(sniper_url, headers=self.auth_headers, data=payload)
                        if res.ok:
                            self.shifts_secured += 1
                            print(f"✅ Shift {shift_id} secured for {self.name}!")
                            
                await asyncio.sleep(5)

            await browser.close()

async def run_bot_background(user_config: dict, target_date: str, start_time: str, end_time: str, max_shifts: int):
    bot = HumanityBot(user_config, target_date, start_time, end_time, max_shifts)
    await bot.run()

@app.get("/", response_class=HTMLResponse)
async def home():
    # Dynamic options loaded from users_db.json
    with open("users_db.json") as f:
        users = json.load(f)
        
    user_options = "".join([f'<option value="{uid}">{u["name"]}</option>' for uid, u in users.items()])
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Shift Sniper</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; background: #f0f2f5; margin: 0; }}
            .card {{ background: white; padding: 24px; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); max-width: 420px; margin: 20px auto; }}
            h2 {{ margin-top: 0; color: #1a1a1a; text-align: center; font-size: 22px; }}
            label {{ font-weight: 600; color: #444; font-size: 14px; margin-top: 12px; display: block; }}
            select, input, button {{ width: 100%; padding: 12px; margin-top: 6px; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; font-size: 16px; }}
            .time-group {{ display: flex; gap: 10px; }}
            .time-group div {{ flex: 1; }}
            button {{ background: #007bff; color: white; border: none; font-weight: bold; margin-top: 20px; font-size: 18px; cursor: pointer; padding: 14px; }}
            button:active {{ background: #0056b3; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>🎯 Shift Sniper</h2>
            <form action="/start" method="post">
                <label for="user_id">Select Name:</label>
                <select name="user_id" id="user_id" required>
                    {user_options}
                </select>

                <label for="target_date">Target Shift Date:</label>
                <input type="date" id="target_date" name="target_date" value="{today_str}" required>

                <div class="time-group">
                    <div>
                        <label for="start_time">Available From:</label>
                        <input type="time" id="start_time" name="start_time" value="05:00" required>
                    </div>
                    <div>
                        <label for="end_time">Available To:</label>
                        <input type="time" id="end_time" name="end_time" value="14:00" required>
                    </div>
                </div>

                <label for="max_shifts">Max Shifts to Grab:</label>
                <input type="number" id="max_shifts" name="max_shifts" value="1" min="1" max="5">

                <button type="submit">🚀 Start Sniper</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/start")
async def start(
    background_tasks: BackgroundTasks, 
    user_id: str = Form(...), 
    target_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    max_shifts: int = Form(...)
):
    with open("users_db.json") as f:
        users = json.load(f)
    
    user_config = users.get(user_id)
    if not user_config:
        return {"status": "Error", "message": "User profile not found"}

    background_tasks.add_task(run_bot_background, user_config, target_date, start_time, end_time, max_shifts)
    
    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding: 40px; background: #f0f2f5;">
            <div style="background: white; padding: 30px; border-radius: 12px; display: inline-block;">
                <h2 style="color: #28a745;">✅ Sniper Launched!</h2>
                <p>Sniper is running for <b>{user_config['name']}</b>.</p>
                <p><b>Date:</b> {target_date}<br><b>Time:</b> {start_time} - {end_time}</p>
                <a href="/" style="display: inline-block; margin-top: 15px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 6px;">Back to Dashboard</a>
            </div>
        </body>
    </html>
    """)