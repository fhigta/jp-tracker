from flask import Flask, request, redirect, Response
from flask_cors import CORS
import psycopg2
import datetime
import json
import os

app = Flask(__name__)
CORS(app)


# ── DATABASE ──────────────────────────────────────────────────────────────────

def _db_url():
    url = os.environ.get('DATABASE_URL', '')
    # Some providers (Heroku, older Render) give postgres:// — psycopg2 requires postgresql://
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return url

def get_conn():
    return psycopg2.connect(_db_url())

def init_db():
    url = _db_url()
    if not url:
        print('[tracker] DATABASE_URL not set — skipping init_db()')
        return
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS events (
                    id         SERIAL PRIMARY KEY,
                    event_type TEXT,
                    email      TEXT,
                    batch      TEXT,
                    ab_version TEXT,
                    timestamp  TEXT,
                    ip         TEXT,
                    user_agent TEXT
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS crm_data (
                    id         INTEGER PRIMARY KEY,
                    data       TEXT,
                    updated_at TEXT
                )
            ''')
        conn.commit()
    finally:
        conn.close()

def log_event(event_type, email, batch, ab_version, ip, user_agent):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                '''INSERT INTO events
                       (event_type, email, batch, ab_version, timestamp, ip, user_agent)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                (event_type, email, batch, ab_version,
                 datetime.datetime.utcnow().isoformat(), ip, user_agent)
            )
        conn.commit()
    finally:
        conn.close()


# ── TRACKING ENDPOINTS ────────────────────────────────────────────────────────

@app.route('/track/open')
def track_open():
    email   = request.args.get('email', '')
    batch   = request.args.get('batch', '')
    version = request.args.get('version', '')
    log_event('open', email, batch, version,
              request.remote_addr, request.headers.get('User-Agent', ''))
    pixel = (
        b'GIF89a\x01\x00\x01\x00\x80\x00\x00'
        b'\xff\xff\xff\x00\x00\x00!\xf9\x04'
        b'\x00\x00\x00\x00\x00,\x00\x00\x00'
        b'\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
    )
    return Response(pixel, mimetype='image/gif')

@app.route('/track/click')
def track_click():
    email        = request.args.get('email', '')
    redirect_url = request.args.get('redirect', 'https://calendly.com/jpfigallo-concierge/30min')
    version      = request.args.get('version', '')
    batch        = request.args.get('batch', '')
    log_event('click', email, batch, version,
              request.remote_addr, request.headers.get('User-Agent', ''))
    return redirect(redirect_url)


# ── STATS / EVENTS ────────────────────────────────────────────────────────────

@app.route('/stats')
def stats():
    try:
        conn = get_conn()
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500
    try:
        with conn.cursor() as c:
            # Build CRM email -> batch_time lookup from the Neon mirror
            c.execute('SELECT data FROM crm_data WHERE id = 1')
            crm_row = c.fetchone()
            crm_batch_map = {}
            bounced = 0
            if crm_row:
                crm_rows = json.loads(crm_row[0])
                bounced = sum(
                    1 for r in crm_rows
                    if (r.get('status') or '').upper().strip() == 'BOUNCED'
                )
                for r in crm_rows:
                    email = (r.get('email') or '').strip().lower()
                    bt    = (r.get('batch_time') or '').strip().lower()
                    isd   = (r.get('initial_sent_date') or '').strip()
                    if email and bt and isd:
                        crm_batch_map[email] = bt

            def resolve_batch(email, stored_batch):
                """Return stored batch if set; fall back to CRM lookup for pre-batch-param sends."""
                if stored_batch:
                    return stored_batch
                return crm_batch_map.get(email.lower(), '')

            # Fetch raw event rows (non-empty email only)
            c.execute("""
                SELECT event_type, lower(email) AS email, batch, ab_version
                FROM events
                WHERE event_type IN ('open', 'click') AND email <> ''
            """)
            raw_events = c.fetchall()

        # Aggregate with CRM-resolved batch labels
        open_emails         = set()
        click_emails        = set()
        opens_by_batch      = {}
        clicks_by_batch     = {}
        opens_by_version    = {}
        clicks_by_version   = {}
        # track per-batch distinct emails
        open_emails_batch   = {}
        click_emails_batch  = {}
        open_emails_version = {}
        click_emails_version= {}

        for etype, email, batch, version in raw_events:
            batch   = resolve_batch(email, batch)
            version = version or ''
            if etype == 'open':
                open_emails.add(email)
                open_emails_batch.setdefault(batch, set()).add(email)
                open_emails_version.setdefault(version, set()).add(email)
            elif etype == 'click':
                click_emails.add(email)
                click_emails_batch.setdefault(batch, set()).add(email)
                click_emails_version.setdefault(version, set()).add(email)

        opens_by_batch    = {k: len(v) for k, v in open_emails_batch.items()}
        clicks_by_batch   = {k: len(v) for k, v in click_emails_batch.items()}
        opens_by_version  = {k: len(v) for k, v in open_emails_version.items()}
        clicks_by_version = {k: len(v) for k, v in click_emails_version.items()}

        return {
            'unique_opens':      len(open_emails),
            'unique_clicks':     len(click_emails),
            'opens_by_batch':    opens_by_batch,
            'clicks_by_batch':   clicks_by_batch,
            'opens_by_version':  opens_by_version,
            'clicks_by_version': clicks_by_version,
            'bounced':           bounced,
        }
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500
    finally:
        conn.close()

