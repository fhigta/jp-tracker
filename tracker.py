from flask import Flask, request, redirect, Response
import sqlite3
import datetime
import json
import os

app = Flask(__name__)
DB_PATH = "tracking.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT,
        email TEXT,
        batch TEXT,
        ab_version TEXT,
        timestamp TEXT,
        ip TEXT,
        user_agent TEXT
    )''')
    conn.commit()
    conn.close()

def log_event(event_type, email, batch, ab_version, ip, user_agent):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO events
        (event_type, email, batch, ab_version, timestamp, ip, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (event_type, email, batch, ab_version,
         datetime.datetime.utcnow().isoformat(),
         ip, user_agent))
    conn.commit()
    conn.close()

@app.route('/track/open')
def track_open():
    email = request.args.get('email', '')
    batch = request.args.get('batch', '')
    version = request.args.get('version', '')
    ip = request.remote_addr
    ua = request.headers.get('User-Agent', '')
    log_event('open', email, batch, version, ip, ua)
    pixel = (
        b'GIF89a\x01\x00\x01\x00\x80\x00\x00'
        b'\xff\xff\xff\x00\x00\x00!\xf9\x04'
        b'\x00\x00\x00\x00\x00,\x00\x00\x00'
        b'\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
    )
    return Response(pixel, mimetype='image/gif')

@app.route('/track/click')
def track_click():
    email = request.args.get('email', '')
    redirect_url = request.args.get('redirect', 'https://calendly.com/jpfigallo-concierge/30min')
    version = request.args.get('version', '')
    ip = request.remote_addr
    ua = request.headers.get('User-Agent', '')
    log_event('click', email, '', version, ip, ua)
    return redirect(redirect_url)

@app.route('/stats')
def stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    opens = c.execute("SELECT COUNT(DISTINCT email) FROM events WHERE event_type='open'").fetchone()[0]
    clicks = c.execute("SELECT COUNT(DISTINCT email) FROM events WHERE event_type='click'").fetchone()[0]
    by_batch = c.execute("""
        SELECT batch, COUNT(DISTINCT email)
        FROM events WHERE event_type='open'
        GROUP BY batch
    """).fetchall()
    opens_by_version = c.execute("""
        SELECT ab_version, COUNT(DISTINCT email)
        FROM events WHERE event_type='open'
        GROUP BY ab_version
    """).fetchall()
    clicks_by_version = c.execute("""
        SELECT ab_version, COUNT(DISTINCT email)
        FROM events WHERE event_type='click'
        GROUP BY ab_version
    """).fetchall()
    conn.close()
    return {
        "unique_opens": opens,
        "unique_clicks": clicks,
        "opens_by_batch": dict(by_batch),
        "opens_by_version": dict(opens_by_version),
        "clicks_by_version": dict(clicks_by_version)
    }
@app.route('/webhook/calendly', methods=['POST'])
def calendly_webhook():
    try:
        data = request.get_json(silent=True) or {}
        try:
            email = data['payload']['invitee']['email'].strip().lower()
        except (KeyError, TypeError):
            try:
                email = data['invitee']['email'].strip().lower()
            except (KeyError, TypeError):
                email = None
        if not email:
            return {'status': 'no_email'}, 200
        log_event('booking', email, '', request.remote_addr, request.headers.get('User-Agent', ''))
        return {'status': 'success', 'email': email}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500
@app.route('/crm/update', methods=['POST'])
def crm_update():
    try:
        data = request.get_json(silent=True) or []
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS crm_data (
            id INTEGER PRIMARY KEY,
            data TEXT,
            updated_at TEXT
        )''')
        c.execute('''INSERT INTO crm_data (id, data, updated_at)
                     VALUES (1, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET
                     data=excluded.data,
                     updated_at=excluded.updated_at''',
                  (json.dumps(data), datetime.datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        return {'status': 'ok', 'rows': len(data)}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500

@app.route('/crm', methods=['GET'])
def crm_get():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS crm_data (
            id INTEGER PRIMARY KEY,
            data TEXT,
            updated_at TEXT
        )''')
        row = c.execute('SELECT data, updated_at FROM crm_data WHERE id=1').fetchone()
        conn.close()
        if row:
            return {'data': json.loads(row[0]), 'updated_at': row[1]}, 200
        return {'data': [], 'updated_at': None}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
