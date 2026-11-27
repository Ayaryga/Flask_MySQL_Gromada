"""Create an administrator with a securely hashed password."""
import getpass
from app import app, conn
from werkzeug.security import generate_password_hash
email=input('Email адміністратора: ').strip()
name=input('Ім’я для відображення: ').strip()
password=getpass.getpass('Новий пароль (не показується): ')
if len(password)<12: raise SystemExit('Пароль має містити щонайменше 12 символів')
db=conn()
try:
    c=db.cursor();db.start_transaction()
    c.execute('INSERT INTO users(email,password_hash,display_name,is_active) VALUES (%s,%s,%s,1)',(email,generate_password_hash(password),name))
    uid=c.lastrowid
    c.execute("SELECT role_id FROM roles WHERE code='admin'")
    rid=c.fetchone()
    if not rid:raise RuntimeError('Роль admin відсутня; перевірте schema.sql')
    c.execute('INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)',(uid,rid[0]))
    db.commit();print('Створено адміністратора з ID:',uid)
except Exception:
    db.rollback();raise
finally:db.close()