@app.route('/events')
def events():
    try:
        conn = get_conn()
    except Exception as e:
        return json.dumps({'error': str(e)}), 500
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT event_type,
                       lower(email)    AS email,
                       MIN(ab_version) AS ab_version,
                       MIN(timestamp)  AS first_seen,
                       MAX(timestamp)  AS last_seen,
                       COUNT(*)        AS count
                FROM events
                WHERE event_type IN ('open', 'click') AND email <> ''
                GROUP BY event_type, lower(email)
                ORDER BY last_seen DESC
            """)
            rows = c.fetchall()
        return json.dumps([{
            'event_type': r[0],
            'email':      r[1],
            'ab_version': r[2],
            'first_seen': r[3],
            'last_seen':  r[4],
            'count':      r[5],
        } for r in rows])
    except Exception as e:
        return json.dumps({'error': str(e)}), 500
    finally:
        conn.close()


# ── WEBHOOKS ──────────────────────────────────────────────────────────────────

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
        log_event('booking', email, '', '',
                  request.remote_addr, request.headers.get('User-Agent', ''))
        return {'status': 'success', 'email': email}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500


# ── CRM MIRROR ────────────────────────────────────────────────────────────────

@app.route('/crm/update', methods=['POST'])
def crm_update():
    try:
        data = request.get_json(silent=True) or []
        conn = get_conn()
        try:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS crm_data (
                        id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT
                    )
                ''')
                c.execute(
                    '''INSERT INTO crm_data (id, data, updated_at)
                       VALUES (1, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                           data       = EXCLUDED.data,
                           updated_at = EXCLUDED.updated_at''',
                    (json.dumps(data), datetime.datetime.utcnow().isoformat())
                )
            conn.commit()
        finally:
            conn.close()
        return {'status': 'ok', 'rows': len(data)}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500

@app.route('/crm', methods=['GET'])
def crm_get():
    try:
        conn = get_conn()
        try:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS crm_data (
                        id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT
                    )
                ''')
                c.execute('SELECT data, updated_at FROM crm_data WHERE id = 1')
                row = c.fetchone()
        finally:
            conn.close()
        if row:
            return {'data': json.loads(row[0]), 'updated_at': row[1]}, 200
        return {'data': [], 'updated_at': None}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500


# ── DIAGNOSTICS ──────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    url = _db_url()
    connected = False
    error = None
    if url:
        try:
            conn = get_conn()
            conn.close()
            connected = True
        except Exception as e:
            error = str(e)
    # Show any env var keys that look DB-related (names only, not values)
    db_keys = sorted(
        k for k in os.environ
        if any(x in k.upper() for x in ['DATABASE', 'POSTGRES', 'PG', 'NEON', 'DB'])
    )
    return {
        'DATABASE_URL_set': bool(url),
        'DATABASE_URL_length': len(url),
        'DATABASE_URL_prefix': (url[:35] + '...') if len(url) > 35 else url,
        'connected': connected,
        'error': error,
        'db_related_env_keys': db_keys,
    }


# ── STARTUP ───────────────────────────────────────────────────────────────────

try:
    init_db()
    print('[tracker] init_db() OK')
except Exception as _init_err:
    print(f'[tracker] init_db() FAILED — app will start but DB ops will error: {_init_err}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
