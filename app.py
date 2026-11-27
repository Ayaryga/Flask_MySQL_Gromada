"""Чулаківська громада: Flask + MySQL. Збережено HTML/CSS попередньої курсової."""
import os, re, uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
import mysql.connector
from dotenv import load_dotenv
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, UnidentifiedImageError
from translations import TRANSLATIONS

BASE = Path(__file__).resolve().parent
load_dotenv(BASE/'.env')
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or 'DEV-ONLY-REPLACE-THIS-KEY'
app.config.update(MAX_CONTENT_LENGTH=8*1024*1024, UPLOAD_FOLDER=str(BASE/'static'/'uploads'),SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax')
LANGUAGES=['uk','en','de']; DEFAULT_LANG='uk'

def conn():
    return mysql.connector.connect(host=os.getenv('MYSQL_HOST','127.0.0.1'),port=int(os.getenv('MYSQL_PORT','3306')),user=os.getenv('MYSQL_USER','portal_app'),password=os.getenv('MYSQL_PASSWORD',''),database=os.getenv('MYSQL_DATABASE','chulakivka_portal'))

def fetchall(sql,params=()):
    db=conn()
    try:
        cur=db.cursor(dictionary=True);cur.execute(sql,params);return cur.fetchall()
    finally: db.close()

def fetchone(sql,params=()):
    v=fetchall(sql,params);return v[0] if v else None

def tr(key): return TRANSLATIONS.get(g.lang,TRANSLATIONS['uk']).get(key,TRANSLATIONS['uk'].get(key,key))

def localized(row,field):
    if not row:return ''
    return row.get(field+'_'+g.lang) or row.get(field+'_uk') or row.get(field) or ''
app.jinja_env.globals.update(tr=tr,localized=localized,languages=LANGUAGES)

@app.before_request
def language():
    v=request.view_args or {};lang=v.get('lang') or session.get('lang','uk')
    g.lang=lang if lang in LANGUAGES else 'uk'
    session['lang']=g.lang

def csrf_token():
    if 'csrf' not in session:session['csrf']=uuid.uuid4().hex
    return session['csrf']
app.jinja_env.globals['csrf_token']=csrf_token
@app.before_request
def check_csrf():
    if request.method in ('POST','PUT','PATCH','DELETE') and request.form.get('_csrf')!=session.get('csrf'):
        abort(400,'CSRF token missing or invalid')

def login_required(fn):
    @wraps(fn)
    def wrapped(*args,**kw):
        if not session.get('user_id'):return redirect(url_for('admin_login'))
        return fn(*args,**kw)
    return wrapped

def has_role(*roles):return bool(set(session.get('roles',[])).intersection(roles))

def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args,**kw):
            if not has_role(*roles):abort(403)
            return fn(*args,**kw)
        return wrapped
    return deco

def list_publications(kind='news',only_published=True):
    where="AND p.status='published'" if only_published else ''
    rows=fetchall('''SELECT p.publication_id AS id,p.slug,p.type,p.status,p.created_at,p.published_at,
        t.lang_code,t.title,t.body
        FROM publications p LEFT JOIN publication_translations t
        ON t.publication_id=p.publication_id AND t.lang_code=%s
        WHERE p.type=%s '''+where+' ORDER BY p.created_at DESC',(g.lang,kind))
    image_map={}
    if rows:
        ids=[x['id'] for x in rows]
        placeholders=','.join(['%s']*len(ids))
        images=fetchall("SELECT publication_id,filename FROM media_files WHERE mime_type LIKE 'image/%%' AND publication_id IN ("+placeholders+") ORDER BY uploaded_at,media_id",tuple(ids))
        for photo in images:image_map.setdefault(photo['publication_id'],photo['filename'])
    for row in rows:
        row.update(title=row['title'] or '',short_text=(row['body'] or '')[:170],content=row['body'] or '',category_name=kind,image=image_map.get(row['id']),
                   **{'title_'+g.lang:row['title'] or ''})
        row['created_at']=str(row['published_at'] or row['created_at'])
    return rows

