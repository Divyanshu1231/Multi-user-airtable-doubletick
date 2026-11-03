import os, re, json, hmac, hashlib, secrets, sqlite3, time, requests, threading, copy, uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from matcher import load_candidate_records_auto, candidate_fields, send_doubletick_candidate_message

BASE=Path(__file__).resolve().parent
load_dotenv(BASE/'.env')
DB= os.getenv('DATABASE_PATH', str(BASE/'data'/'app.db'))
Path(DB).parent.mkdir(parents=True, exist_ok=True)
SECRET=os.getenv('APP_SECRET','').strip()
if not SECRET: SECRET='dev-'+secrets.token_urlsafe(32)
app=FastAPI(title='Space Consultant Recruitment Messaging API', version='1.0.0')

# DATABASE_URL selects PostgreSQL; otherwise local SQLite is used.
DATABASE_URL=os.getenv('DATABASE_URL','').strip()
try:
    from psycopg2 import IntegrityError as PostgresIntegrityError
    from psycopg2.extras import RealDictCursor
except ImportError:
    PostgresIntegrityError=Exception

class PgCursor:
    def __init__(self, cursor, inserted_id=None):
        self._cursor=cursor; self.lastrowid=inserted_id
    def fetchone(self): return self._cursor.fetchone()
    def fetchall(self): return self._cursor.fetchall()

class PgDB:
    def __init__(self):
        import psycopg2
        self.conn=psycopg2.connect(DATABASE_URL, sslmode='require', connect_timeout=15)
    def execute(self, sql, params=()):
        sql=sql.replace('BEGIN IMMEDIATE','BEGIN')
        if 'PRAGMA table_info(templates)' in sql:
            cur=self.conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='templates'")
            return PgCursor(cur)
        sql=sql.replace("date(created_at,'+5 hours','+30 minutes')=date('now','+5 hours','+30 minutes')", "(created_at::timestamp + interval '5 hours 30 minutes')::date=(CURRENT_TIMESTAMP + interval '5 hours 30 minutes')::date")
        sql=sql.replace('?', '%s')
        wants_id=sql.lstrip().upper().startswith('INSERT INTO LOGS ') and 'RETURNING' not in sql.upper()
        if wants_id: sql += ' RETURNING id'
        cur=self.conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        inserted_id=None
        if wants_id:
            row=cur.fetchone(); inserted_id=row['id'] if row else None
        return PgCursor(cur, inserted_id)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

