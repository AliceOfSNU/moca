#!/usr/bin/env python3
"""Local-first, stdlib-only MOCA signup. See README before deployment."""
import argparse, contextlib, fcntl, hashlib, json, os, re, tempfile, threading, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STATIC = {'/': ('index.html','text/html; charset=utf-8'), '/index.html': ('index.html','text/html; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/banner.png': ('banner.png','image/png')}
DEFAULT = dict(title='Dots로 레스토랑 경영하기', date='10월 10일 (토) 오전 11시~오후 1시', venue='당산역 인근 · [미정: 상세 장소]', address='', capacity=8, fee='6,000원', signup_deadline='[미정: 신청 마감]', checkin_deadline='[미정: 체크인 마감]', fee_details='[미정: 결제 방법·환불 기준]', site_url='[미정: 웹사이트 주소]')
LOCK = threading.RLock()
class Invalid(Exception):
    def __init__(self, status, message): self.status, self.message = status, message

def normalized(s): return unicodedata.normalize('NFKC', s).casefold().strip()
def stamp(): return datetime.now(timezone.utc).isoformat()
def clean(d, key, maximum=300, required=True):
    v = d.get(key, '')
    if not isinstance(v, str) or len(v)>maximum or (required and not v.strip()) or any(ord(c)<32 and c not in '\n\t' for c in v):
        raise Invalid(400, '답변의 길이와 필수 항목을 확인해 주세요.')
    return v.strip()
def token(d):
    v = d.get('key','')
    if not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v): raise Invalid(401,'이 브라우저의 신청 열쇠나 저장해 둔 복구 열쇠가 필요해요.')
    return hashlib.sha256(v.encode()).hexdigest()