def publication_images(publication_id):
    return fetchall('''SELECT filename FROM media_files
        WHERE publication_id=%s AND mime_type LIKE 'image/%%'
        ORDER BY uploaded_at,media_id''',(publication_id,))

def save_publication_images(publication_id, uploads, db):
    """Записує зображення лише до наявної новини, в межах транзакції виклику."""
    allowed={'JPEG':('.jpg','image/jpeg'),'PNG':('.png','image/png'),
             'GIF':('.gif','image/gif'),'WEBP':('.webp','image/webp')}
    created=[]
    folder=BASE/'static'/'uploads'
    folder.mkdir(parents=True,exist_ok=True)
    try:
        for upload in uploads:
            if not upload or not upload.filename: continue
            try:
                image=Image.open(upload.stream)
                image.verify()
                kind=image.format
                upload.stream.seek(0)
                if kind not in allowed:
                    abort(400,'Підтримуються лише JPEG, PNG, GIF та WebP')
                # Перевіряємо також реальний розмір, а не тільки розширення.
                probe=Image.open(upload.stream)
                if probe.width * probe.height > 25_000_000:
                    abort(400,'Зображення надто велике (до 25 Мп)')
                upload.stream.seek(0)
            except (UnidentifiedImageError, OSError, ValueError):
                abort(400,'Не вдалося прочитати зображення')
            suffix,mime=allowed[kind]
            filename=uuid.uuid4().hex+suffix
            dest=folder/filename
            upload.save(dest)
            created.append(dest)
            size=dest.stat().st_size
            if size > 8*1024*1024:
                abort(400,'Розмір фотографії не повинен перевищувати 8 МБ')
            cur=db.cursor()
            try:
                cur.execute('INSERT INTO media_files(publication_id,filename,mime_type,size_bytes) VALUES(%s,%s,%s,%s)',
                           (publication_id,filename,mime,size))
            finally:cur.close()
    except Exception:
        for file in created:file.unlink(missing_ok=True)
        raise
    return created

def pages():return [dict(slug=x['slug'],title=x['title'],title_uk=x['title']) for x in list_publications('page')]

def events_list(published=True):
    where="AND p.status='published'" if published else ''
    rows=fetchall('''SELECT e.event_id AS id,p.publication_id,p.status,e.starts_at AS event_date,e.ends_at,e.place AS location,
      t.title,t.body AS description
      FROM events e JOIN publications p ON p.publication_id=e.publication_id
      LEFT JOIN publication_translations t ON t.publication_id=p.publication_id AND t.lang_code=%s
      WHERE 1=1 '''+where+' ORDER BY e.starts_at',(g.lang,))
    for x in rows:
        x.update(title_uk=x['title'],location_uk=x['location'],description_uk=x['description']);x['event_date']=str(x['event_date'])
    return rows

def media_list():
    rows=fetchall('''SELECT m.media_id AS id,m.filename AS file_path,m.uploaded_at AS created_at,t.title,
       CASE WHEN m.mime_type LIKE 'image/%%' THEN 'photo' ELSE 'file' END AS media_type,
       NULL AS video_url
       FROM media_files m LEFT JOIN publication_translations t ON t.publication_id=m.publication_id AND t.lang_code=%s
       ORDER BY m.uploaded_at DESC''',(g.lang,))
    for x in rows:x['title_uk']=x['title'];x['created_at']=str(x['created_at'])
    return rows

@app.route('/')
def index():return redirect(url_for('localized_index',lang=g.lang))
@app.route('/<lang>')
def localized_index(lang):
    stats=fetchone("""SELECT
        (SELECT COUNT(*) FROM publications WHERE status='published' AND type IN ('news','page')) AS materials,
        (SELECT COUNT(*) FROM events e JOIN publications p ON p.publication_id=e.publication_id WHERE p.status='published') AS events,
        (SELECT COUNT(*) FROM media_files WHERE mime_type LIKE 'image/%') AS photos
    """)
    stats['languages']=len(LANGUAGES)
    return render_template('index.html',posts=list_publications()[:6],events=events_list()[:3],pages=pages(),stats=stats)
