from flask import Flask, request, redirect, Response
from flask_cors import CORS
import psycopg2
import datetime
import json
# v2
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

# Maps campaign slug → crm_data row id.
# The Lakes events have campaign='' (legacy) or 'the_lakes'; both map to slot 1.
# North Lakes events use campaign='north_lakes', stored in slot 2.
_CRM_SLOT = {'': 1, 'the_lakes': 1, 'north_lakes': 2}


def _campaign_where(campaign):
    """Return (sql_fragment, params) for a campaign WHERE condition.
    The Lakes filter includes legacy rows that have campaign='' or NULL."""
    if not campaign:
        return '', ()
    if campaign == 'the_lakes':
        return "(campaign = %s OR campaign = '' OR campaign IS NULL)", (campaign,)
    return 'campaign = %s', (campaign,)


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
            # Add campaign column to events if not present (migration-safe on Postgres 9.6+)
            c.execute("""
                ALTER TABLE events ADD COLUMN IF NOT EXISTS campaign TEXT DEFAULT ''
            """)
        conn.commit()
    finally:
        conn.close()

def log_event(event_type, email, batch, ab_version, ip, user_agent, campaign=''):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                '''INSERT INTO events
                       (event_type, email, batch, ab_version, timestamp, ip, user_agent, campaign)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                (event_type, email, batch, ab_version,
                 datetime.datetime.utcnow().isoformat(), ip, user_agent, campaign)
            )
        conn.commit()
    finally:
        conn.close()


# ── TRACKING ENDPOINTS ────────────────────────────────────────────────────────

@app.route('/track/open')
def track_open():
    email    = request.args.get('email', '')
    batch    = request.args.get('batch', '')
    version  = request.args.get('version', '')
    campaign = request.args.get('campaign', '')
    log_event('open', email, batch, version,
              request.remote_addr, request.headers.get('User-Agent', ''), campaign)
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
    campaign     = request.args.get('campaign', '')
    try:
        log_event('click', email, batch, version,
                  request.remote_addr, request.headers.get('User-Agent', ''), campaign)
    except Exception:
        pass  # always redirect even if DB write fails
    return redirect(redirect_url)


# ── STATS / EVENTS ────────────────────────────────────────────────────────────

@app.route('/stats')
def stats():
    try:
        conn = get_conn()
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500
    try:
        campaign = request.args.get('campaign', '')
        slot_id  = _CRM_SLOT.get(campaign, 1)
        camp_sql, camp_params = _campaign_where(campaign)

        with conn.cursor() as c:
            # Build CRM email -> batch_time lookup from the campaign's Neon mirror slot
            c.execute('SELECT data FROM crm_data WHERE id = %s', (slot_id,))
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

            # Fetch raw event rows filtered by campaign
            camp_clause = ('AND ' + camp_sql) if camp_sql else ''
            c.execute(f"""
                SELECT event_type, lower(email) AS email, batch, ab_version
                FROM events
                WHERE event_type IN ('open', 'click') AND email <> ''
                {camp_clause}
            """, camp_params)
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

        payload = {
            'unique_opens':      len(open_emails),
            'unique_clicks':     len(click_emails),
            'opens_by_batch':    opens_by_batch,
            'clicks_by_batch':   clicks_by_batch,
            'opens_by_version':  opens_by_version,
            'clicks_by_version': clicks_by_version,
            'bounced':           bounced,
        }
        response = app.response_class(
            response=json.dumps(payload),
            status=200,
            mimetype='application/json',
        )
        response.headers['Cache-Control'] = 'no-store'
        return response
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
        campaign = request.args.get('campaign', '')
        camp_sql, camp_params = _campaign_where(campaign)
        camp_clause = ('AND ' + camp_sql) if camp_sql else ''

        with conn.cursor() as c:
            c.execute(f"""
                SELECT event_type,
                       lower(email)    AS email,
                       MIN(ab_version) AS ab_version,
                       MIN(timestamp)  AS first_seen,
                       MAX(timestamp)  AS last_seen,
                       COUNT(*)        AS count
                FROM events
                WHERE event_type IN ('open', 'click') AND email <> '' AND email IS NOT NULL
                {camp_clause}
                GROUP BY event_type, lower(email)
                ORDER BY last_seen DESC
            """, camp_params)
            rows = c.fetchall()
        return json.dumps([{
            'event_type': r[0],
            'email':      r[1],
            'ab_version': r[2],
            'first_seen': r[3],
            'last_seen':  r[4],
            'count':      r[5],
        } for r in rows]), 200, {'Cache-Control': 'no-store'}
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
        campaign = request.args.get('campaign', '')
        slot_id  = _CRM_SLOT.get(campaign, 1)
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
                       VALUES (%s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                           data       = EXCLUDED.data,
                           updated_at = EXCLUDED.updated_at''',
                    (slot_id, json.dumps(data), datetime.datetime.utcnow().isoformat())
                )
            conn.commit()
        finally:
            conn.close()
        return {'status': 'ok', 'rows': len(data), 'campaign': campaign or 'the_lakes', 'slot': slot_id}, 200
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}, 500

@app.route('/crm', methods=['GET'])
def crm_get():
    try:
        campaign = request.args.get('campaign', '')
        slot_id  = _CRM_SLOT.get(campaign, 1)
        conn = get_conn()
        try:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS crm_data (
                        id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT
                    )
                ''')
                c.execute('SELECT data, updated_at FROM crm_data WHERE id = %s', (slot_id,))
                row = c.fetchone()
        finally:
            conn.close()
        resp_data = {'data': json.loads(row[0]), 'updated_at': row[1]} if row else {'data': [], 'updated_at': None}
        response = app.response_class(
            response=json.dumps(resp_data),
            status=200,
            mimetype='application/json',
        )
        response.headers['Cache-Control'] = 'no-store'
        return response
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
