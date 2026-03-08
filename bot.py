import os
import threading
import time
import requests
import psycopg2
import psycopg2.extras
import pytz
import bcrypt
from datetime import datetime
from web3 import Web3
from flask import Flask, jsonify, request
from flask_cors import CORS
from cryptography.fernet import Fernet

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENCRYPT_KEY = os.environ.get("ENCRYPT_KEY")
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "cnkt1234")
fernet = Fernet(ENCRYPT_KEY.encode() if isinstance(ENCRYPT_KEY, str) else ENCRYPT_KEY)

USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
CNKT_ADDRESS = "0x87bdfbe98ba55104701b2f2e999982a317905637"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
FEE_RECEIVER = "0x1C02ADbA08aA59Be60fB6d4DD79eD82F986Df918"

TOKEN_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

DATABASE_URL = os.environ.get("DATABASE_URL")
CDMX = pytz.timezone('America/Mexico_City')

precio_global = {"valor": 0, "actualizado": 0}
precio_lock = threading.Lock()

def loop_precio_global():
    while True:
        try:
            params = {"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": 10 * 10**6}
            r = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
                             params=params, timeout=10).json()
            amount_out = float(r['data']['routeSummary']['amountOut']) / 10**18
            amount_in_usd = float(r['data']['routeSummary']['amountInUsd'])
            precio = amount_in_usd / amount_out
            with precio_lock:
                precio_global["valor"] = round(precio, 6)
                precio_global["actualizado"] = time.time()
        except Exception as e:
            print("Error actualizando precio global: " + str(e))
        time.sleep(15)

def get_precio_actual():
    with precio_lock:
        return precio_global["valor"]

_w3 = None
_w3_lock = threading.Lock()

def get_w3():
    global _w3
    with _w3_lock:
        if _w3 is None:
            _w3 = Web3(Web3.HTTPProvider(os.environ.get("RPC_URL")))
        return _w3

def hora_cdmx():
    return datetime.now(CDMX).strftime('%H:%M:%S')

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            wallet VARCHAR(42) UNIQUE NOT NULL,
            nombre VARCHAR(50) DEFAULT 'Anonimo',
            private_key_enc TEXT NOT NULL,
            password_hash VARCHAR(256),
            ganancia_acumulada FLOAT DEFAULT 0,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre VARCHAR(50) DEFAULT 'Anonimo'")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ganancia_acumulada FLOAT DEFAULT 0")
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS swaps (
            id SERIAL PRIMARY KEY,
            wallet VARCHAR(42) NOT NULL,
            tipo VARCHAR(10) NOT NULL,
            amount_usdt FLOAT NOT NULL,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bots_activos (
            wallet VARCHAR(42) PRIMARY KEY,
            rango_bajo FLOAT NOT NULL,
            rango_alto FLOAT NOT NULL,
            amount_usdt FLOAT NOT NULL,
            stop_zona FLOAT NOT NULL,
            actualizado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS senales (
            id SERIAL PRIMARY KEY,
            emisor VARCHAR(50) NOT NULL,
            categoria VARCHAR(50) NOT NULL,
            mensaje TEXT NOT NULL,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    """)
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
    print("Base de datos lista!")

bots_activos = {}
bots_lock = threading.Lock()

def nuevo_estado():
    return {
        "activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
        "ciclos": 0, "ganancia_total": 0, "ultimo_log": "", "logs": [], "cnkt_comprados": 0,
        "RANGO_BAJO": None, "RANGO_ALTO": None, "AMOUNT_USDT": None, "STOP_ZONA": None,
    }

def log_estado(estado, msg):
    hora = hora_cdmx()
    linea = hora + " | " + msg
    print(linea)
    estado["ultimo_log"] = linea
    estado["logs"].append(linea)
    if len(estado["logs"]) > 100:
        estado["logs"] = estado["logs"][-100:]

def guardar_bot_activo(wallet, rango_bajo, rango_alto, amount_usdt, stop_zona):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bots_activos (wallet, rango_bajo, rango_alto, amount_usdt, stop_zona)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (wallet) DO UPDATE SET
                rango_bajo=EXCLUDED.rango_bajo, rango_alto=EXCLUDED.rango_alto,
                amount_usdt=EXCLUDED.amount_usdt, stop_zona=EXCLUDED.stop_zona,
                actualizado_en=NOW()
        """, (wallet, rango_bajo, rango_alto, amount_usdt, stop_zona))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Error guardando bot activo: " + str(e))

def eliminar_bot_activo(wallet):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM bots_activos WHERE wallet = %s", (wallet,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Error eliminando bot activo: " + str(e))

def registrar_swap(wallet, tipo, amount_usdt):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO swaps (wallet, tipo, amount_usdt) VALUES (%s, %s, %s)", (wallet, tipo, amount_usdt))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Error registrando swap: " + str(e))