@app.route('/<lang>/news')
def news(lang):return render_template('news.html',posts=list_publications(),pages=pages())
@app.route('/<lang>/post/<int:post_id>')
def post_detail(lang,post_id):
    p=fetchone('''SELECT p.publication_id AS id,p.published_at AS created_at,t.title,t.body AS content
      FROM publications p JOIN publication_translations t ON p.publication_id=t.publication_id
      WHERE p.publication_id=%s AND p.status='published' AND t.lang_code=%s''',(post_id,g.lang))
    if not p:abort(404)
    images=publication_images(post_id)
    p.update(category_name='Новини',image=images[0]['filename'] if images else None,title_uk=p['title'],content_uk=p['content'],created_at=str(p['created_at']))
    return render_template('post_detail.html',post=p,images=images,pages=pages())
@app.route('/<lang>/page/<slug>')
def page_detail(lang,slug):
    p=fetchone('''SELECT p.slug,t.title,t.body AS content FROM publications p JOIN publication_translations t
       ON p.publication_id=t.publication_id WHERE p.slug=%s AND p.type='page' AND p.status='published' AND t.lang_code=%s''',(slug,g.lang))
    if not p:abort(404)
    p.update(title_uk=p['title'],content_uk=p['content'])
    return render_template('page_detail.html',page=p,pages=pages())
@app.route('/<lang>/media')
def media(lang):return render_template('media.html',media_items=media_list(),pages=pages())
@app.route('/<lang>/events')
def events(lang):return render_template('events.html',events=events_list(),pages=pages())

@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    if request.method=='POST':
        email=request.form.get('username','').strip()
        password=request.form.get('password','')
        user=fetchone('SELECT * FROM users WHERE email=%s AND is_active=1',(email,))
        if user and check_password_hash(user['password_hash'],password):
            roles=fetchall('''SELECT r.code FROM roles r JOIN user_roles ur ON ur.role_id=r.role_id WHERE ur.user_id=%s''',(user['user_id'],))
            if not roles:flash('Для цього облікового запису не призначено роль');return render_template('admin/login.html')
            session.clear();session.update(user_id=user['user_id'],username=user['display_name'],roles=[r['code'] for r in roles]);return redirect(url_for('admin_dashboard'))
        flash('Неправильний логін або пароль')
    return render_template('admin/login.html')
@app.route('/admin/logout',methods=['POST'])
@login_required
def admin_logout():session.clear();return redirect(url_for('localized_index',lang='uk'))
@app.route('/admin')
@login_required
def admin_dashboard():
    counts=fetchone('''SELECT (SELECT COUNT(*) FROM publications WHERE type IN ('news','page')) AS posts_count,
    (SELECT COUNT(*) FROM media_files) AS media_count,(SELECT COUNT(*) FROM publications WHERE type='page') AS pages_count,
    (SELECT COUNT(*) FROM events) AS events_count''')
    return render_template('admin/dashboard.html',**counts)
@app.route('/admin/posts')
@login_required
def admin_posts():
    rows=fetchall('''SELECT p.publication_id AS id,p.created_at,p.type,p.status,
     (SELECT title FROM publication_translations t WHERE t.publication_id=p.publication_id AND t.lang_code='uk') AS title
     FROM publications p WHERE p.type IN ('news','page') ORDER BY p.created_at DESC''')
    for x in rows:x['category_name']=x['type'];x['created_at']=str(x['created_at'])
    return render_template('admin/posts.html',posts=rows)

def post_form_data():
    typ=request.form.get('type','news');status=request.form.get('status','draft')
    if typ not in ('news','page') or status not in ('draft','review'):abort(400)
    data={l:(request.form.get('title_'+l,'').strip(),request.form.get('content_'+l,'').strip()) for l in LANGUAGES}
    if not data['uk'][0]:abort(400,'Потрібен український заголовок')
    return typ,status,data

