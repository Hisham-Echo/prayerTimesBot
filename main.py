import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiohttp
import datetime
import sqlite3
import os
import math
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from dotenv import load_dotenv

# ==========================================
# 1. LOGGING & SECURITY
# ==========================================
# Configure logging to print beautifully to the console with timestamps
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("PrayerBot")

load_dotenv()
BOT_TOKEN = os.getenv('DISCORD_TOKEN')

# ==========================================
# 2. DATABASE SETUP & AUTO-MIGRATION
# ==========================================
def init_db():
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS guilds
                 (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, city TEXT, country TEXT, timezone TEXT)''')
    
    c.execute("PRAGMA table_info(guilds)")
    columns = [row[1] for row in c.fetchall()]
    if 'role_id' not in columns:
        c.execute("ALTER TABLE guilds ADD COLUMN role_id INTEGER")
    if 'method' not in columns:
        c.execute("ALTER TABLE guilds ADD COLUMN method INTEGER DEFAULT 5")
    conn.commit()
    conn.close()

def set_channel_db(guild_id, channel_id):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("INSERT INTO guilds (guild_id, channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (guild_id, channel_id))
    conn.commit(); conn.close()

def set_location_db(guild_id, city, country, timezone):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("INSERT INTO guilds (guild_id, city, country, timezone) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET city=excluded.city, country=excluded.country, timezone=excluded.timezone", (guild_id, city, country, timezone))
    conn.commit(); conn.close()

def set_role_db(guild_id, role_id):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("UPDATE guilds SET role_id = ? WHERE guild_id = ?", (role_id, guild_id))
    conn.commit(); conn.close()

def set_method_db(guild_id, method_id):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("UPDATE guilds SET method = ? WHERE guild_id = ?", (method_id, guild_id))
    conn.commit(); conn.close()

def get_all_guilds():
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("SELECT guild_id, channel_id, city, country, timezone, role_id, method FROM guilds WHERE channel_id IS NOT NULL AND city IS NOT NULL")
    rows = c.fetchall(); conn.close()
    return rows

def get_guild_config(guild_id):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("SELECT channel_id, city, country, timezone, role_id, method FROM guilds WHERE guild_id = ?", (guild_id,))
    row = c.fetchone(); conn.close()
    return row

def remove_guild_db(guild_id):
    conn = sqlite3.connect('prayer_config.db')
    c = conn.cursor()
    c.execute("DELETE FROM guilds WHERE guild_id = ?", (guild_id,))
    conn.commit(); conn.close()

init_db()

# ==========================================
# 3. BOT SETUP & CACHES
# ==========================================
intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

guild_caches = {}  
api_cache = {}     
prayer_order = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

METHODS = {
    "Egyptian General Authority": 5,
    "Umm Al-Qura University, Makkah": 4,
    "Islamic Society of North America (ISNA)": 2,
    "Muslim World League (MWL)": 3,
    "University of Islamic Sciences, Karachi": 1,
    "King Abdulaziz City, Gulf Region": 8,
    "Moonsighting Committee Worldwide": 15
}

ALL_TIMEZONES = sorted(list(available_timezones()))

# ==========================================
# 4. AUTOCOMPLETE FUNCTIONS
# ==========================================
async def timezone_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [tz for tz in ALL_TIMEZONES if current in tz.lower()]
    matches.sort(key=lambda x: not x.lower().startswith(current))
    return [app_commands.Choice(name=tz, value=tz) for tz in matches[:25]]

async def method_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    current = current.lower()
    matches = [m for m in METHODS.keys() if current in m.lower()]
    return [app_commands.Choice(name=m, value=METHODS[m]) for m in matches[:25]]

# ==========================================
# 5. HELPER FUNCTIONS & SMART CACHE
# ==========================================
def parse_time(time_str):
    return datetime.datetime.strptime(time_str.split(' ')[0], "%H:%M").time()

def format_time_12h(time_obj):
    return time_obj.strftime("%I:%M %p")

def calculate_qibla(lat, lng):
    mecca_lat, mecca_lng = 21.4225, 39.8262
    lat_r, lng_r = math.radians(lat), math.radians(lng)
    mecca_lat_r, mecca_lng_r = math.radians(mecca_lat), math.radians(mecca_lng)
    term1 = math.sin(mecca_lng_r - lng_r)
    term2 = math.cos(lat_r) * math.tan(mecca_lat_r)
    term3 = math.sin(lat_r) * math.cos(mecca_lng_r - lng_r)
    qibla = math.degrees(math.atan2(term1, term2 - term3))
    return (qibla + 360) % 360

def generate_progress_bar(start_time, end_time, current_time):
    start_mins = start_time.hour * 60 + start_time.minute
    end_mins = end_time.hour * 60 + end_time.minute
    curr_mins = current_time.hour * 60 + current_time.minute
    if end_mins <= start_mins: return "[----------] 0%"
    progress = max(0.0, min(1.0, (curr_mins - start_mins) / (end_mins - start_mins)))
    filled = int(progress * 10)
    return f"[{'█' * filled}{'░' * (10 - filled)}] {int(progress * 100)}%"

async def get_prayer_times(city, country, method):
    cache_key = f"{city.strip().lower()}|{country.strip().lower()}|{method}"
    today_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    
    if cache_key in api_cache and api_cache[cache_key]["date"] == today_utc:
        return api_cache[cache_key]["data"]
        
    url = "http://api.aladhan.com/v1/timingsByCity"
    params = {"city": city, "country": country, "method": method}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if data['code'] == 200:
                    timings = data['data']['timings']
                    hijri = data['data']['date']['hijri']
                    hijri_str = f"{hijri['day']} {hijri['month']['en']} {hijri['year']}"
                    meta = data['data']['meta']
                    lat = meta.get('latitude')
                    lng = meta.get('longitude')
                    
                    cached_data = {"timings": timings, "hijri": hijri_str, "lat": lat, "lng": lng}
                    api_cache[cache_key] = {"date": today_utc, "data": cached_data}
                    return cached_data
    except Exception as e:
        logger.error(f"Failed to fetch prayer times for {city}, {country}: {e}")
    return None

async def ensure_schedule(guild_id, city, country, method, tz_str):
    try:
        tz = ZoneInfo(tz_str)
    except ZoneInfoNotFoundError:
        return None
        
    now = datetime.datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")
    
    if guild_id not in guild_caches:
        guild_caches[guild_id] = {"schedule": {}, "announced": set(), "last_date": "", "hijri": ""}
        
    cache = guild_caches[guild_id]
    
    if today_str != cache["last_date"] or not cache["schedule"]:
        data = await get_prayer_times(city, country, method)
        if data:
            cache["schedule"] = {p: parse_time(data["timings"][p]) for p in prayer_order}
            cache["hijri"] = data.get("hijri", "")
            cache["announced"] = set()
            cache["last_date"] = today_str
            
    return cache

def get_embed_style(prayer_name):
    styles = {
        "Fajr":    {"color": 0x3498DB, "emoji": "🌅"}, "Dhuhr":   {"color": 0xF1C40F, "emoji": "☀️"}, 
        "Asr":     {"color": 0xE67E22, "emoji": "🌤️"}, "Maghrib": {"color": 0xE74C3C, "emoji": "🌇"}, 
        "Isha":    {"color": 0x8E44AD, "emoji": "🌙"}, "Default": {"color": 0x2ECC71, "emoji": "🕌"}
    }
    return styles.get(prayer_name, styles["Default"])

# ==========================================
# 6. BACKGROUND AUTO-ANNOUNCER
# ==========================================
@tasks.loop(minutes=1)
async def check_prayer_times():
    configured_guilds = get_all_guilds()
    for guild_id, channel_id, city, country, tz_str, role_id, method in configured_guilds:
        cache = await ensure_schedule(guild_id, city, country, method, tz_str)
        if not cache or not cache["schedule"]:
            continue
            
        tz = ZoneInfo(tz_str)
        now = datetime.datetime.now(tz)
        current_time = now.time().replace(second=0, microsecond=0)
        channel = bot.get_channel(channel_id)
        if not channel: continue
            
        for prayer in prayer_order:
            p_time = cache["schedule"].get(prayer)
            if p_time == current_time and prayer not in cache["announced"]:
                cache["announced"].add(prayer)
                await send_auto_announcement(channel, prayer, now, city, country, cache["hijri"], role_id)

# NEW: Catches and logs any crash in the background task
@check_prayer_times.error
async def check_prayer_times_error(error):
    logger.error(f"CRITICAL: Background task 'check_prayer_times' crashed!", exc_info=error)

async def send_auto_announcement(channel, prayer_name, current_time, city, country, hijri_date, role_id):
    try:
        style = get_embed_style(prayer_name)
        embed = discord.Embed(
            title=f"{style['emoji']}  {prayer_name} Prayer Time",
            description=f"It is time for **{prayer_name}** prayer.\n\n"
                        f"🕋 *Hayya 'alas-salah* (Come to prayer)\n"
                        f"🕋 *Hayya 'alal-falah* (Come to success)*",
            color=style["color"], timestamp=current_time
        )
        footer_text = f"{city}, {country} | 📅 {hijri_date} | {current_time.strftime('%A, %B %d, %Y')}"
        embed.set_footer(text=footer_text)
        
        content = f"<@&{role_id}>" if role_id else None
        await channel.send(content=content, embed=embed)
    except discord.Forbidden:
        logger.warning(f"Missing permissions in channel {channel.id} (Server: {channel.guild.name}). Removing config.")
        remove_guild_db(channel.guild.id)
    except Exception as e:
        logger.error(f"Unexpected error sending announcement to {channel.id}: {e}", exc_info=e)

# ==========================================
# 7. SLASH COMMANDS
# ==========================================

@bot.tree.command(name="setup", description="Set the channel for automatic prayer announcements (Admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(interaction: discord.Interaction, channel: discord.TextChannel):
    set_channel_db(interaction.guild_id, channel.id)
    embed = discord.Embed(title="✅ Channel Configured", description=f"Announcements will be sent to {channel.mention}.", color=0x2ECC71)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setlocation", description="Set your city, country, and time zone (Admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlocation_command(interaction: discord.Interaction, city: str, country: str, timezone: str):
    try: ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return await interaction.response.send_message("❌ Invalid Time Zone. Please use the autocomplete suggestions.", ephemeral=True)
    
    set_location_db(interaction.guild_id, city, country, timezone)
    if interaction.guild_id in guild_caches: del guild_caches[interaction.guild_id]
    
    embed = discord.Embed(title="✅ Location Configured", description=f"🏙️ **City:** `{city}`\n🌍 **Country:** `{country}`\n⏳ **Time Zone:** `{timezone}`", color=0x2ECC71)
    await interaction.response.send_message(embed=embed)

@setlocation_command.autocomplete('timezone')
async def _tz_auto(interaction: discord.Interaction, current: str): 
    return await timezone_autocomplete(interaction, current)

@bot.tree.command(name="setrole", description="Set a role to ping when prayer times arrive (Admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setrole_command(interaction: discord.Interaction, role: discord.Role):
    set_role_db(interaction.guild_id, role.id)
    embed = discord.Embed(title="🔔 Role Ping Configured", description=f"The bot will now ping {role.mention} when a prayer time arrives.", color=0x9B59B6)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setmethod", description="Choose the Islamic calculation method (Admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setmethod_command(interaction: discord.Interaction, method: int):
    set_method_db(interaction.guild_id, method)
    if interaction.guild_id in guild_caches: del guild_caches[interaction.guild_id]
    
    method_name = next((k for k, v in METHODS.items() if v == method), "Unknown")
    embed = discord.Embed(title="🧮 Calculation Method Set", description=f"Prayer times will now be calculated using the **{method_name}** method.", color=0xE67E22)
    await interaction.response.send_message(embed=embed)

@setmethod_command.autocomplete('method')
async def _method_auto(interaction: discord.Interaction, current: str): 
    return await method_autocomplete(interaction, current)

@bot.tree.command(name="qibla", description="Find the direction of the Qibla from your configured city")
async def qibla_command(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild_id)
    if not config or not config[2]:
        return await interaction.response.send_message("❌ Please use `/setlocation` first.", ephemeral=True)

    _, city, country, tz_str, _, method = config
    data = await get_prayer_times(city, country, method)
    
    if not data or not data.get("lat") or not data.get("lng"):
        return await interaction.response.send_message("❌ Could not calculate Qibla. Coordinates not found for this city.", ephemeral=True)

    lat, lng = data["lat"], data["lng"]
    qibla_angle = calculate_qibla(lat, lng)
    
    if 337.5 <= qibla_angle or qibla_angle < 22.5: compass = "🧭 ⬆️ (North)"
    elif 22.5 <= qibla_angle < 67.5: compass = "🧭 ↗️ (North-East)"
    elif 67.5 <= qibla_angle < 112.5: compass = "🧭 ➡️ (East)"
    elif 112.5 <= qibla_angle < 157.5: compass = "🧭 ↘️ (South-East)"
    elif 157.5 <= qibla_angle < 202.5: compass = "🧭 ⬇️ (South)"
    elif 202.5 <= qibla_angle < 247.5: compass = "🧭 ↙️ (South-West)"
    elif 247.5 <= qibla_angle < 292.5: compass = "🧭 ⬅️ (West)"
    else: compass = "🧭 ↖️ (North-West)"

    embed = discord.Embed(
        title="🕋 Qibla Direction",
        description=f"The direction to the Holy Kaaba from **{city}, {country}** is:\n\n"
                    f"## 🧭 {qibla_angle:.2f}°\n"
                    f"**{compass}**\n\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                    f"*Face this direction when you pray.*",
        color=0x1ABC9C
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="test", description="Send a test message to verify the bot is working")
async def test_command(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild_id)
    location_text = "Not configured. Use `/setlocation`."
    if config and config[1]: location_text = f"{config[1]}, {config[2]} | {config[3]}"

    embed = discord.Embed(
        title="✅ Bot Test Successful",
        description="Assalamu Alaikum! The bot is online and functioning perfectly.\n\n"
                    "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                    f"🕌 **Status:** `Online`\n"
                    f"📍 **Location:** `{location_text}`",
        color=0x2ECC71
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="prayers", description="Show the full prayer schedule for today")
async def prayers_command(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild_id)
    if not config or not config[3]:
        return await interaction.response.send_message("❌ Please use `/setlocation` first.", ephemeral=True)

    channel_id, city, country, tz_str, _, method = config
    tz = ZoneInfo(tz_str)
    now = datetime.datetime.now(tz)
    
    cache = await ensure_schedule(interaction.guild_id, city, country, method, tz_str)
    if not cache or not cache["schedule"]:
        return await interaction.response.send_message("❌ Failed to fetch schedule. Please try again later.", ephemeral=True)

    description = f"🕌 **Daily Prayer Schedule**\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    for prayer in prayer_order:
        style = get_embed_style(prayer)
        time_str = format_time_12h(cache["schedule"][prayer])
        status = "`✅ Passed`" if cache["schedule"][prayer] <= now.time() else "`⏳ Upcoming`"
        description += f"## {style['emoji']} **{prayer}:** `{time_str}` {status}\n"
    description += "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

    embed = discord.Embed(title="📅 Today's Prayer Times", description=description, color=0x1ABC9C, timestamp=now)
    embed.set_footer(text=f"{city}, {country} | 📅 {cache.get('hijri', '')} | {now.strftime('%A, %B %d, %Y')}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="nextp", description="Show details about the next upcoming prayer")
async def nextp_command(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild_id)
    if not config or not config[3]:
        return await interaction.response.send_message("❌ Please use `/setlocation` first.", ephemeral=True)

    channel_id, city, country, tz_str, _, method = config
    tz = ZoneInfo(tz_str)
    now = datetime.datetime.now(tz)
    current_time = now.time()
    
    cache = await ensure_schedule(interaction.guild_id, city, country, method, tz_str)
    if not cache or not cache["schedule"]:
        return await interaction.response.send_message("❌ Failed to fetch schedule. Please try again later.", ephemeral=True)

    next_prayer = next((p for p in prayer_order if cache["schedule"][p] > current_time), None)
    prev_prayer = next((p for p in reversed(prayer_order) if cache["schedule"][p] <= current_time), None)

    if not next_prayer:
        embed = discord.Embed(title="🌙 All Prayers Completed", description="All prayers for today have passed. The next prayer is **Fajr** tomorrow.\n\nMay Allah accept your prayers! 🤲", color=0x8E44AD)
        return await interaction.response.send_message(embed=embed)

    style = get_embed_style(next_prayer)
    time_diff = datetime.datetime.combine(now.date(), cache["schedule"][next_prayer]) - datetime.datetime.combine(now.date(), current_time)
    hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
    minutes = remainder // 60

    progress_bar = ""
    if prev_prayer:
        progress_bar = f"\n## 📊 **Progress:** {generate_progress_bar(cache['schedule'][prev_prayer], cache['schedule'][next_prayer], current_time)}"

    embed = discord.Embed(
        title=f"{style['emoji']} Next Prayer: {next_prayer}",
        description=f"The next prayer is **{next_prayer}**.\n\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                    f"## ⏰ **Time:** `{format_time_12h(cache['schedule'][next_prayer])}`\n"
                    f"## ⏳ **Time Remaining:** `{hours}h {minutes}m`{progress_bar}\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬",
        color=style["color"]
    )
    embed.set_footer(text=f"{city}, {country} | 📅 {cache.get('hijri', '')}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="prevp", description="Show details about the previous prayer")
async def prevp_command(interaction: discord.Interaction):
    config = get_guild_config(interaction.guild_id)
    if not config or not config[3]:
        return await interaction.response.send_message("❌ Please use `/setlocation` first.", ephemeral=True)

    channel_id, city, country, tz_str, _, method = config
    tz = ZoneInfo(tz_str)
    now = datetime.datetime.now(tz)
    current_time = now.time()
    
    cache = await ensure_schedule(interaction.guild_id, city, country, method, tz_str)
    if not cache or not cache["schedule"]:
        return await interaction.response.send_message("❌ Failed to fetch schedule. Please try again later.", ephemeral=True)

    prev_prayer = next((p for p in reversed(prayer_order) if cache["schedule"][p] <= current_time), None)

    if not prev_prayer:
        embed = discord.Embed(title="🌅 Before Fajr", description="Fajr has not started yet today. The night is still young.\n\n*Use the time for Tahajjud and Dhikr.* 📿", color=0x3498DB)
        return await interaction.response.send_message(embed=embed)

    style = get_embed_style(prev_prayer)
    time_diff = datetime.datetime.combine(now.date(), current_time) - datetime.datetime.combine(now.date(), cache["schedule"][prev_prayer])
    hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
    minutes = remainder // 60

    embed = discord.Embed(
        title=f"{style['emoji']} Previous Prayer: {prev_prayer}",
        description=f"The last prayer was **{prev_prayer}**.\n\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                    f"## ⏰ **Time:** `{format_time_12h(cache['schedule'][prev_prayer])}`\n"
                    f"## ⏳ **Time Elapsed:** `{hours}h {minutes}m` ago\n"
                    f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n\n"
                    f"*Don't forget your post-prayer Adhkar (remembrances).* 🤲",
        color=style["color"]
    )
    embed.set_footer(text=f"{city}, {country} | 📅 {cache.get('hijri', '')}")
    await interaction.response.send_message(embed=embed)

# ==========================================
# 8. GLOBAL ERROR HANDLERS (CATCHES EVERYTHING)
# ==========================================

# Catches any error in ANY slash command and prints it to the console
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Slash Command Error in '/{interaction.command.name}': {error}", exc_info=error)
    
    # Prevent the "The application did not respond" ghost message for the user
    if not interaction.response.is_done():
        await interaction.response.send_message("❌ An unexpected error occurred. Please check the console.", ephemeral=True)

# Catches any error in standard bot events (like on_ready, on_message, etc.)
@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"Unhandled Event Error in '{event}':", exc_info=True)

# ==========================================
# 9. BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user.name}")
    logger.info(f"🌍 Connected to {len(bot.guilds)} servers.")
    try:
        synced = await bot.tree.sync()
        logger.info(f"🔄 Synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}", exc_info=e)
        
    if not check_prayer_times.is_running():
        check_prayer_times.start()

@bot.event
async def on_guild_remove(guild):
    remove_guild_db(guild.id)
    if guild.id in guild_caches: del guild_caches[guild.id]
    logger.info(f"🗑️ Removed configuration for server: {guild.name}")

# ==========================================
# 10. RUN BOT
# ==========================================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)