def guardar_ciclo(wallet, hora_compra, precio_compra, hora_venta, precio_venta, cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt):
    try:
        conn = get_db()
        cur = conn.cursor()
        hora_compra_final = hora_compra if hora_compra else "previo"
        precio_compra_final = precio_compra if precio_compra else 0
        cur.execute("""
            INSERT INTO ciclos (wallet, hora_compra, precio_compra, hora_venta, precio_venta,
                                cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (wallet, hora_compra_final, precio_compra_final, hora_venta, precio_venta,
              cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt))
        cur.execute("UPDATE usuarios SET ganancia_acumulada = ganancia_acumulada + %s WHERE wallet = %s",
                    (ganancia_usdt, wallet))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Error guardando ciclo: " + str(e))

def cargar_ganancia_acumulada(wallet):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT ganancia_acumulada FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return float(row["ganancia_acumulada"]) if row else 0
    except:
        return 0

def loop_bot(wallet, private_key, estado, stop_event):
    w3 = get_w3()
    account = w3.eth.account.from_key(private_key)
    usdt_contract = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
    cnkt_contract = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)

    RANGO_BAJO  = estado["RANGO_BAJO"]
    RANGO_ALTO  = estado["RANGO_ALTO"]
    AMOUNT_USDT = estado["AMOUNT_USDT"]
    STOP_ZONA   = estado["STOP_ZONA"]
    STOP_ABAJO  = RANGO_BAJO * (1 - STOP_ZONA)
    STOP_ARRIBA = RANGO_ALTO * (1 + STOP_ZONA)
    INTERVALO   = 30
    cnkt_necesario = AMOUNT_USDT / RANGO_ALTO

    def get_balance_usdt():
        return usdt_contract.functions.balanceOf(account.address).call() / 10**6

    def get_balance_cnkt():
        return cnkt_contract.functions.balanceOf(account.address).call() / 10**18

    def aprobar_tokens():
        log_estado(estado, "Aprobando tokens...")
        try:
            gas_price = int(w3.eth.gas_price * 1.5)
            tx_usdt = usdt_contract.functions.approve(KYBER_ROUTER, 1000 * 10**6).build_transaction({
                "from": account.address, "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": gas_price, "chainId": 137
            })
            w3.eth.send_raw_transaction(account.sign_transaction(tx_usdt).raw_transaction)
            log_estado(estado, "USDT aprobado!")
        except Exception as e:
            log_estado(estado, "Error aprobando USDT: " + str(e))
            if stop_event.is_set():
                return
        stop_event.wait(15)
        if stop_event.is_set():
            return
        try:
            gas_price = int(w3.eth.gas_price * 1.5)
            tx_cnkt = cnkt_contract.functions.approve(KYBER_ROUTER, 10000000 * 10**18).build_transaction({
                "from": account.address, "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": gas_price, "chainId": 137
            })
            w3.eth.send_raw_transaction(account.sign_transaction(tx_cnkt).raw_transaction)
            log_estado(estado, "CNKT aprobado!")
        except Exception as e:
            log_estado(estado, "Error aprobando CNKT: " + str(e))
            if stop_event.is_set():
                return
        stop_event.wait(15)
        if stop_event.is_set():
            return

    def comprar():
        amount_in = int(AMOUNT_USDT * 10**6)
        route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
            params={"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": amount_in,
                    "feeAmount": "20", "isInBps": "true", "feeReceiver": FEE_RECEIVER,
                    "chargeFeeBy": "currency_in"}).json()
        build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
            json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
                  "recipient": account.address, "slippageTolerance": 50}).json()
        tx = {"from": account.address, "to": build['data']['routerAddress'], "data": build['data']['data'],
              "value": int(build['data']['transactionValue']), "nonce": w3.eth.get_transaction_count(account.address),
              "gasPrice": w3.eth.gas_price, "gas": int(build['data']['gas']) + 50000, "chainId": 137}
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
        log_estado(estado, "COMPRA: https://polygonscan.com/tx/" + tx_hash.hex())
        registrar_swap(wallet, "COMPRA", AMOUNT_USDT)
        stop_event.wait(15)
        return float(route['data']['routeSummary']['amountOut']) / 10**18

    def vender(cantidad_cnkt):
        amount_in = int(cantidad_cnkt * 10**18)
        route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
            params={"tokenIn": CNKT_ADDRESS, "tokenOut": USDT_ADDRESS, "amountIn": amount_in,
                    "feeAmount": "20", "isInBps": "true", "feeReceiver": FEE_RECEIVER,
                    "chargeFeeBy": "currency_in"}).json()
        build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
            json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
                  "recipient": account.address, "slippageTolerance": 50}).json()
        tx = {"from": account.address, "to": build['data']['routerAddress'], "data": build['data']['data'],
              "value": int(build['data']['transactionValue']), "nonce": w3.eth.get_transaction_count(account.address),
              "gasPrice": w3.eth.gas_price, "gas": int(build['data']['gas']) + 50000, "chainId": 137}
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
        log_estado(estado, "VENTA: https://polygonscan.com/tx/" + tx_hash.hex())
        registrar_swap(wallet, "VENTA", AMOUNT_USDT)
        stop_event.wait(15)
        return float(route['data']['routeSummary']['amountOut']) / 10**6

    log_estado(estado, "BOT INICIADO para " + wallet[:6] + "..." + wallet[-4:])
    log_estado(estado, "Compra en: $" + str(RANGO_BAJO))
    log_estado(estado, "Vende en:  $" + str(RANGO_ALTO))
    log_estado(estado, "Capital:   $" + str(AMOUNT_USDT))

    aprobar_tokens()

    if stop_event.is_set():
        estado["activo"] = False
        eliminar_bot_activo(wallet)
        log_estado(estado, "Bot detenido durante aprobacion.")
        return

    usdt_actual = get_balance_usdt()
    cnkt_actual = get_balance_cnkt()
    log_estado(estado, "USDT: $" + str(round(usdt_actual, 2)))
    log_estado(estado, "CNKT: " + str(round(cnkt_actual, 2)))

    if usdt_actual < AMOUNT_USDT and cnkt_actual < cnkt_necesario:
        log_estado(estado, "ERROR: No tienes USDT ni CNKT suficiente")
        estado["activo"] = False
        eliminar_bot_activo(wallet)
        return

    estado["ganancia_total"] = cargar_ganancia_acumulada(wallet)
    hora_compra_actual = None
    precio_compra_actual = None
    cnkt_comprado_actual = 0

    while not stop_event.is_set():
        try:
            precio = get_precio_actual()
            if precio < 0.000001:
                log_estado(estado, "Esperando precio...")
                stop_event.wait(5)
                continue

            usdt = get_balance_usdt()
            cnkt = get_balance_cnkt()
            estado["precio"] = precio
            estado["usdt"] = round(usdt, 2)
            estado["cnkt"] = round(cnkt, 2)

            if precio <= RANGO_BAJO:
                estado["modo"] = "COMPRA"
            elif precio >= RANGO_ALTO:
                estado["modo"] = "VENTA"

            modo = estado["modo"]
            log_estado(estado, "$" + str(precio) + " | USDT: $" + str(round(usdt,2)) +
                       " | CNKT: " + str(round(cnkt,0)) + " | Modo: " + modo + " | Ciclos: " + str(estado["ciclos"]))

            if precio < STOP_ABAJO or precio > STOP_ARRIBA:
                log_estado(estado, "PRECIO FUERA DE RANGO - BOT DETENIDO")
                estado["activo"] = False
                eliminar_bot_activo(wallet)
                break

            if precio <= RANGO_BAJO and modo == "COMPRA":
                if usdt >= AMOUNT_USDT:
                    log_estado(estado, "Senal de COMPRA!")
                    hora_compra_actual = hora_cdmx()
                    precio_compra_actual = precio
                    estado["cnkt_comprados"] = comprar()
                    if stop_event.is_set():
                        break
                    cnkt_comprado_actual = estado["cnkt_comprados"]
                    log_estado(estado, "CNKT recibidos: " + str(round(estado["cnkt_comprados"], 2)))
                else:
                    log_estado(estado, "Esperando USDT suficiente...")

            elif precio >= RANGO_ALTO and modo == "VENTA":
                cnkt_comp = estado["cnkt_comprados"]
                if cnkt_comp > 0 and cnkt >= cnkt_comp:
                    log_estado(estado, "Senal de VENTA!")
                    hora_venta = hora_cdmx()
                    usdt_recibido = vender(cnkt_comp)
                    if stop_event.is_set():
                        break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ganancia_total"] += ganancia
                    estado["ciclos"] += 1
                    log_estado(estado, "Ganancia ciclo: $" + str(round(ganancia, 4)))
                    log_estado(estado, "Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                    guardar_ciclo(wallet, hora_compra_actual, precio_compra_actual,
                                  hora_venta, precio, cnkt_comprado_actual, cnkt_comp, ganancia, AMOUNT_USDT)
                    estado["cnkt_comprados"] = 0
                    cnkt_comprado_actual = 0
                    hora_compra_actual = None
                    precio_compra_actual = None
                elif cnkt_comp == 0 and cnkt >= cnkt_necesario:
                    log_estado(estado, "Senal de VENTA! (CNKT previo)")
                    hora_venta = hora_cdmx()
                    usdt_recibido = vender(cnkt_necesario)
                    if stop_event.is_set():
                        break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ganancia_total"] += ganancia
                    estado["ciclos"] += 1
                    log_estado(estado, "Ganancia ciclo: $" + str(round(ganancia, 4)))
                    log_estado(estado, "Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                    guardar_ciclo(wallet, "previo", precio, hora_venta, precio,
                                  0, cnkt_necesario, ganancia, AMOUNT_USDT)
                else:
                    log_estado(estado, "Esperando CNKT suficiente...")
            else:
                log_estado(estado, "Esperando...")

            stop_event.wait(INTERVALO)

        except Exception as e:
            log_estado(estado, "Error: " + str(e))
            stop_event.wait(30)

    estado["activo"] = False
    eliminar_bot_activo(wallet)
    log_estado(estado, "Bot detenido.")

def iniciar_bot_thread(wallet, private_key, rango_bajo, rango_alto, amount_usdt, stop_zona):
    estado = nuevo_estado()
    estado["activo"] = True
    estado["RANGO_BAJO"] = rango_bajo
    estado["RANGO_ALTO"] = rango_alto
    estado["AMOUNT_USDT"] = amount_usdt
    estado["STOP_ZONA"] = stop_zona
    stop_event = threading.Event()
    t = threading.Thread(target=loop_bot, args=(wallet, private_key, estado, stop_event), daemon=True)
    t.start()
    with bots_lock:
        bots_activos[wallet] = {"thread": t, "stop_event": stop_event, "estado": estado}

def restaurar_bots():
    print("Restaurando bots activos...")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ba.wallet, ba.rango_bajo, ba.rango_alto, ba.amount_usdt, ba.stop_zona, u.private_key_enc
            FROM bots_activos ba JOIN usuarios u ON ba.wallet = u.wallet
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for i, row in enumerate(rows):
            try:
                private_key = fernet.decrypt(row["private_key_enc"].encode()).decode()
                threading.Timer(i * 3, iniciar_bot_thread, args=[
                    row["wallet"], private_key, row["rango_bajo"],
                    row["rango_alto"], row["amount_usdt"], row["stop_zona"]
                ]).start()
                print("Bot restaurado: " + row["wallet"][:6] + "...")
            except Exception as e:
                print("Error restaurando: " + str(e))
        print(str(len(rows)) + " bots restaurados!")
    except Exception as e:
        print("Error restaurando bots: " + str(e))

@app.route("/master/status", methods=["GET"])
def master_status():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM master_config WHERE id=1")
        mc = cur.fetchone()
        cur.execute("SELECT COUNT(*) as total FROM padawans WHERE activo=TRUE")
        padawans_activos = cur.fetchone()["total"]
        cur.close()
        conn.close()
        return jsonify({"ok": True, "master": dict(mc), "padawans_activos": padawans_activos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/master/start", methods=["POST"])
def master_start():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "No autorizado"}), 401
    try:
        rango_bajo    = float(data["rango_bajo"])
        rango_alto    = float(data["rango_alto"])
        amount_usdt   = float(data["amount_usdt"])
        stop_zona     = float(data.get("stop_zona", 0.03))
        wallet_master = data.get("wallet_master", "").lower()
    except (KeyError, ValueError):
        return jsonify({"ok": False, "msg": "Faltan parametros"})
    if not wallet_master:
        return jsonify({"ok": False, "msg": "Falta wallet del master"})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet=%s", (wallet_master,))
        master_row = cur.fetchone()
        if not master_row:
            cur.close(); conn.close()
            return jsonify({"ok": False, "msg": "Wallet master no registrada en el sistema"})
        cur.execute("""
            UPDATE master_config SET activo=TRUE, wallet_master=%s, rango_bajo=%s, rango_alto=%s,
            amount_usdt=%s, stop_zona=%s, actualizado_en=NOW() WHERE id=1
        """, (wallet_master, rango_bajo, rango_alto, amount_usdt, stop_zona))
        cur.execute("SELECT wallet FROM padawans WHERE activo=TRUE")
        padawan_wallets = [r["wallet"] for r in cur.fetchall()]
        conn.commit()
        cur.close()
        conn.close()
        private_key_master = fernet.decrypt(master_row["private_key_enc"].encode()).decode()
        with bots_lock:
            if wallet_master not in bots_activos or not bots_activos[wallet_master]["estado"]["activo"]:
                guardar_bot_activo(wallet_master, rango_bajo, rango_alto, amount_usdt, stop_zona)
                iniciar_bot_thread(wallet_master, private_key_master, rango_bajo, rango_alto, amount_usdt, stop_zona)
        for wallet in padawan_wallets:
            _arrancar_padawan(wallet, rango_bajo, rango_alto, amount_usdt, stop_zona)
        return jsonify({"ok": True, "msg": "Modo master iniciado! " + str(len(padawan_wallets)) + " padawans arrancando."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/master/stop", methods=["POST"])
def master_stop():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "No autorizado"}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE master_config SET activo=FALSE, actualizado_en=NOW() WHERE id=1")
        cur.execute("SELECT wallet_master FROM master_config WHERE id=1")
        mc = cur.fetchone()
        cur.execute("SELECT wallet FROM padawans WHERE activo=TRUE")
        padawan_wallets = [r["wallet"] for r in cur.fetchall()]
        conn.commit()
        cur.close()
        conn.close()
        detenidos = 0
        wallets_a_detener = padawan_wallets[:]
        if mc and mc["wallet_master"]:
            wallets_a_detener.append(mc["wallet_master"])
        with bots_lock:
            for wallet in wallets_a_detener:
                if wallet in bots_activos and bots_activos[wallet]["estado"]["activo"]:
                    bots_activos[wallet]["stop_event"].set()
                    eliminar_bot_activo(wallet)
                    detenidos += 1
        return jsonify({"ok": True, "msg": "Modo master detenido. " + str(detenidos) + " padawans detenidos."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/padawan/activar/<wallet>", methods=["POST"])
def padawan_activar(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO padawans (wallet, activo) VALUES (%s, TRUE) ON CONFLICT (wallet) DO UPDATE SET activo=TRUE, actualizado_en=NOW()", (wallet,))
        cur.execute("SELECT * FROM master_config WHERE id=1 AND activo=TRUE")
        mc = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if mc:
            _arrancar_padawan(wallet, mc["rango_bajo"], mc["rango_alto"], mc["amount_usdt"], mc["stop_zona"])
            return jsonify({"ok": True, "msg": "Modo padawan activado! Tu bot arrancara en breve."})
        return jsonify({"ok": True, "msg": "Modo padawan activado! Esperando al master."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/padawan/desactivar/<wallet>", methods=["POST"])
def padawan_desactivar(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE padawans SET activo=FALSE, actualizado_en=NOW() WHERE wallet=%s", (wallet,))
        conn.commit()
        cur.close()
        conn.close()
        with bots_lock:
            if wallet in bots_activos and bots_activos[wallet]["estado"]["activo"]:
                bots_activos[wallet]["stop_event"].set()
                eliminar_bot_activo(wallet)
        return jsonify({"ok": True, "msg": "Modo padawan desactivado."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/padawan/status/<wallet>", methods=["GET"])
def padawan_status(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT activo FROM padawans WHERE wallet=%s", (wallet,))
        row = cur.fetchone()
        cur.execute("SELECT activo, rango_bajo, rango_alto, amount_usdt, wallet_master FROM master_config WHERE id=1")
        mc = cur.fetchone()
        cur.close()
        conn.close()
        es_padawan = bool(row and row["activo"])
        master_activo = bool(mc and mc["activo"])
        es_wallet_master = bool(mc and mc["activo"] and mc["wallet_master"] and mc["wallet_master"].lower() == wallet)
        return jsonify({"ok": True, "es_padawan": es_padawan, "master_activo": master_activo, "es_wallet_master": es_wallet_master, "master_config": dict(mc) if mc else {}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def _arrancar_padawan(wallet, rango_bajo, rango_alto, amount_usdt_master, stop_zona):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet=%s", (wallet,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return
        w3 = get_w3()
        usdt_contract = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
        try:
            from web3 import Web3 as W3
            balance = usdt_contract.functions.balanceOf(W3.to_checksum_address(wallet)).call() / 10**6
        except:
            balance = 0
        capital = min(int(balance // 5) * 5, amount_usdt_master)
        if capital < 5:
            print("Padawan " + wallet[:6] + " sin capital suficiente")
            return
        private_key = fernet.decrypt(row["private_key_enc"].encode()).decode()
        with bots_lock:
            if wallet in bots_activos and bots_activos[wallet]["estado"]["activo"]:
                return
        guardar_bot_activo(wallet, rango_bajo, rango_alto, capital, stop_zona)
        iniciar_bot_thread(wallet, private_key, rango_bajo, rango_alto, capital, stop_zona)
        print("Padawan arrancado: " + wallet[:6] + " capital: $" + str(capital))
    except Exception as e:
        print("Error arrancando padawan " + wallet[:6] + ": " + str(e))

@app.route("/precio", methods=["GET"])
def precio_endpoint():
    precio = get_precio_actual()
    if precio > 0:
        return jsonify({"ok": True, "precio": precio})
    return jsonify({"ok": False, "precio": 0})

@app.route("/senal", methods=["GET"])
def get_senal():
    try:
        conn = get_db()
        cur = conn.cursor()
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
    pwd = data.get("password", "")
    if pwd != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "No autorizado"}), 401
    emisor    = data.get("emisor", "EVOX")
    categoria = data.get("categoria", "General")
    mensaje   = data.get("mensaje", "").strip()
    if not mensaje:
        return jsonify({"ok": False, "msg": "Mensaje vacio"})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM senales")
        cur.execute("INSERT INTO senales (emisor, categoria, mensaje) VALUES (%s, %s, %s)",
                    (emisor, categoria, mensaje))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "msg": "Senal publicada!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/ciclos_hoy/<wallet>", methods=["GET"])
def ciclos_hoy(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as ciclos_hoy, COALESCE(SUM(ganancia_usdt), 0) as ganancia_hoy
            FROM ciclos WHERE wallet = %s
            AND fecha = (NOW() AT TIME ZONE 'America/Mexico_City')::date
        """, (wallet,))
        hoy = cur.fetchone()
        cur.execute("SELECT COUNT(*) as ciclos_total FROM ciclos WHERE wallet = %s", (wallet,))
        total = cur.fetchone()
        cur.execute("SELECT ganancia_acumulada FROM usuarios WHERE wallet = %s", (wallet,))
        acum = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({
            "ok": True,
            "ciclos_hoy": int(hoy["ciclos_hoy"]) if hoy else 0,
            "ciclos_total": int(total["ciclos_total"]) if total else 0,
            "ganancia_hoy": float(hoy["ganancia_hoy"]) if hoy else 0,
            "ganancia_acum": float(acum["ganancia_acumulada"]) if acum else 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/registro", methods=["POST"])
def registro():
    data = request.json or {}
    wallet = data.get("wallet", "").lower()
    private_key = data.get("private_key", "")
    nombre = data.get("nombre", "Anonimo").strip() or "Anonimo"
    password = data.get("password", "")
    if not wallet or not private_key or not password:
        return jsonify({"ok": False, "msg": "Faltan campos obligatorios"})
    if len(password) < 6:
        return jsonify({"ok": False, "msg": "La contrasena debe tener al menos 6 caracteres"})
    try:
        w3 = get_w3()
        account = w3.eth.account.from_key(private_key)
        if account.address.lower() != wallet.lower():
            return jsonify({"ok": False, "msg": "La private key no corresponde a esta wallet"})
    except:
        return jsonify({"ok": False, "msg": "Private key invalida"})
    pk_enc = fernet.encrypt(private_key.encode()).decode()
    pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT wallet FROM usuarios WHERE nombre = %s AND wallet != %s", (nombre, wallet.lower()))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"ok": False, "msg": "Ese nombre ya esta en uso, elige otro"})
        cur.execute("""
            INSERT INTO usuarios (wallet, nombre, private_key_enc, password_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (wallet) DO UPDATE SET
                private_key_enc=EXCLUDED.private_key_enc,
                nombre=EXCLUDED.nombre,
                password_hash=EXCLUDED.password_hash
        """, (wallet.lower(), nombre, pk_enc, pwd_hash))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "msg": "Usuario registrado!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": "Error: " + str(e)})

