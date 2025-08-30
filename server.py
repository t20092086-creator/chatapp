import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse, Response
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv
import sqlite3
from datetime import datetime, timezone
import socketio
import socket
import asyncio
import aiohttp
from fastapi.staticfiles import StaticFiles

# Load environment variables from .env file
load_dotenv()

VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY')

# Initialize FastAPI and SocketIO
app = FastAPI()
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=10 * 1024 * 1024,
    ping_interval=3,
    ping_timeout=5,
)
sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")
app.mount("/socket.io", sio_app)

# Database and room management
DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
ROOM_USERS = {}  # { room: { username: sid } }
LAST_MESSAGE = {}  # {(room, username): (text, ts)}

# ---------------- Database ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender TEXT NOT NULL,
            text TEXT,
            filename TEXT,
            mimetype TEXT,
            filedata TEXT,
            ts TEXT NOT NULL
        )
    """
    )
    conn.commit()
    conn.close()

def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA table_info(messages)")
    existing_cols = [row[1] for row in c.fetchall()]
    if "filename" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN filename TEXT")
    if "mimetype" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN mimetype TEXT")
    if "filedata" not in existing_cols:
        c.execute("ALTER TABLE messages ADD COLUMN filedata TEXT")
    conn.commit()
    conn.close()

def count_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    result = c.fetchone()[0]
    conn.close()
    return result

def cleanup_old_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    before = count_messages()
    c.execute("DELETE FROM messages WHERE ts < datetime('now', '-48 hours')")
    conn.commit()
    after = count_messages()
    conn.close()
    return before - after

def save_message(room, sender, text=None, filename=None, mimetype=None, filedata=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (room, sender, text, filename, mimetype, filedata, ts) VALUES (?,?,?,?,?,?,?)",
        (
            room,
            sender,
            text,
            filename,
            mimetype,
            filedata,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    deleted = cleanup_old_messages()
    if deleted > 0:
        asyncio.create_task(
            sio.emit(
                "cleanup",
                {"message": f"{deleted} old messages (48h+) were removed."},
                room=room,
            )
        )

def load_messages(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT sender, text, filename, mimetype, filedata, ts FROM messages WHERE room=? ORDER BY id ASC",
        (room,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

def clear_room(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE room=?", (room,))
    conn.commit()
    conn.close()

# ---------------- Push Notification ----------------
# Store subscriptions in memory or database
subscriptions = []

@app.post("/api/subscribe")
async def subscribe(subscription: dict):
    subscriptions.append(subscription)
    return JSONResponse(status_code=200, content={"message": "Subscribed successfully!"})

@app.post("/send-push-notification")
async def send_push_notification():
    # The payload that will be sent to the subscribers
    payload = {
        "title": "Test Message",
        "body": "This is a test notification sent even if the chat is closed."
    }

    for sub in subscriptions:
        try:
            # Sending the push notification
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": "mailto:example@domain.com"}
            )
            print(f"Push sent to {sub['endpoint']}")
        except WebPushException as e:
            print(f"Failed to send push to {sub['endpoint']}: {e}")
            # Optionally, remove subscription if it’s no longer valid
            subscriptions.remove(sub)

    return {"status": "Push notification sent"}

# ---------------- Helper ----------------
async def broadcast_users(room):
    users = [
        {"name": username, "status": "online"} for username in ROOM_USERS.get(room, {})
    ]
    await sio.emit("users_update", {"room": room, "users": users}, room=room)

# ---------------- FastAPI + Socket.IO ----------------
@app.delete("/clear/{room}")
async def clear_messages(room: str):
    clear_room(room)
    await sio.emit(
        "clear", {"room": room, "message": "Room history cleared."}, room=room
    )
    return JSONResponse({"status": "ok", "message": f"Room {room} cleared."})

@app.delete("/destroy/{room}")
async def destroy_room(room: str):
    clear_room(room)
    DESTROYED_ROOMS.add(room)
    if room in ROOM_USERS:
        del ROOM_USERS[room]
    await sio.emit(
        "clear",
        {"room": room, "message": "Room destroyed. All messages cleared."},
        room=room,
    )
    await sio.emit("room_destroyed", {"room": room}, room=room)
    namespace = "/"
    if namespace in sio.manager.rooms and room in sio.manager.rooms[namespace]:
        sids = list(sio.manager.rooms[namespace][room])
        for sid in sids:
            await sio.leave_room(sid, room, namespace=namespace)
    return JSONResponse({"status": "ok", "message": f"Room {room} destroyed."})

# ---------------- Socket.IO Events ----------------
@sio.event
async def join(sid, data):
    room = data["room"]
    username = data["sender"]
    last_ts = data.get("lastTs")

    if room in DESTROYED_ROOMS:
        DESTROYED_ROOMS.remove(room)

    if room not in ROOM_USERS:
        ROOM_USERS[room] = {}

    old_sid = ROOM_USERS[room].get(username)

    # Already joined with same sid → do nothing
    if old_sid == sid:
        return {"success": True, "message": "Already in room"}

    # If joined from another device/browser → replace old sid
    if old_sid and old_sid != sid:
        try:
            await sio.leave_room(old_sid, room)
        except Exception:
            pass

    ROOM_USERS[room][username] = sid
    await sio.enter_room(sid, room)
    await broadcast_users(room)

    # Send only missed messages
    for sender_, text, filename, mimetype, filedata, ts in load_messages(room):
        if last_ts and ts <= last_ts:
            continue
        if filename:
            await sio.emit(
                "file",
                {
                    "sender": sender_,
                    "filename": filename,
                    "mimetype": mimetype,
                    "data": filedata,
                    "ts": ts,
                },
                to=sid,
            )
        else:
            await sio.emit(
                "message", {"sender": sender_, "text": text, "ts": ts}, to=sid
            )

    if not old_sid:
        await sio.emit(
            "message",
            {
                "sender": "System",
                "text": f"{username} joined!",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            room=room,
        )

    return {"success": True}

@sio.event
async def message(sid, data):
    room = data["room"]
    sender = data["sender"]
    text = (data["text"] or "").strip()
    now = datetime.now(timezone.utc)

    if not text:
        return  # ignore empty

    key = (room, sender)
    last = LAST_MESSAGE.get(key)

    # Prevent duplicate within 1.5 sec
    if last and last[0] == text and (now - last[1]).total_seconds() < 1.5:
        return
    LAST_MESSAGE[key] = (text, now)

    if room in DESTROYED_ROOMS:
        return

    save_message(room, sender, text=text)
    await sio.emit(
        "message",
        {
            "sender": sender,
            "text": text,
            "ts": now.isoformat(),
        },
        room=room,
    )

@sio.event
async def file(sid, data):
    room = data["room"]
    if room in DESTROYED_ROOMS:
        return
    save_message(
        room,
        data["sender"],
        filename=data["filename"],
        mimetype=data["mimetype"],
        filedata=data["data"],
    )
    await sio.emit(
        "file",
        {
            "sender": data["sender"],
            "filename": data["filename"],
            "mimetype": data["mimetype"],
            "data": data["data"],
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )

@sio.event
async def leave(sid, data):
    room = data["room"]
    username = data["sender"]
    if room in ROOM_USERS and username in ROOM_USERS[room]:
        del ROOM_USERS[room][username]
        if not ROOM_USERS[room]:
            del ROOM_USERS[room]
    await sio.leave_room(sid, room)
    await broadcast_users(room)
    await sio.emit("left_room", {"room": room}, to=sid)
    await sio.emit(
        "message",
        {
            "sender": "System",
            "text": f"{username} left!",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )

@sio.event
async def disconnect(sid):
    for room, users in list(ROOM_USERS.items()):
        for username, user_sid in list(users.items()):
            if user_sid == sid:
                if ROOM_USERS[room].get(username) != sid:
                    continue
                del users[username]
                if not users:
                    del ROOM_USERS[room]
                await broadcast_users(room)
                await sio.emit(
                    "message",
                    {
                        "sender": "System",
                        "text": f"{username} disconnected.",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                    room=room,
                )

# ---------------- Background Cleanup + KeepAlive ----------------
@app.on_event("startup")
async def startup_tasks():
    async def loop_cleanup():
        while True:
            deleted = cleanup_old_messages()
            if deleted > 0:
                await sio.emit(
                    "cleanup",
                    {"message": f"{deleted} old messages (48h+) were removed."},
                )
            await asyncio.sleep(3600)

    async def ping_self():
        url = "https://realtime-chat-1mv3.onrender.com"
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        print(f"[KeepAlive] Pinged {url} - {resp.status}")
            except Exception as e:
                print(f"[KeepAlive] Error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(loop_cleanup())
    asyncio.create_task(ping_self())

# ---------------- Static Files ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"))

@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"))

@app.get("/sitemap.xml")
def sitemap():
    base_url = "https://realtime-chat-1mv3.onrender.com"
    static_pages = [
        "index.html",
        "about.html",
        "blog.html",
        "contact.html",
        "disclaimer.html",
        "privacy-policy.html",
        "terms-of-service.html",
    ]

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for page in static_pages:
        url = page.replace("index.html", "")
        if url == "":
            loc = f"{base_url}/"
        else:
            loc = f"{base_url}/{url}"
        xml += f"  <url><loc>{loc}</loc></url>\n"
    xml += "</urlset>"
    return Response(content=xml, media_type="application/xml")

@app.get("/ads.txt")
def ads():
    return FileResponse("ads.txt", media_type="text/plain")

@app.get("/robots.txt")
def robots():
    return FileResponse("robots.txt", media_type="text/plain")

app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

# ---------------- Run Server ----------------
if __name__ == "__main__":
    import uvicorn

    init_db()
    migrate_db()
    local_ip = socket.gethostbyname(socket.gethostname())
    print("🚀 Server running at:")
    print("   ➤ Local:   http://127.0.0.1:8000")
    print(f"   ➤ Network: http://{local_ip}:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