def db():
    if DATABASE_URL:
        c=PgDB()
        c.execute("CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS templates(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, template_name TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        existing={r['name'] for r in c.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='templates'").fetchall()}
        for col,ddl in {'language':"TEXT DEFAULT 'en'",'category':"TEXT DEFAULT 'MARKETING'",'approval_status':"TEXT DEFAULT 'DRAFT'",'header_text':"TEXT DEFAULT ''",'footer_text':"TEXT DEFAULT ''",'provider_detail':"TEXT DEFAULT ''",'submitted_at':"TEXT DEFAULT ''"}.items():
            if col not in existing: c.execute(f'ALTER TABLE templates ADD COLUMN {col} {ddl}')
        c.execute("CREATE TABLE IF NOT EXISTS chat_messages(id BIGSERIAL PRIMARY KEY, user_id BIGINT, phone TEXT NOT NULL, candidate_name TEXT DEFAULT '', direction TEXT NOT NULL, message_type TEXT DEFAULT 'TEXT', content TEXT DEFAULT '', media_url TEXT DEFAULT '', provider_message_id TEXT DEFAULT '', status TEXT DEFAULT 'RECEIVED', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        chat_cols={r['name'] for r in c.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='chat_messages'").fetchall()}
        if 'status' not in chat_cols: c.execute("ALTER TABLE chat_messages ADD COLUMN status TEXT DEFAULT 'RECEIVED'")
        c.execute("CREATE TABLE IF NOT EXISTS logs(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, candidate_name TEXT, phone TEXT, kind TEXT, template_name TEXT, status TEXT, detail TEXT, provider_message_id TEXT DEFAULT '', status_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        log_cols={r['name'] for r in c.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='logs'").fetchall()}
        for col,ddl in {'provider_message_id':"TEXT DEFAULT ''",'status_updated_at':"TIMESTAMP DEFAULT CURRENT_TIMESTAMP"}.items():
            if col not in log_cols: c.execute(f'ALTER TABLE logs ADD COLUMN {col} {ddl}')
        c.commit(); return c
    c=sqlite3.connect(os.getenv('DATABASE_PATH', str(BASE/'data'/'app.db'))); c.row_factory=sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, template_name TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    existing={r['name'] for r in c.execute('PRAGMA table_info(templates)').fetchall()}
    for col,ddl in {'language':"TEXT DEFAULT 'en'",'category':"TEXT DEFAULT 'MARKETING'",'approval_status':"TEXT DEFAULT 'DRAFT'",'header_text':"TEXT DEFAULT ''",'footer_text':"TEXT DEFAULT ''",'provider_detail':"TEXT DEFAULT ''",'submitted_at':"TEXT DEFAULT ''"}.items():
        if col not in existing: c.execute(f'ALTER TABLE templates ADD COLUMN {col} {ddl}')
    c.execute("CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT NOT NULL, candidate_name TEXT DEFAULT '', direction TEXT NOT NULL, message_type TEXT DEFAULT 'TEXT', content TEXT DEFAULT '', media_url TEXT DEFAULT '', provider_message_id TEXT DEFAULT '', status TEXT DEFAULT 'RECEIVED', created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    chat_cols={r['name'] for r in c.execute('PRAGMA table_info(chat_messages)').fetchall()}
    if 'status' not in chat_cols: c.execute("ALTER TABLE chat_messages ADD COLUMN status TEXT DEFAULT 'RECEIVED'")
    c.execute("CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, candidate_name TEXT, phone TEXT, kind TEXT, template_name TEXT, status TEXT, detail TEXT, provider_message_id TEXT DEFAULT '', status_updated_at TEXT DEFAULT CURRENT_TIMESTAMP, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    existing_logs={r['name'] for r in c.execute('PRAGMA table_info(logs)').fetchall()}
    for col,ddl in {'provider_message_id':"TEXT DEFAULT ''",'status_updated_at':"TEXT DEFAULT CURRENT_TIMESTAMP"}.items():
        if col not in existing_logs: c.execute(f'ALTER TABLE logs ADD COLUMN {col} {ddl}')
    c.commit(); return c

def phash(pw, salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+'$'+hashlib.pbkdf2_hmac('sha256',pw.encode(),salt.encode(),240000).hex()
def verify(pw, saved):
    try:
        salt, digest=saved.split('$',1)
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',pw.encode(),salt.encode(),240000).hex(),digest)
    except Exception:return False
def token(uid, role):
    payload=f'{uid}:{role}:{int(time.time())+86400}'
    sig=hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode((payload+':'+sig).encode()).decode().rstrip('=')
def current_user(authorization: Optional[str]=Header(None)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Login required')
    import base64
    try:
        raw=base64.urlsafe_b64decode(authorization[7:]+'===').decode(); uid,role,exp,sig=raw.split(':')
        payload=f'{uid}:{role}:{exp}'
        if int(exp)<time.time() or not hmac.compare_digest(sig,hmac.new(SECRET.encode(),payload.encode(),hashlib.sha256).hexdigest()): raise ValueError()
        c=db(); u=c.execute('SELECT id,username,role,active FROM users WHERE id=?',(uid,)).fetchone(); c.close()
        if not u or not u['active']: raise ValueError()
        return dict(u)
    except Exception: raise HTTPException(401,'Invalid or expired session')
def admin(u=Depends(current_user)):
    if u['role']!='admin': raise HTTPException(403,'Admin access required')
    return u

def get_setting(key, default=''):
    c=db(); r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone(); c.close(); return r['value'] if r else default
def airtable_urls():
    raw=get_setting('airtable_urls','')
    if raw:
        try:
            urls=json.loads(raw)
            if isinstance(urls,list): return [str(x).strip() for x in urls if str(x).strip()]
        except Exception: pass
    legacy=get_setting('airtable_url','').strip()
    return [legacy] if legacy else []
# Airtable candidate cache: filter changes reuse the same fetched records instead
# of downloading every Airtable view again. Cache is per running app worker.
CANDIDATE_CACHE_TTL = max(30, int(os.getenv('AIRTABLE_CACHE_TTL_SECONDS', '300')))
_candidate_cache = {'key': None, 'expires': 0.0, 'rows': None}
_candidate_cache_lock = threading.Lock()

def clear_candidate_cache():
    with _candidate_cache_lock:
        _candidate_cache.update({'key': None, 'expires': 0.0, 'rows': None})

def records(force_refresh=False):
    urls=airtable_urls()
    if not urls: raise HTTPException(400,'Admin has not configured any Airtable candidate view URLs.')
    cache_key=tuple(urls)
    now=time.monotonic()
    if not force_refresh and _candidate_cache['key']==cache_key and _candidate_cache['rows'] is not None and now < _candidate_cache['expires']:
        return copy.deepcopy(_candidate_cache['rows'])
    try:
        # Lock prevents simultaneous filter requests from all fetching Airtable.
        with _candidate_cache_lock:
            now=time.monotonic()
            if not force_refresh and _candidate_cache['key']==cache_key and _candidate_cache['rows'] is not None and now < _candidate_cache['expires']:
                return copy.deepcopy(_candidate_cache['rows'])
            token_value=os.getenv('AIRTABLE_TOKEN','').strip()
            combined=[]; seen=set()
            for index,url in enumerate(urls):
                rows,_=load_candidate_records_auto(url,token_value)
                for row in rows:
                    rid=str(row.get('_record_id') or abs(hash(json.dumps(row,sort_keys=True,default=str))))
                    # Prefix IDs by source so records from different Airtable bases cannot collide.
                    unique=f'{index}:{rid}'
                    if unique in seen: continue
                    seen.add(unique); row['_record_id']=unique; combined.append(row)
            _candidate_cache.update({'key':cache_key, 'expires':time.monotonic()+CANDIDATE_CACHE_TTL, 'rows':combined})
            return copy.deepcopy(combined)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502,'Airtable fetch failed: '+str(e))
def norm(v): return re.sub(r'\s+',' ',str(v or '').strip()).casefold()
def values(v):
    if isinstance(v,list): return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if str(v or '').strip() else []
def row_for(r):
    f=candidate_fields(r)
    return {'id':r.get('_record_id') or str(abs(hash(json.dumps(r,sort_keys=True,default=str)))), 'name':str(f.get('name') or ''),'phone':str(f.get('phone') or ''),'specialization':values(f.get('specialization')),'state':values(f.get('state') or f.get('location')),'city':values(f.get('city')),'raw':r}
def replace_vars(text,c):
    f=candidate_fields(c)
    d={'name':f.get('name',''),'doctor_name':f.get('name',''),'specialization':f.get('specialization',''),'city':f.get('city',''),'state':f.get('state','')}
    return re.sub(r'\{\{\s*([a-zA-Z_]+)\s*\}\}',lambda m:str(d.get(m.group(1).lower(),'')),text)

class Login(BaseModel): username:str; password:str
class UserCreate(BaseModel): username:str; password:str=Field(min_length=8)
class UserUpdate(BaseModel): password:Optional[str]=None; active:Optional[bool]=None
class AirtableConfig(BaseModel): urls:list[str]=[]; max_messages_per_user_per_day:int=50
class TemplateIn(BaseModel):
    name:str; kind:str='custom'; content:str; template_name:str=''
    language:str='en'; category:str='MARKETING'; header_text:str=''; footer_text:str=''
class SendIn(BaseModel): candidate_ids:list[str]; kind:str; template_name:str=''; message:str=''; language:str='en'
class ChatReplyIn(BaseModel): message:str=Field(min_length=1,max_length=4000)

@app.on_event('startup')
def startup():
    # ADMIN_USERNAME / ADMIN_PASSWORD are the authoritative bootstrap credentials.
    # Existing HR users, templates, settings, logs and chats are never deleted.
    c=db()
    try:
        username=(os.getenv('ADMIN_USERNAME') or 'admin').strip()
        password=os.getenv('ADMIN_PASSWORD') or ''
        admin_row=c.execute("SELECT id,username FROM users WHERE role='admin' ORDER BY id ASC LIMIT 1").fetchone()
        if not admin_row:
            # Create the first admin only when no admin account exists.
            if not password:
                password=secrets.token_urlsafe(18)
                print('INITIAL ADMIN PASSWORD was generated. Set ADMIN_PASSWORD in Render Environment.')
            c.execute("INSERT INTO users(username,password_hash,role,active) VALUES(?,?,'admin',1)",(username,phash(password)))
            c.commit()
        elif password:
            # Fixes the common case where the database was initialized before
            # ADMIN_PASSWORD was added to Render. Update only the admin login row.
            # If the requested username belongs to an HR account, do not overwrite it.
            name_owner=c.execute('SELECT id,role FROM users WHERE username=?',(username,)).fetchone()
            if name_owner and name_owner['id'] != admin_row['id']:
                print('ADMIN CREDENTIAL SYNC skipped: ADMIN_USERNAME is already used by another account.')
            else:
                c.execute('UPDATE users SET username=?, password_hash=?, active=1 WHERE id=?',
                          (username,phash(password),admin_row['id']))
                c.commit()
                print('Admin login credentials synchronized from Render Environment Variables.')
    finally:
        c.close()

@app.get('/')
def home(): return FileResponse(BASE/'static'/'index.html')
@app.get('/health')
def health(): return {'ok':True}
@app.post('/api/login')
def login(x:Login):
    c=db(); u=c.execute('SELECT * FROM users WHERE username=?',(x.username.strip(),)).fetchone(); c.close()
    if not u or not u['active'] or not verify(x.password,u['password_hash']): raise HTTPException(401,'Incorrect username or password')
    return {'token':token(u['id'],u['role']),'user':{'id':u['id'],'username':u['username'],'role':u['role']}}
@app.get('/api/me')
def me(u=Depends(current_user)): return u
@app.get('/api/admin/config')
def config(u=Depends(admin)):
    urls=airtable_urls()
    try: limit=int(get_setting('max_messages_per_user_per_day','50'))
    except Exception: limit=50
    return {'airtable_urls':urls,'airtable_url':'\n'.join(urls),'airtable_configured':bool(urls),'max_messages_per_user_per_day':limit}
@app.put('/api/admin/config')
def set_config(x:AirtableConfig,u=Depends(admin)):
    urls=list(dict.fromkeys(v.strip() for v in x.urls if v.strip()))
    for url in urls:
        if not url.startswith(('https://airtable.com/','https://www.airtable.com/')): raise HTTPException(400,'Every Airtable URL must start with https://airtable.com/ or https://www.airtable.com/.')
    if not 1<=x.max_messages_per_user_per_day<=5000: raise HTTPException(400,'Daily HR message limit must be between 1 and 5000.')
    c=db()
    c.execute('INSERT INTO settings(key,value) VALUES(\'airtable_urls\',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(json.dumps(urls),))
    c.execute('INSERT INTO settings(key,value) VALUES(\'max_messages_per_user_per_day\',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(str(x.max_messages_per_user_per_day),))
    c.commit(); c.close(); clear_candidate_cache(); return {'ok':True,'airtable_urls':urls,'max_messages_per_user_per_day':x.max_messages_per_user_per_day}
@app.get('/api/admin/users')
def users(u=Depends(admin)):
    c=db(); rows=c.execute("SELECT id,username,role,active,created_at FROM users WHERE role='hr' ORDER BY id DESC").fetchall(); c.close(); return [dict(r) for r in rows]
@app.post('/api/admin/users')
def create_user(x:UserCreate,u=Depends(admin)):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}',x.username): raise HTTPException(400,'Username must be 3–40 characters (letters, numbers, dot, underscore or hyphen).')
    c=db()
    try: c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'hr')",(x.username,phash(x.password))); c.commit()
    except (sqlite3.IntegrityError, PostgresIntegrityError): c.close(); raise HTTPException(409,'Username already exists')
    c.close(); return {'ok':True}
@app.patch('/api/admin/users/{uid}')
def update_user(uid:int,x:UserUpdate,u=Depends(admin)):
    c=db(); r=c.execute("SELECT id FROM users WHERE id=? AND role='hr'",(uid,)).fetchone()
    if not r: c.close(); raise HTTPException(404,'User not found')
    if x.password is not None:
        if len(x.password)<8: c.close(); raise HTTPException(400,'Password must be at least 8 characters')
        c.execute('UPDATE users SET password_hash=? WHERE id=?',(phash(x.password),uid))
    if x.active is not None:c.execute('UPDATE users SET active=? WHERE id=?',(int(x.active),uid))
    c.commit(); c.close(); return {'ok':True}
@app.get('/api/candidates')
def candidates(
    specialization: str = '',
    state: str = '',
    city: str = '',
    spec: Optional[str] = None,
    refresh: bool = False,
    u=Depends(current_user),
):
    # `spec` is accepted as a backward-compatible alias for older dashboards.
    specialization = (specialization or spec or '').strip()

    # Build dropdown options from the complete source list BEFORE filtering.
    # This prevents a selected filter from removing other available options.
    all_data = [row_for(r) for r in records(force_refresh=refresh)]
    specs = sorted({v for x in all_data for v in x['specialization']}, key=str.casefold)
    states = sorted({v for x in all_data for v in x['state']}, key=str.casefold)
    cities = sorted({v for x in all_data for v in x['city']}, key=str.casefold)

    filtered = all_data
    if specialization:
        wanted = norm(specialization)
        filtered = [
            x for x in filtered
            if any(norm(v) == wanted for v in x['specialization'])
        ]
    if state:
        wanted = norm(state)
        filtered = [x for x in filtered if any(norm(v) == wanted for v in x['state'])]
    if city:
        wanted = norm(city)
        filtered = [x for x in filtered if any(norm(v) == wanted for v in x['city'])]

    safe_candidates = [
        {k: v for k, v in x.items() if k not in ('raw', 'phone')}
        for x in filtered
    ]
    return {
        'options': {'specializations': specs, 'states': states, 'cities': cities},
        'candidates': safe_candidates,
        'total_candidates': len(safe_candidates),
    }
@app.get('/api/templates')
def templates(u=Depends(current_user)):
    c=db(); rows=c.execute('SELECT * FROM templates WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/api/templates')
def save_template(x:TemplateIn,u=Depends(current_user)):
    if x.kind not in ('approved','custom'): raise HTTPException(400,'Template kind must be approved or custom')
    if not x.name.strip() or not x.content.strip(): raise HTTPException(400,'Template name and content are required')
    if len(x.content.strip())>1024: raise HTTPException(400,'WhatsApp template body must be 1024 characters or fewer')
    if x.language.strip() not in ('en','hi','en_US','hi_IN'): raise HTTPException(400,'Use a supported language code such as en or hi')
    if x.category.upper() not in ('MARKETING','UTILITY'): raise HTTPException(400,'Category must be MARKETING or UTILITY')
    c=db(); c.execute('INSERT INTO templates(user_id,name,kind,content,template_name,language,category,approval_status,header_text,footer_text) VALUES(?,?,?,?,?,?,?,?,?,?)',(u['id'],x.name.strip(),x.kind,x.content.strip(),x.template_name.strip(),x.language.strip(),x.category.upper(),'DRAFT',x.header_text.strip(),x.footer_text.strip())); c.commit(); c.close(); return {'ok':True}


def doubletick_headers():
    key=os.getenv('DOUBLETICK_API_KEY','').strip()
    if not key: raise HTTPException(503,'DOUBLETICK_API_KEY is not configured on the backend.')
    return {'Authorization':key,'Accept':'application/json','Content-Type':'application/json'}

def dt_sender_number():
    raw=os.getenv('DOUBLETICK_WABA_NUMBER','').strip() or os.getenv('DOUBLETICK_SENDER_NUMBER','').strip()
    digits=re.sub(r'\D','',raw)
    if not digits: raise HTTPException(503,'Set DOUBLETICK_WABA_NUMBER (or DOUBLETICK_SENDER_NUMBER) in backend environment variables.')
    return digits

def dt_error(resp):
    try: body=resp.json()
    except Exception: body={'raw':resp.text[:1000]}
    return body

@app.post('/api/templates/{tid}/submit')
def submit_template(tid:int,u=Depends(current_user)):
    c=db(); row=c.execute('SELECT * FROM templates WHERE id=? AND user_id=?',(tid,u['id'])).fetchone()
    if not row: c.close(); raise HTTPException(404,'Template draft not found')
    if row['approval_status'] in ('PENDING','APPROVED'):
        c.close(); raise HTTPException(409,f"Template is already {row['approval_status']}")
    name=(row['template_name'] or row['name']).strip().lower()
    name=re.sub(r'[^a-z0-9_]+','_',name).strip('_')
    if not name: c.close(); raise HTTPException(400,'Template name must contain English letters or numbers')
    if len(row['content'])>1024: c.close(); raise HTTPException(400,'Body must be 1024 characters or fewer')
    if row['header_text'] and len(row['header_text'])>60: c.close(); raise HTTPException(400,'Header must be 60 characters or fewer')
    if row['footer_text'] and len(row['footer_text'])>60: c.close(); raise HTTPException(400,'Footer must be 60 characters or fewer')
    components={'body':{'text':row['content']}}
    if row['header_text']: components['header']={'format':'TEXT','text':row['header_text']}
    if row['footer_text']: components['footer']={'text':row['footer_text']}
    payload={'name':name,'language':row['language'] or 'en','category':row['category'] or 'MARKETING','allowCategoryUpdate':True,'wabaNumbers':[dt_sender_number()],'components':components}
    try:
        resp=requests.post('https://public.doubletick.io/template',headers=doubletick_headers(),json=payload,timeout=30)
    except requests.RequestException as exc:
        c.close(); raise HTTPException(502,'DoubleTick template submission connection failed: '+str(exc))
    body=dt_error(resp)
    if not resp.ok:
        c.close(); raise HTTPException(resp.status_code,f'DoubleTick template submission failed: {body}')
    c.execute("UPDATE templates SET template_name=?,approval_status='PENDING',provider_detail=?,submitted_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",(name,json.dumps(body,ensure_ascii=False)[:1500],tid,u['id']))
    c.commit(); c.close(); return {'ok':True,'status':'PENDING','template_name':name,'provider_response':body}

def _walk_template_items(value):
    """Normalize the different list wrappers returned by DoubleTick."""
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ('templates', 'data', 'results', 'items', 'result', 'payload'):
            child=value.get(key)
            if isinstance(child, (list, dict)):
                found=_walk_template_items(child)
                if found:
                    return found
        if value.get('name') and value.get('status'):
            return [value]
    return []

@app.post('/api/templates/refresh-status')
def refresh_template_status(u=Depends(current_user)):
    c=db()
    rows=c.execute("SELECT id,template_name,language FROM templates WHERE user_id=? AND COALESCE(template_name,'')<>''",(u['id'],)).fetchall()
    if not rows:
        c.close(); return {'ok':True,'updated':0,'matched':0,'message':'No submitted templates to refresh.'}
    try:
        resp=requests.get('https://public.doubletick.io/v2/templates',headers=doubletick_headers(),params={'status':'ALL','allWabaPhoneNumbers':'true'},timeout=30)
    except requests.RequestException as exc:
        c.close(); raise HTTPException(502,'Could not fetch DoubleTick template status: '+str(exc))
    body=dt_error(resp)
    if not resp.ok:
        c.close(); raise HTTPException(resp.status_code,f'DoubleTick template status failed: {body}')
    items=_walk_template_items(body)
    updated=0; matched=0; status_summary={}
    for row in rows:
        tname=str(row['template_name'] or '').strip().casefold()
        tlang=str(row['language'] or 'en').strip().casefold()
        named=[x for x in items if str(x.get('name') or x.get('templateName') or '').strip().casefold()==tname]
        match=next((x for x in named if str(x.get('language') or x.get('templateLanguage') or 'en').strip().casefold()==tlang),None)
        # Some DoubleTick accounts return locale-qualified codes (e.g. en_US) while
        # older saved drafts contain the base code (en). Only use this fallback when unique.
        if not match:
            same_family=[x for x in named if str(x.get('language') or x.get('templateLanguage') or 'en').strip().casefold().split('_')[0].split('-')[0] == tlang.split('_')[0].split('-')[0]]
            if len(same_family)==1: match=same_family[0]
        if match:
            matched+=1
            status=str(match.get('status') or match.get('approvalStatus') or match.get('newStatus') or '').strip().upper()
            if status in ('APPROVED','REJECTED','PENDING','PAUSED'):
                c.execute('UPDATE templates SET approval_status=?,provider_detail=? WHERE id=?',(status,json.dumps(match,ensure_ascii=False)[:1500],row['id']))
                updated+=1; status_summary[status]=status_summary.get(status,0)+1
    c.commit(); c.close()
    return {'ok':True,'updated':updated,'matched':matched,'statuses':status_summary,
            'message':f'DoubleTick returned {len(items)} template(s); matched {matched} of your {len(rows)} saved template(s).'}

@app.delete('/api/templates/{tid}')
def delete_template(tid:int,u=Depends(current_user)):
    c=db(); c.execute('DELETE FROM templates WHERE id=? AND user_id=?',(tid,u['id'])); c.commit(); c.close(); return {'ok':True}
def _find_provider_message_id(value):
    """Find a DoubleTick message ID in proxy response objects."""
    if isinstance(value, dict):
        for key in ('messageId','dtMessageId','message_id','id'):
            candidate=value.get(key)
            if isinstance(candidate,(str,int)) and str(candidate).strip():
                # Avoid mistaking generic template/customer IDs for message IDs.
                if key != 'id' or any(k in value for k in ('status','to','recipient','message')):
                    return str(candidate).strip()
        for child in value.values():
            found=_find_provider_message_id(child)
            if found: return found
    elif isinstance(value, list):
        for child in value:
            found=_find_provider_message_id(child)
            if found: return found
    return ''

def _find_provider_status(value):
    if isinstance(value, dict):
        status=str(value.get('status') or value.get('messageStatus') or '').strip().upper()
        if status in ('FAILED','ENQUEUED','PROCESSING','SENT','DELIVERED','READ'):
            return status, str(value.get('errorMessage') or value.get('failMessage') or value.get('error') or '')
        for child in value.values():
            found=_find_provider_status(child)
            if found[0]: return found
    elif isinstance(value, list):
        for child in value:
            found=_find_provider_status(child)
            if found[0]: return found
    return '', ''

@app.post('/api/webhooks/doubletick')
async def doubletick_webhook(request:Request, x_webhook_secret:Optional[str]=Header(None)):
    """Receive DoubleTick TEMPLATE_UPDATE and MESSAGE_STATUS_UPDATE events."""
    expected=os.getenv('DOUBLETICK_WEBHOOK_SECRET','').strip()
    if not expected:
        raise HTTPException(503,'DOUBLETICK_WEBHOOK_SECRET is not configured.')
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret,expected):
        raise HTTPException(401,'Invalid webhook secret')
    try: payload=await request.json()
    except Exception: raise HTTPException(400,'Webhook body must be valid JSON')
    if not isinstance(payload,dict): raise HTTPException(400,'Webhook payload must be a JSON object')
    event=str(payload.get('event') or payload.get('eventType') or payload.get('type') or '').upper()
    # DoubleTick's documented sample body omits an explicit event field;
    # infer the event from its distinctive payload shape when necessary.
    if not event and isinstance(payload.get('message'),dict) and payload.get('from') and payload.get('to'):
        event='MESSAGE_RECEIVED'
    if not event and payload.get('status') and payload.get('to'):
        event='MESSAGE_STATUS_UPDATE'
    if event == 'MESSAGE_RECEIVED':
        data=payload.get('data') if isinstance(payload.get('data'),dict) else payload.get('payload') if isinstance(payload.get('payload'),dict) else payload
        msg=data.get('message') if isinstance(data.get('message'),dict) else {}
        phone=re.sub(r'\D','',str(data.get('from') or data.get('customerPhone') or data.get('sender') or ''))
        if not phone: raise HTTPException(400,'Incoming message is missing sender phone number')
        contact=data.get('contact') if isinstance(data.get('contact'),dict) else {}
        name=str(contact.get('name') or data.get('customerName') or data.get('name') or phone)[:200]
        mtype=str(msg.get('type') or data.get('messageType') or 'TEXT').upper()
        text_value=msg.get('text') or msg.get('caption') or msg.get('body') or msg.get('title') or ''
        if isinstance(text_value,(dict,list)): text_value=json.dumps(text_value,ensure_ascii=False)
        media_url=str(msg.get('url') or msg.get('mediaUrl') or msg.get('media_url') or '')
        if not text_value and media_url: text_value=f'[{mtype}]'
        if not text_value: text_value=json.dumps(msg or data,ensure_ascii=False)[:4000]
        message_id=str(data.get('dtMessageId') or data.get('messageId') or '')
        c=db()
        if DATABASE_URL:
            owner=c.execute("SELECT user_id,candidate_name,phone FROM logs WHERE RIGHT(REGEXP_REPLACE(COALESCE(phone,''),'[^0-9]','','g'),10)=? ORDER BY id DESC LIMIT 1",(phone[-10:],)).fetchone()
        else:
            owner=c.execute("SELECT user_id,candidate_name,phone FROM logs WHERE substr(replace(replace(replace(phone,'+',''),' ',''),'-',''),-10)=? ORDER BY id DESC LIMIT 1",(phone[-10:],)).fetchone()
        uid=owner['user_id'] if owner else None
        canonical=re.sub(r'\D','',str(owner['phone'] or '')) if owner and owner['phone'] else phone
        if owner and owner['candidate_name']: name=str(owner['candidate_name'])
        duplicate=None
        if message_id: duplicate=c.execute("SELECT id FROM chat_messages WHERE provider_message_id=? AND provider_message_id<>'' LIMIT 1",(message_id,)).fetchone()
        if not duplicate:
            c.execute('INSERT INTO chat_messages(user_id,phone,candidate_name,direction,message_type,content,media_url,provider_message_id,status) VALUES(?,?,?,?,?,?,?,?,?)',
                      (uid,canonical,name,'IN',mtype,str(text_value)[:5000],media_url,message_id,'RECEIVED'))
        c.commit(); c.close()
        return {'ok':True,'event':'MESSAGE_RECEIVED','saved':not bool(duplicate),'assigned':bool(uid),'matched_phone':bool(owner)}

    # Template status notifications use TEMPLATE_STATUS_UPDATE as the payload event name.
    if event in ('TEMPLATE_UPDATE','TEMPLATE_STATUS_UPDATE') or payload.get('newStatus'):
        name=str(payload.get('templateName') or payload.get('name') or '').strip()
        language=str(payload.get('templateLanguage') or payload.get('language') or 'en').strip()
        status=str(payload.get('newStatus') or payload.get('status') or '').strip().upper()
        if not name or status not in ('APPROVED','REJECTED','PENDING','PAUSED'):
            raise HTTPException(400,'Template webhook is missing templateName or a valid newStatus')
        c=db()
        c.execute('UPDATE templates SET approval_status=?,provider_detail=? WHERE LOWER(template_name)=LOWER(?) AND LOWER(language)=LOWER(?)',
                  (status,json.dumps(payload,ensure_ascii=False)[:1500],name,language))
        c.commit(); c.close()
        return {'ok':True,'event':'TEMPLATE_STATUS_UPDATE','template_name':name,'language':language,'status':status}

    if event in ('MESSAGE_STATUS_UPDATE','MESSAGE_STATUS',''):
        message_id=str(payload.get('messageId') or payload.get('dtMessageId') or '').strip()
        status=str(payload.get('status') or '').strip().upper()
        if status not in ('SENT','DELIVERED','READ','FAILED'):
            raise HTTPException(400,'Unsupported or missing message status')
        recipient=re.sub(r'\D','',str(payload.get('to') or payload.get('recipient') or ''))
        detail=str(payload.get('failMessage') or payload.get('errorMessage') or f'DoubleTick status update: {status}')[:1500]
        c=db(); target=None
        if message_id:
            target=c.execute('SELECT id,status FROM logs WHERE provider_message_id=? ORDER BY id DESC LIMIT 1',(message_id,)).fetchone()
        # Compatibility fallback for old proxy responses that did not return a message ID.
        if not target and recipient:
            candidates=c.execute("SELECT id,phone,status FROM logs WHERE status IN ('PROCESSING','ENQUEUED','SENT','UNKNOWN') ORDER BY id DESC LIMIT 200").fetchall()
            for candidate in candidates:
                digits=re.sub(r'\D','',str(candidate['phone'] or ''))
                if digits and digits==recipient:
                    target=candidate; break
        if target:
            c.execute('UPDATE logs SET status=?,detail=?,status_updated_at=CURRENT_TIMESTAMP WHERE id=?', (status,detail,target['id']))
        if message_id: c.execute('UPDATE chat_messages SET status=? WHERE provider_message_id=?',(status,message_id))
        c.commit(); c.close()
        return {'ok':True,'event':'MESSAGE_STATUS_UPDATE','matched':bool(target),'status':status,'message_id':message_id}
    return {'ok':True,'ignored':True,'event':event}

@app.post('/api/send')
def send(x:SendIn,u=Depends(current_user)):
    if not x.candidate_ids: raise HTTPException(400,'Select at least one candidate')
    if x.kind not in ('approved','custom'): raise HTTPException(400,'Invalid message type')
    if x.kind=='custom':
        raise HTTPException(501,'Custom message sending is not supported by the supplied proxy endpoint. Outside the 24-hour window, use an approved WhatsApp template.')
    if not x.template_name.strip(): raise HTTPException(400,'Approved DoubleTick template name is required')
    all_rows=records(); selected=set(x.candidate_ids); found=[]
    for r in all_rows:
        rr=row_for(r)
        if rr['id'] in selected: found.append(r)
    if len(found)!=len(selected): raise HTTPException(400,'Some selected candidates are no longer in the configured Airtable views. Refresh the list.')
    c=db()
    try:
        limit=int(get_setting('max_messages_per_user_per_day','50'))
        if u['role']!='admin':
            # Count every attempt (including failed sends) to prevent retries bypassing the cap.
            used=c.execute("SELECT COUNT(*) n FROM logs WHERE user_id=? AND date(created_at,'+5 hours','+30 minutes')=date('now','+5 hours','+30 minutes')",(u['id'],)).fetchone()['n']
            remaining=max(0,limit-used)
            if len(found)>remaining:
                c.close(); raise HTTPException(429,f'Daily WhatsApp limit reached. You can attempt {remaining} more message(s) today; your limit is {limit} per day.')
        # Reserve all attempts in one write transaction so simultaneous requests cannot exceed the cap.
        c.execute('BEGIN IMMEDIATE')
        if DATABASE_URL and u['role']!='admin':
            c.execute('SELECT pg_advisory_xact_lock(?)',(int(u['id']),))
        if u['role']!='admin':
            used=c.execute("SELECT COUNT(*) n FROM logs WHERE user_id=? AND date(created_at,'+5 hours','+30 minutes')=date('now','+5 hours','+30 minutes')",(u['id'],)).fetchone()['n']
            remaining=max(0,limit-used)
            if len(found)>remaining:
                c.rollback(); c.close(); raise HTTPException(429,f'Daily WhatsApp limit reached. You can attempt {remaining} more message(s) today; your limit is {limit} per day.')
        ids=[]
        for r in found:
            f=candidate_fields(r)
            cur=c.execute("INSERT INTO logs(user_id,candidate_name,phone,kind,template_name,status,detail) VALUES(?,?,?,?,?,'PROCESSING','Reserved for sending')",(u['id'],str(f.get('name') or ''),str(f.get('phone') or ''),'approved',x.template_name.strip()))
            ids.append(cur.lastrowid)
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.close(); raise
    results=[]
    for r,log_id in zip(found,ids):
        f=candidate_fields(r); status='FAILED'; detail=''; provider_id=''
        try:
            response=send_doubletick_candidate_message(r,x.template_name.strip(),x.language.strip() or 'en')
            provider_id=_find_provider_message_id(response)
            provider_status,provider_error=_find_provider_status(response)
            if provider_status=='FAILED':
                status='FAILED'; detail=provider_error or json.dumps(response,ensure_ascii=False)[:1500]
            elif provider_status in ('SENT','DELIVERED','READ','ENQUEUED'):
                status=provider_status; detail=json.dumps(response,ensure_ascii=False)[:1500]
            else:
                # HTTP success from the proxy means accepted, not necessarily delivered.
                status='ENQUEUED'; detail=json.dumps(response,ensure_ascii=False)[:1500]
        except Exception as e:
            detail=str(e)[:1500]
        # Commit each candidate immediately. A later timeout/restart must not roll back earlier results.
        current=c.execute('SELECT status FROM logs WHERE id=?',(log_id,)).fetchone()
        current_status=str(current['status'] or '') if current else ''
        final_states=('SENT','DELIVERED','READ','FAILED')
        if current_status not in final_states:
            c.execute('UPDATE logs SET status=?,detail=?,provider_message_id=?,status_updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,detail,provider_id,log_id))
        elif provider_id:
            c.execute('UPDATE logs SET provider_message_id=? WHERE id=?',(provider_id,log_id))
        c.commit()
        results.append({'name':str(f.get('name') or ''),'status':current_status if current_status in final_states else status,'detail':detail,'message_id':provider_id})
    c.close()
    return {'ok':True,'summary':{'total':len(results),'sent':sum(z['status'] in ('SENT','DELIVERED','READ','ENQUEUED') for z in results),'failed':sum(z['status']=='FAILED' for z in results),'enqueued':sum(z['status']=='ENQUEUED' for z in results)},'results':results}
def _e164(value):
    digits=re.sub(r'\D','',str(value or ''))
    if len(digits)==10: digits='91'+digits
    return '+'+digits if 10<=len(digits)<=15 else ''

@app.post('/api/chats/{phone}/reply')
def send_chat_reply(phone:str, x:ChatReplyIn, u=Depends(current_user)):
    """Send a free-form WhatsApp reply through DoubleTick (24-hour session only)."""
    digits=re.sub(r'\D','',phone)
    if not digits: raise HTTPException(400,'Invalid phone number')
    api_key=os.getenv('DOUBLETICK_API_KEY','').strip()
    sender=_e164(os.getenv('DOUBLETICK_SENDER_NUMBER') or os.getenv('DOUBLETICK_WABA_NUMBER') or '')
    recipient=_e164(digits)
    if not api_key: raise HTTPException(503,'DOUBLETICK_API_KEY is not configured on the server.')
    if not sender: raise HTTPException(503,'Set DOUBLETICK_SENDER_NUMBER (your WhatsApp sender number) in Render.')
    if not recipient: raise HTTPException(400,'Candidate phone number must be a valid international number.')
    c=db()
    if u['role']!='admin':
        if DATABASE_URL:
            allowed=c.execute("SELECT 1 FROM logs WHERE user_id=? AND RIGHT(REGEXP_REPLACE(COALESCE(phone,''),'[^0-9]','','g'),10)=? LIMIT 1",(u['id'],digits[-10:])).fetchone()
        else:
            allowed=c.execute("SELECT 1 FROM logs WHERE user_id=? AND substr(replace(replace(replace(phone,'+',''),' ',''),'-',''),-10)=? LIMIT 1",(u['id'],digits[-10:])).fetchone()
        if not allowed:
            c.close(); raise HTTPException(403,'This conversation is not assigned to your HR account.')
    msg_id=str(uuid.uuid4())
    payload={'to':recipient,'from':sender,'messageId':msg_id,'content':{'text':x.message.strip()}}
    try:
        resp=requests.post('https://public.doubletick.io/whatsapp/message/text',
                           headers={'Authorization':api_key,'Content-Type':'application/json','Accept':'application/json'},
                           json=payload,timeout=30)
        try: body=resp.json()
        except Exception: body={'raw':resp.text[:1500]}
    except requests.RequestException as exc:
        c.close(); raise HTTPException(502,f'DoubleTick connection failed: {exc}')
    if resp.status_code not in (200,201,202):
        c.close()
        detail=body.get('message') or body.get('error') or body
        raise HTTPException(resp.status_code if 400<=resp.status_code<500 else 502,
            f'DoubleTick rejected reply. Free-form replies require an active 24-hour customer service window. Provider response: {detail}')
    provider_id=_find_provider_message_id(body) or msg_id
    if DATABASE_URL:
        name_row=c.execute("SELECT candidate_name FROM logs WHERE RIGHT(REGEXP_REPLACE(COALESCE(phone,''),'[^0-9]','','g'),10)=? ORDER BY id DESC LIMIT 1",(digits[-10:],)).fetchone()
    else:
        name_row=c.execute("SELECT candidate_name FROM logs WHERE substr(replace(replace(replace(phone,'+',''),' ',''),'-',''),-10)=? ORDER BY id DESC LIMIT 1",(digits[-10:],)).fetchone()
    candidate_name=name_row['candidate_name'] if name_row else ''
    c.execute("INSERT INTO chat_messages(user_id,phone,candidate_name,direction,message_type,content,media_url,provider_message_id,status) VALUES(?,?,?,?,?,?,?,?,?)",
              (u['id'],digits,candidate_name,'OUT','TEXT',x.message.strip(),'',provider_id,'SENT'))
    c.commit(); c.close()
    return {'ok':True,'status':'SENT','message_id':provider_id,'provider':body}

@app.get('/api/chats')
def chats(u=Depends(current_user)):
    c=db()
    if u['role']=='admin':
        rows=c.execute("SELECT phone,MAX(candidate_name) candidate_name,MAX(created_at) last_at,COUNT(*) message_count FROM chat_messages GROUP BY phone ORDER BY last_at DESC LIMIT 500").fetchall()
        sent=c.execute("SELECT phone,MAX(candidate_name) candidate_name,MAX(created_at) last_at,COUNT(*) message_count FROM logs GROUP BY phone ORDER BY last_at DESC LIMIT 500").fetchall()
    else:
        rows=c.execute("SELECT phone,MAX(candidate_name) candidate_name,MAX(created_at) last_at,COUNT(*) message_count FROM chat_messages WHERE user_id=? GROUP BY phone ORDER BY last_at DESC LIMIT 300",(u['id'],)).fetchall()
        sent=c.execute("SELECT phone,MAX(candidate_name) candidate_name,MAX(created_at) last_at,COUNT(*) message_count FROM logs WHERE user_id=? GROUP BY phone ORDER BY last_at DESC LIMIT 300",(u['id'],)).fetchall()
    merged={re.sub(r'\D','',str(r['phone'] or '')):dict(r) for r in rows}
    for r in sent:
        key=re.sub(r'\D','',str(r['phone'] or ''))
        if key: merged[key]=dict(r) if key not in merged else {**merged[key], 'last_at':max(str(merged[key].get('last_at') or ''),str(r['last_at'] or ''))}
    c.close(); return sorted(merged.values(),key=lambda x:str(x.get('last_at') or ''),reverse=True)

@app.get('/api/chats/{phone}')
def chat_detail(phone:str,u=Depends(current_user)):
    digits=re.sub(r'\D','',phone)
    if not digits: raise HTTPException(400,'Invalid phone number')
    c=db()
    if u['role']=='admin':
        incoming=c.execute('SELECT id,user_id,phone,candidate_name,direction,message_type,content,media_url,provider_message_id,status,created_at FROM chat_messages WHERE phone=? ORDER BY id ASC LIMIT 1000',(digits,)).fetchall()
        outgoing=c.execute("SELECT id,user_id,phone,candidate_name,'OUT' direction,kind message_type,template_name content,'' media_url,provider_message_id,created_at,status FROM logs WHERE REPLACE(REPLACE(REPLACE(phone,'+',''),' ',''),'-','') LIKE ? ORDER BY id ASC LIMIT 1000",('%'+digits[-10:],)).fetchall()
    else:
        incoming=c.execute('SELECT id,user_id,phone,candidate_name,direction,message_type,content,media_url,provider_message_id,status,created_at FROM chat_messages WHERE phone=? AND user_id=? ORDER BY id ASC LIMIT 1000',(digits,u['id'])).fetchall()
        outgoing=c.execute("SELECT id,user_id,phone,candidate_name,'OUT' direction,kind message_type,template_name content,'' media_url,provider_message_id,created_at,status FROM logs WHERE user_id=? AND REPLACE(REPLACE(REPLACE(phone,'+',''),' ',''),'-','') LIKE ? ORDER BY id ASC LIMIT 1000",(u['id'],'%'+digits[-10:])).fetchall()
    items=[dict(x) for x in incoming]+[dict(x) for x in outgoing]
    items.sort(key=lambda x:str(x.get('created_at') or ''))
    c.close(); return items

@app.get('/api/history')
def history(u=Depends(current_user)):
    c=db()
    # Mark abandoned reservations so they do not remain stuck as PROCESSING forever.
    if DATABASE_URL:
        c.execute("UPDATE logs SET status='UNKNOWN',detail='Previous send was interrupted before a provider result was saved. Check DoubleTick before retrying to avoid duplicate messages.',status_updated_at=CURRENT_TIMESTAMP WHERE status='PROCESSING' AND created_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'")
    else:
        c.execute("UPDATE logs SET status='UNKNOWN',detail='Previous send was interrupted before a provider result was saved. Check DoubleTick before retrying to avoid duplicate messages.',status_updated_at=CURRENT_TIMESTAMP WHERE status='PROCESSING' AND datetime(created_at) < datetime('now','-15 minutes')")
    if u['role']=='admin': rows=c.execute('SELECT l.*,u.username FROM logs l JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 500').fetchall()
    else: rows=c.execute('SELECT id,user_id,candidate_name,kind,template_name,status,detail,created_at,provider_message_id,status_updated_at FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 300',(u['id'],)).fetchall()
    c.commit(); c.close(); return [dict(r) for r in rows]
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
