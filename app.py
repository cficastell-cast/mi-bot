"""
EVOX Bot — Servidor Central (Railway)
Solo maneja: leaderboard, señales, admin, registro externo.
El bot corre localmente en la PC de cada usuario.
"""
import os
import threading
import psycopg2
import psycopg2.extras
import pytz
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "cnkt1234")
DATABASE_URL  = os.environ.get("DATABASE_URL")
CDMX          = pytz.timezone('America/Mexico_City')

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db()
    cur  = conn.cursor()
    # Usuarios (sin private key — solo para leaderboard)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            wallet VARCHAR(42) UNIQUE NOT NULL,
            nombre VARCHAR(50) DEFAULT 'Anonimo',
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre VARCHAR(50) DEFAULT 'Anonimo'")
    # Ciclos reportados por los clientes locales
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ciclos (
            id SERIAL PRIMARY KEY,
            wallet VARCHAR(42) NOT NULL,
            fecha DATE DEFAULT CURRENT_DATE,
            hora_compra VARCHAR(20),
            precio_compra FLOAT,
            hora_venta VARCHAR(20),
            precio_venta FLOAT,
            cnkt_comprado FLOAT,
            cnkt_vendido FLOAT,
            ganancia_usdt FLOAT,
            amount_usdt FLOAT DEFAULT 0,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE ciclos ADD COLUMN IF NOT EXISTS amount_usdt FLOAT DEFAULT 0")
    # Señales
    cur.execute("""
        CREATE TABLE IF NOT EXISTS senales (
            id SERIAL PRIMARY KEY,
            emisor VARCHAR(50) NOT NULL,
            categoria VARCHAR(50) NOT NULL,
            mensaje TEXT NOT NULL,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    # Master / padawans (opcional, por si quieres mantenerlo)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS master_config (
            id INT PRIMARY KEY DEFAULT 1,
            activo BOOLEAN DEFAULT FALSE,
            wallet_master VARCHAR(42),
            rango_bajo FLOAT,
            rango_alto FLOAT,
            amount_usdt FLOAT,
            stop_zona FLOAT DEFAULT 0.03,
            actualizado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("INSERT INTO master_config (id, activo) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS padawans (
            wallet VARCHAR(42) PRIMARY KEY,
            activo BOOLEAN DEFAULT FALSE,
            actualizado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("DB lista!")

# ─── REGISTRO EXTERNO (desde bot local) ──────────────────────────────────────
@app.route("/registro_externo", methods=["POST"])
def registro_externo():
    """El bot local llama esto para registrar al usuario en el leaderboard."""
    data   = request.json or {}
    wallet = data.get("wallet", "").lower()
    nombre = data.get("nombre", "Anonimo").strip() or "Anonimo"
    if not wallet:
        return jsonify({"ok": False, "msg": "Falta wallet"})
    try:
        conn = get_db()
        cur  = conn.cursor()
        # Verificar nombre único
        cur.execute("SELECT wallet FROM usuarios WHERE nombre = %s AND wallet != %s", (nombre, wallet))
        if cur.fetchone():
            # Agregar sufijo numérico para que sea único
            import random
            nombre = nombre + str(random.randint(10, 99))
        cur.execute("""
            INSERT INTO usuarios (wallet, nombre) VALUES (%s, %s)
            ON CONFLICT (wallet) DO UPDATE SET nombre = EXCLUDED.nombre
        """, (wallet, nombre))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── REPORTAR CICLO (desde bot local) ────────────────────────────────────────
@app.route("/reportar_ciclo", methods=["POST"])
def reportar_ciclo():
    """El bot local reporta cada ciclo completado para el leaderboard."""
    data   = request.json or {}
    wallet = data.get("wallet", "").lower()
    nombre = data.get("nombre", "Anonimo")
    ciclo  = data.get("ciclo", {})
    if not wallet or not ciclo:
        return jsonify({"ok": False, "msg": "Faltan datos"})
    try:
        conn = get_db()
        cur  = conn.cursor()
        # Asegurar que el usuario exista
        cur.execute("""
            INSERT INTO usuarios (wallet, nombre) VALUES (%s, %s)
            ON CONFLICT (wallet) DO NOTHING
        """, (wallet, nombre))
        # Guardar ciclo
        cur.execute("""
            INSERT INTO ciclos (wallet, fecha, hora_compra, precio_compra, hora_venta,
                                precio_venta, cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            wallet,
            ciclo.get("fecha", datetime.now(CDMX).strftime('%Y-%m-%d')),
            ciclo.get("hora_compra", ""),
            ciclo.get("precio_compra", 0),
            ciclo.get("hora_venta", ""),
            ciclo.get("precio_venta", 0),
            ciclo.get("cnkt_comprado", 0),
            ciclo.get("cnkt_vendido", 0),
            ciclo.get("ganancia_usdt", 0),
            ciclo.get("amount_usdt", 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── SEÑALES ──────────────────────────────────────────────────────────────────
@app.route("/senal", methods=["GET"])
def get_senal():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT emisor, categoria, mensaje,
                   to_char(creado_en AT TIME ZONE 'America/Mexico_City', 'HH24:MI DD/MM') as hora
            FROM senales
            WHERE creado_en >= NOW() - INTERVAL '24 hours'
            ORDER BY creado_en DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return jsonify({"ok": True, "senal": dict(row)})
        return jsonify({"ok": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/senal", methods=["POST"])
def post_senal():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "No autorizado"}), 401
    emisor    = data.get("emisor", "EVOX")
    categoria = data.get("categoria", "General")
    mensaje   = data.get("mensaje", "").strip()
    if not mensaje:
        return jsonify({"ok": False, "msg": "Mensaje vacio"})
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("DELETE FROM senales")
        cur.execute("INSERT INTO senales (emisor, categoria, mensaje) VALUES (%s, %s, %s)",
                    (emisor, categoria, mensaje))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "msg": "Senal publicada!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ─── LEADERBOARD ──────────────────────────────────────────────────────────────
@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    try:
        conn = get_db()
        cur  = conn.cursor()

        def query(order_by, periodo_filter=""):
            cur.execute(f"""
                SELECT u.nombre, u.wallet,
                       COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total,
                       COUNT(c.id) as ciclos_total
                FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet
                {periodo_filter}
                GROUP BY u.nombre,u.wallet ORDER BY {order_by} DESC LIMIT 25
            """)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                w = d["wallet"]
                d["wallet_short"] = w[:6] + "..." + w[-4:]
                del d["wallet"]
                rows.append(d)
            return rows

        hoy_filter = "AND c.fecha=(NOW() AT TIME ZONE 'America/Mexico_City')::date"
        mes_filter = "AND DATE_TRUNC('month',c.creado_en AT TIME ZONE 'America/Mexico_City')=DATE_TRUNC('month',NOW() AT TIME ZONE 'America/Mexico_City')"

        result = {
            "hoy_ganancias":       query("ganancia_total", hoy_filter),
            "hoy_ciclos":          query("ciclos_total",   hoy_filter),
            "mes_ganancias":       query("ganancia_total", mes_filter),
            "mes_ciclos":          query("ciclos_total",   mes_filter),
            "historico_ganancias": query("ganancia_total"),
            "historico_ciclos":    query("ciclos_total"),
        }
        cur.close()
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})

# ─── PADAWAN / MASTER STATUS (solo lectura para el cliente local) ─────────────
@app.route("/padawan/status/<wallet>", methods=["GET"])
def padawan_status(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT activo FROM padawans WHERE wallet=%s", (wallet,))
        row = cur.fetchone()
        cur.execute("SELECT activo, rango_bajo, rango_alto, amount_usdt, wallet_master FROM master_config WHERE id=1")
        mc  = cur.fetchone()
        cur.close()
        conn.close()
        es_padawan       = bool(row and row["activo"])
        master_activo    = bool(mc and mc["activo"])
        es_wallet_master = bool(mc and mc["activo"] and mc["wallet_master"] and mc["wallet_master"].lower() == wallet)
        return jsonify({
            "ok": True, "es_padawan": es_padawan,
            "master_activo": master_activo,
            "es_wallet_master": es_wallet_master,
            "master_config": dict(mc) if mc else {}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ─── ADMIN ────────────────────────────────────────────────────────────────────
@app.route("/admin", methods=["GET"])
def admin():
    if request.args.get("password") != BOT_PASSWORD:
        return jsonify({"error": "No autorizado"}), 401
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM usuarios")
        total_usuarios = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as ciclos FROM ciclos")
        ciclos_totales = cur.fetchone()["ciclos"]
        cur.execute("SELECT COUNT(*) as total FROM ciclos WHERE tipo_swap='COMPRA'") if False else None
        cur.execute("SELECT COALESCE(SUM(amount_usdt),0) as volumen FROM ciclos WHERE creado_en >= NOW() - INTERVAL '24 hours'")
        volumen_24h = float(cur.fetchone()["volumen"])
        cur.execute("SELECT COALESCE(SUM(amount_usdt),0) as total FROM ciclos")
        total_usdt = float(cur.fetchone()["total"])
        cur.execute("""
            SELECT u.nombre, u.wallet, u.creado_en,
                   COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet
            GROUP BY u.nombre,u.wallet,u.creado_en ORDER BY ganancia_total DESC
        """)
        usuarios = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT c.wallet, u.nombre, c.fecha, c.precio_compra, c.precio_venta,
                   c.ganancia_usdt, c.amount_usdt,
                   to_char(c.creado_en AT TIME ZONE 'America/Mexico_City','HH24:MI:SS') as hora_registro
            FROM ciclos c JOIN usuarios u ON c.wallet=u.wallet
            ORDER BY c.creado_en DESC LIMIT 20
        """)
        ultimos_ciclos = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({
            "resumen": {
                "total_usuarios": total_usuarios,
                "bots_activos": 0,  # Los bots corren localmente
                "ciclos_totales": ciclos_totales,
                "compras_totales": 0,
                "ventas_totales": 0,
                "volumen_24h": volumen_24h,
                "comisiones_estimadas": total_usdt * 0.002,
            },
            "usuarios": usuarios,
            "ultimos_ciclos": ultimos_ciclos
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin-panel", methods=["GET"])
def admin_panel():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    try:
        return open(os.path.join(BASE_DIR, "admin.html"), encoding="utf-8").read(), 200, {"Content-Type": "text/html"}
    except Exception as e:
        return "admin.html no encontrado: " + str(e), 404

@app.route("/", methods=["GET"])
def home():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    try:
        return open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8").read(), 200, {"Content-Type": "text/html"}
    except:
        return jsonify({"msg": "EVOX Central API corriendo", "version": "2.0-distributed"})

@app.route("/download/EVOX_Bot.zip", methods=["GET"])
def download_exe():
    """Sirve el zip del exe. Pon el EVOX_Bot.zip en la misma carpeta que server_central.py"""
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(BASE_DIR, "EVOX_Bot.zip")
    if os.path.exists(zip_path):
        from flask import send_file
        return send_file(zip_path, as_attachment=True, download_name="EVOX_Bot.zip")
    return jsonify({"error": "Archivo no disponible aún"}), 404

@app.route("/reset_db", methods=["POST"])
def reset_db():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"error": "No autorizado"}), 401
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("TRUNCATE TABLE usuarios, ciclos, senales RESTART IDENTITY")
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "msg": "DB limpiada!"})
    except Exception as e:
        return jsonify({"error": str(e)})

for img in ["icon", "evox", "charlie", "susan"]:
    def make_route(name):
        @app.route(f"/{name}.png", methods=["GET"], endpoint=f"img_{name}")
        def img_route():
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            try:
                return open(os.path.join(BASE_DIR, f"{name}.png"), "rb").read(), 200, {"Content-Type": "image/png"}
            except:
                return "", 404
    make_route(img)

@app.route("/reset_db", methods=["POST"])
def reset_db():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"error": "No autorizado"}), 401
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("TRUNCATE TABLE usuarios, ciclos, senales RESTART IDENTITY")
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "msg": "DB limpiada!"})
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print("EVOX Central API en puerto " + str(port))
    app.run(host="0.0.0.0", port=port)