def save_post(post_id=None):
    typ,status,data=post_form_data();db=conn()
    saved_files=[]
    try:
        cur=db.cursor(dictionary=True);db.start_transaction()
        if post_id:
            cur.execute('SELECT type,status FROM publications WHERE publication_id=%s FOR UPDATE',(post_id,))
            existing=cur.fetchone()
            if not existing:abort(404,'Матеріал не знайдено')
            if existing['status']=='published':
                # Editing published content must not withdraw it from public access.
                typ=existing['type']
                status='published'
            cur.execute('UPDATE publications SET type=%s,status=%s WHERE publication_id=%s',(typ,status,post_id))
        else:
            slug='material-'+uuid.uuid4().hex[:16]
            cur.execute('INSERT INTO publications(author_id,type,status,slug) VALUES(%s,%s,%s,%s)',(session['user_id'],typ,status,slug));post_id=cur.lastrowid
        for lang,(title,body) in data.items():
            if title:
                cur.execute('''INSERT INTO publication_translations(publication_id,lang_code,title,body) VALUES(%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE title=VALUES(title),body=VALUES(body)''',(post_id,lang,title,body))
        # Фотографії можна додавати під час створення і редагування матеріалу.
        # Поле необов'язкове, наявні фото не видаляються.
        if typ=='news':
            saved_files=save_publication_images(post_id,request.files.getlist('images'),db)
        db.commit()
    except Exception:
        db.rollback()
        for file in saved_files:file.unlink(missing_ok=True)
        raise
    finally:db.close()
    return post_id

def edit_data(pid):
    p=fetchone('SELECT publication_id AS id,type,status FROM publications WHERE publication_id=%s AND type IN (\'news\',\'page\')',(pid,))
    if not p:abort(404)
    for l in LANGUAGES:
        t=fetchone('SELECT title,body FROM publication_translations WHERE publication_id=%s AND lang_code=%s',(pid,l))
        p['title_'+l]=t['title'] if t else '';p['content_'+l]=t['body'] if t else '';p['short_text_'+l]=''
    return p
@app.route('/admin/posts/add',methods=['GET','POST'])
@role_required('editor','admin')
def admin_post_add():
    if request.method=='POST':save_post();return redirect(url_for('admin_posts'))
    return render_template('admin/post_form.html',post=None,categories=[],languages=LANGUAGES)
@app.route('/admin/posts/edit/<int:post_id>',methods=['GET','POST'])
@role_required('editor','admin')
def admin_post_edit(post_id):
    if request.method=='POST':save_post(post_id);return redirect(url_for('admin_posts'))
    return render_template('admin/post_form.html',post=edit_data(post_id),categories=[],languages=LANGUAGES,post_images=publication_images(post_id))
@app.route('/admin/posts/publish/<int:post_id>',methods=['POST'])
@role_required('publisher','admin')
def admin_post_publish(post_id):
    db=conn()
    try:
        cur=db.cursor();cur.callproc('publish_publication',(post_id,session['user_id']));db.commit()
    except mysql.connector.Error as exc:
        db.rollback();flash('Не вдалося опублікувати: '+str(exc))
    finally:db.close()
    return redirect(url_for('admin_posts'))
@app.route('/admin/posts/delete/<int:post_id>',methods=['POST'])
@role_required('admin')
def admin_post_delete(post_id):
    db=conn()
    try:
        c=db.cursor();c.execute('DELETE FROM publications WHERE publication_id=%s',(post_id,));db.commit()
    finally:db.close()
    return redirect(url_for('admin_posts'))

