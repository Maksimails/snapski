"""
SNAPski Telegram Bot
====================
Handles: game launch, referrals, challenge links, Stars payments,
         score tracking, leaderboard, and challenge notifications.

Requirements:
    pip install python-telegram-bot==20.7 supabase python-dotenv

Environment variables (.env):
    BOT_TOKEN        — from @BotFather
    SUPABASE_URL     — from supabase.com project settings
    SUPABASE_KEY     — service_role key (not anon)
    GAME_URL         — your GitHub Pages URL e.g. https://you.github.io/snapski
    WEBHOOK_URL      — your Railway/Render public URL e.g. https://snapski.up.railway.app
"""

import os
import json
import logging
import hashlib
import random
from datetime import datetime, timezone
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, WebAppInfo, MenuButtonWebApp,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler,
    ContextTypes, filters, ApplicationBuilder,
)
from supabase import create_client, Client

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
load_dotenv()

BOT_TOKEN   = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GAME_URL    = os.environ.get("GAME_URL", "https://your-username.github.io/snapski")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Stars pricing (1 Star = ~$0.013)
PRICE_CONTINUE     = 1    # Stars to continue after death
PRICE_CHAMPIONSHIP = 5    # Stars to enter weekly championship
PRIZE_POOL_CUT     = 0.80 # 80% of entry goes to prize pool
REFERRAL_CUT_L1    = 0.20 # 20% of referred player's spending → referrer
REFERRAL_CUT_L2    = 0.05 # 5%  of level-2 referral spending → original referrer

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("snapski")

# ─────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def upsert_player(user_id: int, username: str, first_name: str, referred_by: int = None):
    """Create player row if not exists, update username if changed."""
    existing = db.table("players").select("*").eq("user_id", user_id).execute()
    if existing.data:
        db.table("players").update({"username": username, "first_name": first_name})\
          .eq("user_id", user_id).execute()
        return existing.data[0]
    payload = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "referred_by": referred_by,
        "stars_earned": 0,
        "best_score": 0,
        "best_pct": 0,
        "total_games": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = db.table("players").insert(payload).execute()
    return result.data[0]

def get_player(user_id: int):
    r = db.table("players").select("*").eq("user_id", user_id).execute()
    return r.data[0] if r.data else None