@contextlib.contextmanager
def records():
    with LOCK, open(DATA.with_suffix('.lock'), 'a') as lock:
        os.chmod(DATA.with_suffix('.lock'),0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        content = json.loads(DATA.read_text()) if DATA.exists() else {'version':1,'registrations':[]}
        yield content

def save(content):
    fd, name = tempfile.mkstemp(dir=DATA.parent, prefix='.moca-', suffix='.tmp')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(content,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(name,DATA)
    finally:
        if os.path.exists(name): os.unlink(name)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass  # Never log keys, names, answers or request bodies.
    def setup(self):
        super().setup(); self.connection.settimeout(10)
    def response(self,status,payload,kind='application/json; charset=utf-8'):
        data = json.dumps(payload,ensure_ascii=False).encode() if isinstance(payload,(dict,list)) else payload
        self.send_response(status)
        for k,v in {'Content-Type':kind,'Content-Length':str(len(data)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def valid_host(self):
        if self.headers.get('Host') not in HOSTS: raise Invalid(403,'이 주소로는 요청할 수 없어요.')
    def do_GET(self):
        try:
            self.valid_host()
            path=urlsplit(self.path).path
            if path == '/api/public':
                with records() as data:
                    people=[{'name':r['name'], **({'occupation':r['occupation']} if r['share_occupation'] else {})} for r in data['registrations'] if r['share_name']]
                    self.response(200,{'config':CONFIG,'count':len(data['registrations']),'participants':people})
                return
            if path not in STATIC: raise Invalid(404,'페이지를 찾지 못했어요.')
            filename,kind=STATIC[path]; p=ROOT/filename
            if p.is_symlink() or not p.is_file() or p.resolve().parent != ROOT: raise Invalid(404,'파일을 찾지 못했어요.')
            self.response(200,p.read_bytes(),kind)
        except Invalid as e: self.response(e.status,{'error':e.message})
        except Exception: self.response(500,{'error':'지금은 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요.'})
    def do_POST(self):
        try:
            self.valid_host()
            origin=self.headers.get('Origin')
            if origin and origin not in ORIGINS: raise Invalid(403,'같은 사이트에서 다시 시도해 주세요.')
            if self.headers.get('Sec-Fetch-Site') not in (None,'same-origin','none'): raise Invalid(403,'같은 사이트에서 다시 시도해 주세요.')
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json' or self.headers.get('X-Moca-Request') != '1': raise Invalid(415,'요청 형식을 확인해 주세요.')
            if self.headers.get('Transfer-Encoding'): raise Invalid(400,'요청 형식을 확인해 주세요.')
            try: length=int(self.headers.get('Content-Length','0'))
            except ValueError: raise Invalid(400,'요청 형식을 확인해 주세요.')
            if not 0 < length <= 8192: raise Invalid(413,'답변이 너무 길거나 비어 있어요.')
            try: d=json.loads(self.rfile.read(length))
            except (ValueError,UnicodeError): raise Invalid(400,'답변 형식을 확인해 주세요.')
            if not isinstance(d,dict): raise Invalid(400,'답변 형식을 확인해 주세요.')
            hashed=token(d)
            with records() as data:
                rows=data['registrations']; r=next((x for x in rows if x['key_hash']==hashed),None)
                path=urlsplit(self.path).path
                if path == '/api/me':
                    if r is None: raise Invalid(404,'저장된 신청을 찾지 못했어요. 신청한 브라우저나 복구 열쇠를 확인해 주세요.')
                elif path == '/api/signup':
                    name=clean(d,'name',40); occupation=clean(d,'occupation',80)
                    access=clean(d,'access',30); prep=clean(d,'preparation',30)
                    if access not in ('ready','checking','blocked') or prep not in ('yes','checking'): raise Invalid(400,'준비 항목을 다시 골라 주세요.')
                    for field in ('share_name','share_occupation'):
                        if type(d.get(field)) is not bool: raise Invalid(400,'공개 여부를 골라 주세요.')
                    if d['share_occupation'] and not d['share_name']: raise Invalid(400,'이름을 공개할 때만 하는 일을 함께 공개할 수 있어요.')
                    if any(normalized(x['name'])==normalized(name) and x is not r for x in rows): raise Invalid(409,'이 이름으로는 저장할 수 없어요. 본인의 기존 신청이면 복구 열쇠를 사용하고, 처음 신청한다면 구별되는 별명을 써 주세요.')
                    if r is None and len(rows)>=CONFIG['capacity']: raise Invalid(409,'지금은 정원이 찼어요. 기존 신청 수정은 가능해요.')
                    roadblocks=clean(d,'roadblocks',500,False)
                    if r is None: r={'key_hash':hashed,'created_at':stamp(),'checkin':None}; rows.append(r)
                    r.update(name=name,occupation=occupation,access=access,preparation=prep,roadblocks=roadblocks,share_name=d['share_name'],share_occupation=d['share_occupation'],updated_at=stamp())
                    save(data)
                elif path == '/api/checkin':
                    if r is None: raise Invalid(404,'먼저 신청한 브라우저나 복구 열쇠로 신청을 불러와 주세요.')
                    state=clean(d,'state',30)
                    if state not in ('done','partial','not_started'): raise Invalid(400,'준비 상태를 골라 주세요.')
                    r['checkin']={'state':state,'roadblocks':clean(d,'roadblocks',500,False),'updated_at':stamp()}; save(data)
                else: raise Invalid(404,'요청을 찾지 못했어요.')
                self.response(200,{'registration':{k:v for k,v in r.items() if k!='key_hash'}})
        except Invalid as e: self.response(e.status,{'error':e.message})
        except Exception: self.response(500,{'error':'저장하지 못했어요. 답변을 유지하고 있으니 잠시 뒤 다시 시도해 주세요.'})

def main():
    global DATA,CONFIG,HOSTS,ORIGIN,ORIGINS
    ap=argparse.ArgumentParser(); ap.add_argument('--data',required=True,help='Private directory outside website/'); ap.add_argument('--host',default='127.0.0.1'); ap.add_argument('--port',type=int,default=8000); ap.add_argument('--config',help='Optional private JSON configuration file'); ap.add_argument('--origin',help='Exact external origin for reviewed HTTPS reverse-proxy deployment')
    args=ap.parse_args(); folder=Path(args.data).expanduser().resolve()
    if folder==ROOT or ROOT in folder.parents: ap.error('--data must be outside website/')
    folder.mkdir(mode=0o700,parents=True,exist_ok=True); DATA=folder/'registrations.json'
    if DATA.is_symlink(): ap.error('Data file cannot be a symlink')
    CONFIG=DEFAULT.copy()
    if args.config:
        config=json.loads(Path(args.config).read_text())
        if not isinstance(config,dict) or set(config)-set(DEFAULT): ap.error('Unknown configuration keys')
        CONFIG.update(config)
    if type(CONFIG['capacity']) is not int or not 1<=CONFIG['capacity']<=100: ap.error('capacity must be 1..100')
    if any(not isinstance(v,str) or len(v)>500 for k,v in CONFIG.items() if k!='capacity'): ap.error('Configuration text must be strings up to 500 characters')
    ORIGIN=args.origin or f'http://{args.host}:{args.port}'
    parsed=urlsplit(ORIGIN)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment: ap.error('origin must be scheme://host[:port]')
    HOSTS={parsed.netloc}
    if not args.origin and args.host=='127.0.0.1': HOSTS.add(f'localhost:{args.port}')
    ORIGINS={ORIGIN}
    if not args.origin and args.host=='127.0.0.1': ORIGINS.add(f'http://localhost:{args.port}')
    print(f'Local server: {ORIGIN}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
if __name__=='__main__': main()