@app.route('/admin/events',methods=['GET','POST'])
@login_required
def admin_events():
    if request.method=='POST':
        if not has_role('editor','admin'):abort(403)
        date=request.form.get('event_date','').strip()
        start_time=request.form.get('start_time','').strip()
        end_time=request.form.get('end_time','').strip()
        title=request.form.get('title_uk','').strip()
        try:
            start=datetime.strptime(f'{date} {start_time}', '%Y-%m-%d %H:%M')
            end=datetime.strptime(f'{date} {end_time}', '%Y-%m-%d %H:%M')
        except ValueError:
            abort(400, 'Укажіть правильну дату й час події')
        if end <= start:
            abort(400, 'Час завершення події має бути пізнішим за час початку')
        if not title:abort(400,'Укажіть назву події')
        db=conn()
        try:
            c=db.cursor();db.start_transaction()
            c.execute("INSERT INTO publications(author_id,type,status,slug) VALUES(%s,'event','review',%s)",(session['user_id'],'event-'+uuid.uuid4().hex[:16]));pid=c.lastrowid
            c.execute('INSERT INTO events(publication_id,starts_at,ends_at,place) VALUES(%s,%s,%s,%s)',(pid,start,end,request.form.get('location_uk','')))
            for l in LANGUAGES:
                v=request.form.get('title_'+l,'').strip()
                if v:c.execute('INSERT INTO publication_translations(publication_id,lang_code,title,body) VALUES(%s,%s,%s,%s)',(pid,l,v,request.form.get('description_'+l,'')))
            db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
        flash('Подію додано на погодження; опублікуйте її окремо')
        return redirect(url_for('admin_events'))
    return render_template('admin/events.html',events=events_list(False))
@app.route('/admin/events/delete/<int:event_id>',methods=['POST'])
@role_required('admin')
def admin_event_delete(event_id):
    db=conn()
    try:
        c=db.cursor();c.execute('DELETE FROM publications WHERE publication_id=(SELECT publication_id FROM events WHERE event_id=%s)',(event_id,));db.commit()
    finally:db.close()
    return redirect(url_for('admin_events'))

@app.route('/admin/media',methods=['GET','POST'])
@login_required
def admin_media():
    if request.method=='POST':
        if not has_role('editor','admin'):abort(403)
        upload=request.files.get('file');pid=request.form.get('publication_id',type=int)
        if not pid or not upload or not upload.filename:abort(400,'Оберіть публікацію та файл')
        if not fetchone('SELECT publication_id FROM publications WHERE publication_id=%s',(pid,)):
            abort(400,'Обрана публікація не існує')
        ext=Path(upload.filename).suffix.lower()
        if ext not in ('.jpg','.jpeg','.png','.gif','.webp'):abort(400,'Дозволено лише зображення')
        mime={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp'}[ext]
        name=uuid.uuid4().hex+ext;destination=BASE/'static'/'uploads'/name;destination.parent.mkdir(parents=True,exist_ok=True)
        upload.save(destination)
        db=conn()
        try:
            c=db.cursor();c.execute('INSERT INTO media_files(publication_id,filename,mime_type,size_bytes) VALUES(%s,%s,%s,%s)',(pid,name,mime,destination.stat().st_size));db.commit()
        except Exception:destination.unlink(missing_ok=True);db.rollback();raise
        finally:db.close()
        return redirect(url_for('admin_media'))
    publications_for_media=fetchall('''
        SELECT p.publication_id, p.type, p.status,
               COALESCE(NULLIF(uk.title,''), NULLIF(en.title,''), NULLIF(de.title,''), CONCAT('Матеріал №',p.publication_id)) AS title
        FROM publications p
        LEFT JOIN publication_translations uk ON uk.publication_id=p.publication_id AND uk.lang_code='uk'
        LEFT JOIN publication_translations en ON en.publication_id=p.publication_id AND en.lang_code='en'
        LEFT JOIN publication_translations de ON de.publication_id=p.publication_id AND de.lang_code='de'
        ORDER BY p.created_at DESC, p.publication_id DESC
    ''')
    return render_template('admin/media.html',media_items=media_list(),publications_for_media=publications_for_media)
@app.route('/admin/media/delete/<int:media_id>',methods=['POST'])
@role_required('admin')
def admin_media_delete(media_id):
    db=conn()
    try:
        c=db.cursor(dictionary=True);c.execute('SELECT filename FROM media_files WHERE media_id=%s',(media_id,));m=c.fetchone()
        if m:
            c.execute('DELETE FROM media_files WHERE media_id=%s',(media_id,));db.commit();(BASE/'static'/'uploads'/m['filename']).unlink(missing_ok=True)
    finally:db.close()
    return redirect(url_for('admin_media'))

if __name__=='__main__':
    if app.secret_key=='DEV-ONLY-REPLACE-THIS-KEY':raise RuntimeError('Задайте FLASK_SECRET_KEY у .env')
    app.run(debug=False)