def save_score(user_id: int, score: int, pct: float, title: str, mode: str, rounds: int, chain: int):
    """Save a completed game score and update best if beaten."""
    db.table("scores").insert({
        "user_id": user_id,
        "score": score,
        "pct": pct,
        "title": title,
        "mode": mode,
        "rounds": rounds,
        "best_chain": chain,
        "played_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    # Update player best
    player = get_player(user_id)
    if player and score > player["best_score"]:
        db.table("players").update({
            "best_score": score,
            "best_pct": pct,
            "best_title": title,
        }).eq("user_id", user_id).execute()
        return True  # New best
    return False

def get_weekly_leaderboard():
    """Top 10 scores this week, championship mode only."""
    # Get start of current week (Monday)
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_start.replace(day=now.day - days_since_monday)

    r = db.table("scores")\
        .select("user_id, score, title, players(username, first_name)")\
        .eq("mode", "championship")\
        .gte("played_at", week_start.isoformat())\
        .order("score", desc=True)\
        .limit(10)\
        .execute()
    return r.data or []

def get_champ_pool():
    """Current championship prize pool in Stars."""
    r = db.table("config").select("value").eq("key", "champ_pool").execute()
    return int(r.data[0]["value"]) if r.data else 340

def add_to_pool(stars: int):
    """Add Stars to prize pool (80% of entry fee)."""
    pool = get_champ_pool()
    new_pool = pool + int(stars * PRIZE_POOL_CUT)
    db.table("config").upsert({"key": "champ_pool", "value": str(new_pool)}).execute()
    return new_pool

def get_challenges_beaten(user_id: int, score: int):
    """Find all players who this user just beat (they set a challenge, user now surpasses it)."""
    r = db.table("challenges")\
        .select("challenger_id, challenge_score, notified")\
        .lt("challenge_score", score)\
        .eq("notified", False)\
        .neq("challenger_id", user_id)\
        .execute()
    return r.data or []

def mark_challenges_notified(challenger_ids: list[int]):
    for cid in challenger_ids:
        db.table("challenges").update({"notified": True})\
          .eq("challenger_id", cid).execute()

def upsert_challenge(user_id: int, score: int):
    """Store/update a player's challenge score."""
    db.table("challenges").upsert({
        "challenger_id": user_id,
        "challenge_score": score,
        "notified": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

def credit_referrals(user_id: int, stars_spent: int):
    """Pay L1 referrer 20%, L2 referrer 5%."""
    player = get_player(user_id)
    if not player or not player.get("referred_by"):
        return

    l1_id = player["referred_by"]
    l1_earn = int(stars_spent * REFERRAL_CUT_L1)
    if l1_earn > 0:
        db.table("players").update({
            "stars_earned": db.raw(f"stars_earned + {l1_earn}")
        }).eq("user_id", l1_id).execute()
        db.table("referral_earnings").insert({
            "referrer_id": l1_id,
            "from_user_id": user_id,
            "stars": l1_earn,
            "level": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    # Level 2
    l1 = get_player(l1_id)
    if l1 and l1.get("referred_by"):
        l2_id = l1["referred_by"]
        l2_earn = int(stars_spent * REFERRAL_CUT_L2)
        if l2_earn > 0:
            db.table("players").update({
                "stars_earned": db.raw(f"stars_earned + {l2_earn}")
            }).eq("user_id", l2_id).execute()
            db.table("referral_earnings").insert({
                "referrer_id": l2_id,
                "from_user_id": user_id,
                "stars": l2_earn,
                "level": 2,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────
def game_url_for(user_id: int, mode: str = "daily", challenge_score: int = None, challenger_name: str = None) -> str:
    """Build the game URL with all params the frontend needs."""
    url = f"{GAME_URL}?uid={user_id}&mode={mode}"
    if challenge_score:
        url += f"&challengeScore={challenge_score}"
    if challenger_name:
        url += f"&challengerName={challenger_name}"
    return url

def rank_emoji(pos: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(pos, f"{pos}.")

def title_to_emoji(title: str) -> str:
    return {
        "⚡LIGHTNING": "⚡",
        "BLADE": "🔪",
        "RAZOR": "💎",
        "SHARP": "🔹",
        "SNAPPY": "✅",
        "BLUNT": "💤",
    }.get(title, "")

def daily_seed() -> int:
    """Same seed for everyone on the same calendar day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return int(hashlib.md5(today.encode()).hexdigest(), 16) % 100000

NOTIFICATION_MESSAGES = [
    "⚡ {name} just WHIPPED your SNAPski score!\n{score} pts · {pct}th percentile\n\n👉 CHALLENGE THEM BACK",
    "😤 {name} thinks they're faster than you.\n{score} pts · {pct}th percentile\n\nProve them wrong →",
    "🔥 {name} beat your score!\n{score} pts vs your best\n\nYou gonna let that stand?",
    "👀 {name} clocked {pct}th percentile.\nThat used to be YOUR territory.\n\nReclaim your spot →",
    "💀 {name} just ended your reign.\n{score} pts · {pct}th percentile\n\nGet back in the game →",
]

def challenge_notification_text(name: str, score: int, pct: float) -> str:
    template = random.choice(NOTIFICATION_MESSAGES)
    return template.format(name=name, score=score, pct=pct)

# ─────────────────────────────────────────
#  /start — entry point for everything
# ─────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args  # list of strings after /start

    referred_by = None
    challenge_score = None
    challenger_name = None
    challenger_id = None
    mode = "daily"

    # Parse deep link params
    if args:
        param = args[0]

        # Referral link: ref_{user_id}
        if param.startswith("ref_"):
            ref_id = int(param[4:])
            if ref_id != user.id:
                referred_by = ref_id
                log.info(f"Referral: {user.id} referred by {ref_id}")

        # Challenge link: challenge_{challenger_id}_{score}
        elif param.startswith("challenge_"):
            parts = param.split("_")
            if len(parts) == 3:
                challenger_id = int(parts[1])
                challenge_score = int(parts[2])
                challenger = get_player(challenger_id)
                challenger_name = challenger["first_name"] if challenger else "Someone"
                mode = "daily"
                log.info(f"Challenge: {user.id} vs {challenger_id} (score={challenge_score})")

    # Register / update player
    upsert_player(user.id, user.username or "", user.first_name, referred_by)
    player = get_player(user.id)

    # Build response
    if challenge_score and challenger_name:
        await _send_challenge_welcome(update, user, challenger_name, challenge_score, mode)
    else:
        await _send_main_welcome(update, user, player, referred_by)

async def _send_main_welcome(update, user, player, referred_by):
    pool = get_champ_pool()
    best = player["best_score"] if player else 0
    best_pct = player.get("best_pct", 0) if player else 0

    text = (
        f"⚡ *SNAPski*\n"
        f"_How fast are you?_\n\n"
    )
    if best > 0:
        text += f"Your best: *{best}* pts · *{best_pct}th* percentile\n\n"

    if referred_by:
        ref = get_player(referred_by)
        ref_name = ref["first_name"] if ref else "a friend"
        text += f"_{ref_name} challenged you to beat their score._\n\n"

    text += (
        f"🏆 Championship pool this week: *{pool} ⭐*\n"
        f"Top 3 players split the pool every Monday.\n\n"
        f"Tap below to play."
    )

    url = game_url_for(user.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ PLAY NOW", web_app=WebAppInfo(url=url))
    ],[
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        InlineKeyboardButton("⚡ My Stats", callback_data="stats"),
    ],[
        InlineKeyboardButton("🔗 Challenge a friend", callback_data="get_challenge_link"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def _send_challenge_welcome(update, user, challenger_name, challenge_score, mode):
    text = (
        f"👀 *{challenger_name}* scored *{challenge_score}* points.\n\n"
        f"They think you can't beat it.\n\n"
        f"Tap below and prove them wrong ↓"
    )
    url = game_url_for(user.id, mode=mode, challenge_score=challenge_score, challenger_name=challenger_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⚡ BEAT {challenge_score}", web_app=WebAppInfo(url=url))
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ─────────────────────────────────────────
#  SCORE HANDLER (from game via sendData)
# ─────────────────────────────────────────
async def handle_game_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Receives JSON from the game via Telegram.WebApp.sendData().
    This fires as a regular message with web_app_data.
    """
    user = update.effective_user
    if not update.message or not update.message.web_app_data:
        return

    try:
        data = json.loads(update.message.web_app_data.data)
    except json.JSONDecodeError:
        log.error(f"Bad JSON from game: {update.message.web_app_data.data}")
        return

    action = data.get("action")

    if action == "score":
        await _handle_score(update, user, data)
    elif action == "share":
        await _handle_share(update, user, data)
    elif action == "share_game":
        await _handle_share_game(update, user, data)

async def _handle_score(update, user, data):
    score      = int(data.get("score", 0))
    pct        = float(data.get("pct", 0))
    title      = data.get("title", "BLUNT")
    mode       = data.get("mode", "practice")
    rounds     = int(data.get("rounds", 0))
    chain      = int(data.get("chain", 0))

    # Practice scores don't count
    if mode == "practice":
        await update.message.reply_text(
            f"Practice session: *{score}* pts · Round {rounds}\n_Score not recorded — play Daily or Championship to rank._",
            parse_mode="Markdown"
        )
        return

    is_new_best = save_score(user.id, score, pct, title, mode, rounds, chain)

    # Check if this player beat anyone's challenge
    beaten = get_challenges_beaten(user.id, score)
    if beaten:
        await _notify_beaten_challengers(ctx=None, beaten=beaten, beater=user, score=score, pct=pct, bot=update.get_bot())
        mark_challenges_notified([b["challenger_id"] for b in beaten])

    # Update player's challenge record with their new score
    if score > 0:
        upsert_challenge(user.id, score)

    pool = get_champ_pool()
    text = f"{'🏆 *NEW PERSONAL BEST!*' if is_new_best else '✅ *Score recorded*'}\n\n"
    text += f"Score: *{score}* pts\n"
    text += f"Percentile: *{pct}th*\n"
    text += f"Rank: *{title}*\n"
    text += f"Rounds survived: *{rounds}*\n"
    text += f"Best chain: *{chain}×*\n\n"

    if mode == "championship":
        text += f"Championship pool this week: *{pool} ⭐*"

    # Share button
    ref_link = f"https://t.me/SNAPskiBot?start=challenge_{user.id}_{score}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Challenge friends", url=ref_link)
    ],[
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def _handle_share(update, user, data):
    score = int(data.get("score", 0))
    ref_link = f"https://t.me/SNAPskiBot?start=challenge_{user.id}_{score}"
    await update.message.reply_text(
        f"⚡ *Your challenge link is ready*\n\n"
        f"Anyone who plays from this link competes against your score of *{score}* pts.\n"
        f"You'll be notified if they beat you.\n\n"
        f"`{ref_link}`\n\n"
        f"_Copy and paste into any Telegram chat_",
        parse_mode="Markdown"
    )

async def _handle_share_game(update, user, data):
    best_score = int(data.get("bestScore", 0))
    ref_link = f"https://t.me/SNAPskiBot?start=challenge_{user.id}_{best_score}"
    text = (
        f"⚡ *SNAPski — How fast are you?*\n\n"
        f"Test your reaction time against players worldwide.\n"
        f"Weekly championship · Prize pool in Stars\n\n"
        f"Can you beat *{user.first_name}*'s score of *{best_score}*?\n\n"
        f"👉 {ref_link}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def _notify_beaten_challengers(ctx, beaten, beater, score, pct, bot):
    """Push notification to everyone whose score was just beaten."""
    for b in beaten:
        cid = b["challenger_id"]
        try:
            msg = challenge_notification_text(beater.first_name, score, round(pct, 1))
            ref_link = f"https://t.me/SNAPskiBot?start=challenge_{beater.id}_{score}"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⚡ CHALLENGE THEM BACK", url=ref_link)
            ]])
            await bot.send_message(
                chat_id=cid,
                text=msg,
                reply_markup=keyboard,
            )
            log.info(f"Notified {cid} that {beater.id} beat their score")
        except Exception as e:
            log.warning(f"Failed to notify {cid}: {e}")

# ─────────────────────────────────────────
#  STARS — INVOICES
# ─────────────────────────────────────────
async def send_continue_invoice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a 1-Star invoice for game continue."""
    await update.message.reply_invoice(
        title="Continue Playing",
        description="Buy 1 extra life and keep your score & chain alive.",
        payload=f"continue_{update.effective_user.id}",
        currency="XTR",  # Telegram Stars currency code
        prices=[LabeledPrice("1 Life", PRICE_CONTINUE)],
    )

async def send_championship_invoice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a 5-Star invoice for championship entry."""
    pool = get_champ_pool()
    await update.message.reply_invoice(
        title="Championship Entry",
        description=f"Enter this week's SNAPski Championship. Current prize pool: {pool} Stars. Top 3 win.",
        payload=f"championship_{update.effective_user.id}",
        currency="XTR",
        prices=[LabeledPrice("Championship Entry", PRICE_CHAMPIONSHIP)],
    )

async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Must answer pre-checkout within 10 seconds."""
    await update.pre_checkout_query.answer(ok=True)

async def payment_complete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle successful Stars payment."""
    user = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    stars = update.message.successful_payment.total_amount

    log.info(f"Payment complete: {user.id} paid {stars} Stars ({payload})")

    # Credit referral chain
    credit_referrals(user.id, stars)

    if payload.startswith("continue_"):
        await update.message.reply_text(
            "✅ *Extra life granted!*\nYour score and chain are preserved.\nGo get them. ⚡",
            parse_mode="Markdown"
        )

    elif payload.startswith("championship_"):
        new_pool = add_to_pool(stars)
        await update.message.reply_text(
            f"🏆 *You're in the Championship!*\n\n"
            f"Prize pool is now *{new_pool} ⭐*\n"
            f"Top 3 scores this week win.\n"
            f"Leaderboard resets every Monday at midnight UTC.\n\n"
            f"Your score has been submitted. Good luck. ⚡",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────
#  CALLBACKS — inline buttons
# ─────────────────────────────────────────
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "leaderboard":
        await _show_leaderboard(query, user)
    elif query.data == "stats":
        await _show_stats(query, user)
    elif query.data == "get_challenge_link":
        await _show_challenge_link(query, user)

async def _show_leaderboard(query, user):
    lb = get_weekly_leaderboard()
    pool = get_champ_pool()

    if not lb:
        text = "🏆 *Weekly Championship*\n\nNo scores yet this week.\nBe the first to play! ⚡"
    else:
        text = f"🏆 *Weekly Championship*\nPrize pool: *{pool} ⭐*\n\n"
        for i, row in enumerate(lb, 1):
            p = row.get("players") or {}
            name = p.get("first_name") or p.get("username") or "Player"
            score = row["score"]
            title = row.get("title", "")
            text += f"{rank_emoji(i)} *{name}* — {score} pts {title_to_emoji(title)}\n"

        # Prize breakdown
        text += f"\n🥇 {round(pool*0.5)} ⭐  🥈 {round(pool*0.3)} ⭐  🥉 {round(pool*0.2)} ⭐"
        text += "\n\n_Resets every Monday at midnight UTC_"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Play Now", web_app=WebAppInfo(url=game_url_for(user.id)))
    ]])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def _show_stats(query, user):
    player = get_player(user.id)
    if not player or player["best_score"] == 0:
        await query.edit_message_text(
            "You haven't played yet!\nTap Play to get your first score. ⚡",
        )
        return

    earned = player.get("stars_earned", 0)
    text = (
        f"⚡ *Your SNAPski Stats*\n\n"
        f"Best score: *{player['best_score']}* pts\n"
        f"Percentile: *{player.get('best_pct', 0)}th*\n"
        f"Rank: *{player.get('best_title', 'BLUNT')}*\n\n"
        f"Stars earned from referrals: *{earned} ⭐*\n\n"
        f"_Share your challenge link to earn 20% of every Star your friends spend — forever._"
    )
    ref_link = f"https://t.me/SNAPskiBot?start=challenge_{user.id}_{player['best_score']}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 My Challenge Link", url=f"https://t.me/share/url?url={ref_link}")
    ],[
        InlineKeyboardButton("▶ Play Again", web_app=WebAppInfo(url=game_url_for(user.id)))
    ]])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def _show_challenge_link(query, user):
    player = get_player(user.id)
    score = player["best_score"] if player else 0
    ref_link = f"https://t.me/SNAPskiBot?start=challenge_{user.id}_{score}"
    text = (
        f"🔗 *Your Friend Challenge Link*\n\n"
        f"`{ref_link}`\n\n"
        f"Post this anywhere.\n"
        f"• Friends who play from this link compete against your score\n"
        f"• You get notified if they beat you\n"
        f"• You earn *20% of every Star* they spend in the game — forever\n\n"
        f"_\"Who is faster here? 👇\" — post it and watch them come_"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

# ─────────────────────────────────────────
#  /leaderboard command
# ─────────────────────────────────────────
async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    class FakeQuery:
        from_user = user
        async def edit_message_text(self, *a, **kw):
            await update.message.reply_text(*a, **kw)
        async def answer(self): pass

    await _show_leaderboard(FakeQuery(), user)

# ─────────────────────────────────────────
#  /stats command
# ─────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    player = get_player(user.id)
    if not player or player["best_score"] == 0:
        await update.message.reply_text("No scores yet! Tap /start to play. ⚡")
        return

    earned = player.get("stars_earned", 0)
    text = (
        f"⚡ *Your SNAPski Stats*\n\n"
        f"Best: *{player['best_score']}* pts · *{player.get('best_pct',0)}th* percentile\n"
        f"Rank: *{player.get('best_title','BLUNT')}*\n"
        f"Stars earned from referrals: *{earned} ⭐*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─────────────────────────────────────────
#  WEEKLY PRIZE PAYOUT (run manually or via cron)
# ─────────────────────────────────────────
async def payout_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Admin command: /payout
    Sends Stars to top 3 championship players.
    In production, restrict to admin user_id only.
    """
    lb = get_weekly_leaderboard()
    pool = get_champ_pool()
    if not lb:
        await update.message.reply_text("No scores this week.")
        return

    splits = [0.5, 0.3, 0.2]
    bot = update.get_bot()
    for i, row in enumerate(lb[:3]):
        cid = row["user_id"]
        prize = int(pool * splits[i])
        p = row.get("players") or {}
        name = p.get("first_name") or "Player"
        try:
            # In production use bot.send_invoice or Telegram's refund/transfer API
            # For now notify the winner — actual Stars transfer requires Telegram's payout API
            await bot.send_message(
                cid,
                f"🏆 *You finished #{i+1} in this week's SNAPski Championship!*\n\n"
                f"Your prize: *{prize} ⭐ Stars*\n\n"
                f"_Prize transfer in progress. Thank you for playing._",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Payout notify failed for {cid}: {e}")

    # Reset pool
    db.table("config").upsert({"key": "champ_pool", "value": "0"}).execute()
    await update.message.reply_text(f"✅ Payout sent to top {min(3, len(lb))} players. Pool reset.")

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("payout", payout_weekly))

    # Game data from WebApp
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_game_data))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Stars payment flow
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_complete))

    # Run
    if WEBHOOK_URL:
        log.info(f"Starting webhook on {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8080)),
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="/webhook",
        )
    else:
        log.info("Starting polling (dev mode)")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