@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    wallet = data.get("wallet", "").lower()
    password = data.get("password", "")
    if not wallet or not password:
        return jsonify({"ok": False, "msg": "Faltan wallet y contrasena"})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nombre, password_hash FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "msg": "Error DB: " + str(e)})
    if not row:
        return jsonify({"ok": False, "msg": "Wallet no registrada"})
    if not row["password_hash"]:
        return jsonify({"ok": False, "msg": "Re-registrate para crear tu contrasena"})
    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
    return jsonify({"ok": True, "nombre": row["nombre"], "wallet": wallet})

@app.route("/status/<wallet>", methods=["GET"])
def status(wallet):
    wallet = wallet.lower()
    with bots_lock:
        if wallet in bots_activos:
            est = bots_activos[wallet]["estado"]
            return jsonify({
                "activo": est["activo"], "modo": est["modo"], "precio": est["precio"],
                "usdt": est["usdt"], "cnkt": est["cnkt"], "ciclos": est["ciclos"],
                "ganancia_total": est["ganancia_total"], "ultimo_log": est["ultimo_log"],
                "config": {"RANGO_BAJO": est["RANGO_BAJO"], "RANGO_ALTO": est["RANGO_ALTO"],
                           "AMOUNT_USDT": est["AMOUNT_USDT"], "STOP_ZONA": est["STOP_ZONA"]},
                "wallet": wallet
            })
    ganancia_db = cargar_ganancia_acumulada(wallet)
    return jsonify({"activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
                    "ciclos": 0, "ganancia_total": ganancia_db, "ultimo_log": "", "wallet": wallet,
                    "config": {"RANGO_BAJO": None, "RANGO_ALTO": None, "AMOUNT_USDT": None, "STOP_ZONA": None}})

@app.route("/logs/<wallet>", methods=["GET"])
def logs(wallet):
    wallet = wallet.lower()
    with bots_lock:
        if wallet in bots_activos:
            return jsonify({"logs": bots_activos[wallet]["estado"]["logs"]})
    return jsonify({"logs": []})

@app.route("/historial/<wallet>", methods=["GET"])
def historial(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT fecha, hora_compra, precio_compra, hora_venta, precio_venta,
                   cnkt_comprado, cnkt_vendido, ganancia_usdt,
                   to_char(creado_en AT TIME ZONE 'America/Mexico_City', 'HH24:MI:SS') as hora_registro
            FROM ciclos WHERE wallet = %s ORDER BY creado_en DESC LIMIT 50
        """, (wallet,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"historial": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"historial": [], "error": str(e)})

@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet AND c.fecha=(NOW() AT TIME ZONE 'America/Mexico_City')::date
            GROUP BY u.nombre,u.wallet ORDER BY ganancia_total DESC LIMIT 25
        """)
        hoy_ganancias = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet AND c.fecha=(NOW() AT TIME ZONE 'America/Mexico_City')::date
            GROUP BY u.nombre,u.wallet ORDER BY ciclos_total DESC LIMIT 25
        """)
        hoy_ciclos = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet
            AND DATE_TRUNC('month',c.creado_en AT TIME ZONE 'America/Mexico_City')=DATE_TRUNC('month',NOW() AT TIME ZONE 'America/Mexico_City')
            GROUP BY u.nombre,u.wallet ORDER BY ganancia_total DESC LIMIT 25
        """)
        mes_ganancias = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet
            AND DATE_TRUNC('month',c.creado_en AT TIME ZONE 'America/Mexico_City')=DATE_TRUNC('month',NOW() AT TIME ZONE 'America/Mexico_City')
            GROUP BY u.nombre,u.wallet ORDER BY ciclos_total DESC LIMIT 25
        """)
        mes_ciclos = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet GROUP BY u.nombre,u.wallet ORDER BY ganancia_total DESC LIMIT 25
        """)
        historico_ganancias = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT u.nombre, u.wallet, COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet GROUP BY u.nombre,u.wallet ORDER BY ciclos_total DESC LIMIT 25
        """)
        historico_ciclos = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        for lista in [hoy_ganancias, hoy_ciclos, mes_ganancias, mes_ciclos, historico_ganancias, historico_ciclos]:
            for u in lista:
                w = u["wallet"]
                u["wallet_short"] = w[:6] + "..." + w[-4:]
                del u["wallet"]
        return jsonify({
            "hoy_ganancias": hoy_ganancias, "hoy_ciclos": hoy_ciclos,
            "mes_ganancias": mes_ganancias, "mes_ciclos": mes_ciclos,
            "historico_ganancias": historico_ganancias, "historico_ciclos": historico_ciclos,
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/admin", methods=["GET"])
def admin():
    pwd = request.args.get("password", "")
    if pwd != BOT_PASSWORD:
        return jsonify({"error": "No autorizado"}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM usuarios")
        total_usuarios = cur.fetchone()["total"]
        with bots_lock:
            bots_en_memoria = sum(1 for b in bots_activos.values() if b["estado"]["activo"])
        cur.execute("SELECT COUNT(*) as ciclos FROM ciclos")
        ciclos_totales = cur.fetchone()["ciclos"]
        cur.execute("SELECT COUNT(*) as total FROM swaps WHERE tipo='COMPRA'")
        compras_totales = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM swaps WHERE tipo='VENTA'")
        ventas_totales = cur.fetchone()["total"]
        cur.execute("SELECT COALESCE(SUM(amount_usdt),0) as volumen FROM swaps WHERE creado_en >= NOW() - INTERVAL '24 hours'")
        volumen_24h = float(cur.fetchone()["volumen"])
        cur.execute("SELECT COALESCE(SUM(amount_usdt),0) as total FROM swaps")
        total_swaps_usdt = float(cur.fetchone()["total"])
        comisiones_estimadas = total_swaps_usdt * 0.002
        cur.execute("""
            SELECT u.nombre, u.wallet, u.creado_en,
                   COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total, COUNT(c.id) as ciclos_total
            FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet
            GROUP BY u.nombre,u.wallet,u.creado_en ORDER BY ganancia_total DESC
        """)
        usuarios = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT c.wallet, u.nombre, c.fecha, c.precio_compra, c.precio_venta, c.ganancia_usdt,
                   to_char(c.creado_en AT TIME ZONE 'America/Mexico_City','HH24:MI:SS') as hora_registro
            FROM ciclos c JOIN usuarios u ON c.wallet=u.wallet ORDER BY c.creado_en DESC LIMIT 20
        """)
        ultimos_ciclos = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        for u in usuarios:
            w = u["wallet"].lower()
            with bots_lock:
                u["bot_activo"] = w in bots_activos and bots_activos[w]["estado"]["activo"]
        return jsonify({
            "resumen": {
                "total_usuarios": total_usuarios, "bots_activos": bots_en_memoria,
                "ciclos_totales": ciclos_totales, "compras_totales": compras_totales,
                "ventas_totales": ventas_totales, "volumen_24h": volumen_24h,
                "comisiones_estimadas": comisiones_estimadas,
            },
            "usuarios": usuarios, "ultimos_ciclos": ultimos_ciclos
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/start/<wallet>", methods=["POST"])
def start(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "msg": "Error DB: " + str(e)})
    if not row:
        return jsonify({"ok": False, "msg": "Usuario no registrado"})
    with bots_lock:
        if wallet in bots_activos and bots_activos[wallet]["estado"]["activo"]:
            return jsonify({"ok": False, "msg": "Bot ya esta corriendo"})
    data = request.json or {}
    try:
        rango_bajo  = float(data["rango_bajo"])
        rango_alto  = float(data["rango_alto"])
        amount_usdt = float(data["amount_usdt"])
        stop_zona   = float(data.get("stop_zona", 0.03))
    except (KeyError, ValueError):
        return jsonify({"ok": False, "msg": "Faltan parametros"})
    min_pct = 0.03 if amount_usdt <= 10 else 0.04
    pct_rango = (rango_alto - rango_bajo) / rango_bajo
    if pct_rango < min_pct:
        return jsonify({"ok": False, "msg": "Margen demasiado pequeño para operar de forma segura (minimo " + str(int(min_pct*100)) + "%)"})
    try:
        private_key = fernet.decrypt(row["private_key_enc"].encode()).decode()
    except:
        return jsonify({"ok": False, "msg": "Error desencriptando key"})
    guardar_bot_activo(wallet, rango_bajo, rango_alto, amount_usdt, stop_zona)
    iniciar_bot_thread(wallet, private_key, rango_bajo, rango_alto, amount_usdt, stop_zona)
    return jsonify({"ok": True, "msg": "Bot iniciado!"})

@app.route("/stop/<wallet>", methods=["POST"])
def stop(wallet):
    wallet = wallet.lower()
    with bots_lock:
        if wallet not in bots_activos or not bots_activos[wallet]["estado"]["activo"]:
            return jsonify({"ok": False, "msg": "Bot no esta corriendo"})
        bots_activos[wallet]["stop_event"].set()
        bots_activos[wallet]["estado"]["activo"] = False
    eliminar_bot_activo(wallet)
    return jsonify({"ok": True, "msg": "Deteniendo bot..."})

@app.route("/", methods=["GET"])
def home():
    try:
        return open(os.path.join(BASE_DIR, "control.html")).read(), 200, {"Content-Type": "text/html"}
    except:
        return jsonify({"msg": "EVOX Bot API corriendo"})

@app.route("/admin-panel", methods=["GET"])
def admin_panel():
    try:
        content = open(os.path.join(BASE_DIR, "admin.html")).read()
        return content, 200, {"Content-Type": "text/html"}
    except Exception as e:
        return "admin.html no encontrado: " + str(e), 404

@app.route("/manifest.json", methods=["GET"])
def manifest():
    try:
        return open(os.path.join(BASE_DIR, "manifest.json")).read(), 200, {"Content-Type": "application/json"}
    except:
        return "{}", 404

for img in ["icon", "evox", "charlie", "susan"]:
    def make_route(name):
        @app.route(f"/{name}.png", methods=["GET"], endpoint=f"img_{name}")
        def img_route():
            try:
                return open(os.path.join(BASE_DIR, f"{name}.png"), "rb").read(), 200, {"Content-Type": "image/png"}
            except:
                return "", 404
    make_route(img)

if __name__ == "__main__":
    init_db()
    t_precio = threading.Thread(target=loop_precio_global, daemon=True)
    t_precio.start()
    print("Precio global iniciado!")
    restaurar_bots()
    port = int(os.environ.get("PORT", 5000))
    print("API corriendo en puerto " + str(port))
    app.run(host="0.0.0.0", port=port)