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

# ── VERSIÓN ───────────────────────────────────────────────────
VERSION_ACTUAL = "2.0.0"

# ── CONFIGURACIÓN ─────────────────────────────────────────────
ENCRYPT_KEY  = os.environ.get("ENCRYPT_KEY")
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "cnkt1234")
fernet       = Fernet(ENCRYPT_KEY.encode() if isinstance(ENCRYPT_KEY, str) else ENCRYPT_KEY)
DATABASE_URL = os.environ.get("DATABASE_URL")
CDMX         = pytz.timezone('America/Mexico_City')

# ── CONTRATOS POLYGON ─────────────────────────────────────────
USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
CNKT_ADDRESS = "0x87bdfbe98ba55104701b2f2e999982a317905637"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
FEE_RECEIVER = "0x1C02ADbA08aA59Be60fB6d4DD79eD82F986Df918"

TOKEN_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

# ══════════════════════════════════════════════════════════════
#  POOL DE RPCs — ROTACIÓN AUTOMÁTICA
# ══════════════════════════════════════════════════════════════
def _build_rpc_pool():
    pool = []
    # Primero los Alchemy (más confiables)
    for i in range(1, 5):
        url = os.environ.get(f"RPC_{i}", "").strip()
        if url:
            pool.append(url)
    # RPCs públicos como respaldo
    pool += [
        "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic",
        "https://polygon.drpc.org",
    ]
    return pool

RPC_POOL      = _build_rpc_pool()
_rpc_index    = 0
_rpc_lock     = threading.Lock()
_rpc_fallos   = {}
_w3_cache     = {}
_w3_lock      = threading.Lock()
_rpc_semaphore = threading.Semaphore(20)  # máx 20 llamadas RPC simultáneas

def get_rpc_url():
    with _rpc_lock:
        return RPC_POOL[_rpc_index % len(RPC_POOL)]

def get_w3():
    url = get_rpc_url()
    with _w3_lock:
        if url not in _w3_cache:
            _w3_cache[url] = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
        return _w3_cache[url]

def rotar_rpc(rpc_fallido=None):
    global _rpc_index
    with _rpc_lock:
        if rpc_fallido:
            _rpc_fallos[rpc_fallido] = _rpc_fallos.get(rpc_fallido, 0) + 1
        _rpc_index = (_rpc_index + 1) % len(RPC_POOL)
        nuevo = RPC_POOL[_rpc_index % len(RPC_POOL)]
        print(f"[RPC] Rotando a: {nuevo.split('/v2/')[0]}...")
        return nuevo

def llamada_rpc(fn, max_rotaciones=4):
    """
    Ejecuta una llamada RPC con reintentos inteligentes:
    - Si da 429: reintenta en la MISMA RPC hasta 3 veces (cada 15s)
    - Solo rota a la siguiente RPC si falla 3 veces seguidas
    - Maximo max_rotaciones cambios de RPC
    """
    with _rpc_semaphore:
        ultimo_error = None
        for rotacion in range(max_rotaciones):
            fallos_en_rpc_actual = 0
            while fallos_en_rpc_actual < 3:
                try:
                    return fn()
                except Exception as e:
                    ultimo_error = e
                    es_rate = "429" in str(e) or "rate" in str(e).lower() or "too many" in str(e).lower()
                    if es_rate:
                        fallos_en_rpc_actual += 1
                        rpc_label = get_rpc_url().split('/v2/')[0]
                        print(f"[RPC] 429 en {rpc_label} (fallo {fallos_en_rpc_actual}/3), esperando 15s...")
                        time.sleep(15)
                    else:
                        raise e
            # 3 fallos seguidos en esta RPC -> rotar
            rpc_fallida = get_rpc_url()
            rotar_rpc(rpc_fallida)
            print(f"[RPC] Rotando despues de 3 fallos")
        raise ultimo_error

# ══════════════════════════════════════════════════════════════
#  CACHÉ DE BALANCES GLOBAL
#  Un solo loop lee todos los balances cada 45s
#  en lugar de que cada bot lo haga cada 30s
# ══════════════════════════════════════════════════════════════
_balance_cache  = {}   # wallet -> {"usdt": float, "cnkt": float, "ts_usdt": float, "ts_cnkt": float}
_balance_lock   = threading.Lock()
BALANCE_TTL     = 45   # segundos antes de considerar el cache expirado

def get_balance_usdt_cached(wallet):
    with _balance_lock:
        entry = _balance_cache.get(wallet.lower())
        if entry and (time.time() - entry.get("ts_usdt", 0)) < BALANCE_TTL:
            return entry["usdt"]
    return _leer_balance_usdt(wallet)

def get_balance_cnkt_cached(wallet):
    with _balance_lock:
        entry = _balance_cache.get(wallet.lower())
        if entry and (time.time() - entry.get("ts_cnkt", 0)) < BALANCE_TTL:
            return entry["cnkt"]
    return _leer_balance_cnkt(wallet)

def _leer_balance_usdt(wallet):
    w3 = get_w3()
    contract = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
    val = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call() / 10**6
    with _balance_lock:
        entry = _balance_cache.get(wallet.lower(), {"usdt": 0, "cnkt": 0, "ts_usdt": 0, "ts_cnkt": 0})
        entry["usdt"]    = val
        entry["ts_usdt"] = time.time()
        _balance_cache[wallet.lower()] = entry
    return val

def _leer_balance_cnkt(wallet):
    try:
        w3 = get_w3()
        contract = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)
        raw = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        val = raw / 10**18
        with _balance_lock:
            entry = _balance_cache.get(wallet.lower(), {"usdt": 0, "cnkt": 0, "ts_usdt": 0, "ts_cnkt": 0})
            entry["cnkt"]    = val
            entry["ts_cnkt"] = time.time()
            _balance_cache[wallet.lower()] = entry
        return val
    except Exception as e:
        print(f"[Balance CNKT] Error {wallet[:6]}: {e}")
        return 0

def invalidar_balance(wallet):
    """Fuerza re-lectura del balance después de una tx."""
    with _balance_lock:
        _balance_cache.pop(wallet.lower(), None)

def loop_balance_global():
    """Actualiza balances de todos los bots activos cada 45s."""
    while True:
        try:
            with bots_lock:
                wallets = [w for w, b in bots_activos.items() if b["estado"]["activo"]]
            for wallet in wallets:
                try:
                    _leer_balance_usdt(wallet)
                    time.sleep(0.3)  # pequeña pausa entre wallets
                    _leer_balance_cnkt(wallet)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[Balance] Error {wallet[:6]}: {e}")
        except Exception as e:
            print(f"[Balance Global] Error: {e}")
        time.sleep(45)

# ══════════════════════════════════════════════════════════════
#  PRECIO GLOBAL
# ══════════════════════════════════════════════════════════════
precio_global = {"valor": 0, "actualizado": 0}
precio_lock   = threading.Lock()

def loop_precio_global():
    while True:
        try:
            params = {"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": 10 * 10**6}
            r = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
                             params=params, timeout=10).json()
            amount_out    = float(r['data']['routeSummary']['amountOut']) / 10**18
            amount_in_usd = float(r['data']['routeSummary']['amountInUsd'])
            precio = amount_in_usd / amount_out
            with precio_lock:
                precio_global["valor"]       = round(precio, 6)
                precio_global["actualizado"] = time.time()
        except Exception as e:
            print(f"[Precio] Error: {e}")
        time.sleep(15)

def get_precio_actual():
    with precio_lock:
        return precio_global["valor"]

# ── UTILIDADES ────────────────────────────────────────────────
def hora_cdmx():
    return datetime.now(CDMX).strftime('%H:%M:%S')

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

# ══════════════════════════════════════════════════════════════
#  BASE DE DATOS
# ══════════════════════════════════════════════════════════════
def init_db():
    conn = get_db()
    cur  = conn.cursor()
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_estados (
            wallet VARCHAR(42) PRIMARY KEY,
            nombre VARCHAR(50) DEFAULT 'Anonimo',
            evento VARCHAR(20) NOT NULL,
            version VARCHAR(20) DEFAULT '2.0.0',
            actualizado_en TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("Base de datos lista!")

# ══════════════════════════════════════════════════════════════
#  HELPERS DB
# ══════════════════════════════════════════════════════════════
def guardar_bot_activo(wallet, rango_bajo, rango_alto, amount_usdt, stop_zona):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO bots_activos (wallet, rango_bajo, rango_alto, amount_usdt, stop_zona)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (wallet) DO UPDATE SET
                rango_bajo=EXCLUDED.rango_bajo, rango_alto=EXCLUDED.rango_alto,
                amount_usdt=EXCLUDED.amount_usdt, stop_zona=EXCLUDED.stop_zona,
                actualizado_en=NOW()
        """, (wallet, rango_bajo, rango_alto, amount_usdt, stop_zona))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[DB] Error guardando bot activo: {e}")

def eliminar_bot_activo(wallet):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM bots_activos WHERE wallet = %s", (wallet,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[DB] Error eliminando bot activo: {e}")

def registrar_swap(wallet, tipo, amount_usdt):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO swaps (wallet, tipo, amount_usdt) VALUES (%s,%s,%s)",
                    (wallet, tipo, amount_usdt))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[DB] Error registrando swap: {e}")

def guardar_ciclo(wallet, hora_compra, precio_compra, hora_venta, precio_venta,
                  cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO ciclos (wallet, hora_compra, precio_compra, hora_venta, precio_venta,
                                cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (wallet, hora_compra or "previo", precio_compra or 0,
              hora_venta, precio_venta, cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt))
        cur.execute("UPDATE usuarios SET ganancia_acumulada = ganancia_acumulada + %s WHERE wallet = %s",
                    (ganancia_usdt, wallet))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[DB] Error guardando ciclo: {e}")

def cargar_ganancia_acumulada(wallet):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT ganancia_acumulada FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone(); cur.close(); conn.close()
        return float(row["ganancia_acumulada"]) if row else 0
    except:
        return 0

# ══════════════════════════════════════════════════════════════
#  LOOP DEL BOT
# ══════════════════════════════════════════════════════════════
bots_activos = {}
bots_lock    = threading.Lock()

def nuevo_estado():
    return {
        "activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
        "ciclos": 0, "ganancia_total": 0, "ultimo_log": "", "logs": [],
        "cnkt_comprados": 0,
        "RANGO_BAJO": None, "RANGO_ALTO": None, "AMOUNT_USDT": None, "STOP_ZONA": None,
    }

LOGS_IMPORTANTES = ["COMPRA", "VENTA", "ERROR", "Error", "STOP", "aprobado", "Ganancia", "DETENIDO", "iniciado", "INICIADO", "restaurado"]

def log_estado(estado, msg):
    hora  = hora_cdmx()
    linea = f"{hora} | {msg}"
    # Solo imprimir en Railway si es un evento importante
    if any(k in msg for k in LOGS_IMPORTANTES):
        print(linea)
    estado["ultimo_log"] = linea
    estado["logs"].append(linea)
    if len(estado["logs"]) > 100:
        estado["logs"] = estado["logs"][-100:]

def loop_bot(wallet, private_key, estado, stop_event):
    w3      = get_w3()
    account = w3.eth.account.from_key(private_key)

    RANGO_BAJO  = estado["RANGO_BAJO"]
    RANGO_ALTO  = estado["RANGO_ALTO"]
    AMOUNT_USDT = estado["AMOUNT_USDT"]
    STOP_ZONA   = estado["STOP_ZONA"]
    STOP_ABAJO  = RANGO_BAJO  * (1 - STOP_ZONA)
    STOP_ARRIBA = RANGO_ALTO  * (1 + STOP_ZONA)
    INTERVALO   = 30
    cnkt_necesario = AMOUNT_USDT / RANGO_ALTO

    usdt_contract = get_w3().eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
    cnkt_contract = get_w3().eth.contract(
        address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)

    def get_balance_usdt():
        return get_balance_usdt_cached(wallet)

    def get_balance_cnkt():
        return get_balance_cnkt_cached(wallet)

    def aprobar_tokens():
        # Aprobacion infinita ya se hizo al registrarse — no se necesita hacer nada
        log_estado(estado, "Tokens aprobados.")
        return True

    def comprar():
        amount_in = int(AMOUNT_USDT * 10**6)
        route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
            params={"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": amount_in,
                    "feeAmount": "20", "isInBps": "true", "feeReceiver": FEE_RECEIVER,
                    "chargeFeeBy": "currency_in"}, timeout=10).json()
        build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
            json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
                  "recipient": account.address, "slippageTolerance": 50}, timeout=10).json()
        w3_actual = get_w3()
        tx = {
            "from": account.address, "to": build['data']['routerAddress'],
            "data": build['data']['data'],
            "value": int(build['data']['transactionValue']),
            "nonce": llamada_rpc(lambda: w3_actual.eth.get_transaction_count(account.address)),
            "gasPrice": llamada_rpc(lambda: w3_actual.eth.gas_price),
            "gas": int(build['data']['gas']) + 50000, "chainId": 137
        }
        tx_hash = llamada_rpc(lambda: w3_actual.eth.send_raw_transaction(
            account.sign_transaction(tx).raw_transaction))
        log_estado(estado, f"COMPRA: https://polygonscan.com/tx/{tx_hash.hex()}")
        registrar_swap(wallet, "COMPRA", AMOUNT_USDT)
        invalidar_balance(wallet)  # fuerza re-lectura del balance
        stop_event.wait(15)
        return float(route['data']['routeSummary']['amountOut']) / 10**18

    def vender(cantidad_cnkt):
        amount_in = int(cantidad_cnkt * 10**18)
        route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
            params={"tokenIn": CNKT_ADDRESS, "tokenOut": USDT_ADDRESS, "amountIn": amount_in,
                    "feeAmount": "20", "isInBps": "true", "feeReceiver": FEE_RECEIVER,
                    "chargeFeeBy": "currency_in"}, timeout=10).json()
        build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
            json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
                  "recipient": account.address, "slippageTolerance": 50}, timeout=10).json()
        w3_actual = get_w3()
        tx = {
            "from": account.address, "to": build['data']['routerAddress'],
            "data": build['data']['data'],
            "value": int(build['data']['transactionValue']),
            "nonce": llamada_rpc(lambda: w3_actual.eth.get_transaction_count(account.address)),
            "gasPrice": llamada_rpc(lambda: w3_actual.eth.gas_price),
            "gas": int(build['data']['gas']) + 50000, "chainId": 137
        }
        tx_hash = llamada_rpc(lambda: w3_actual.eth.send_raw_transaction(
            account.sign_transaction(tx).raw_transaction))
        log_estado(estado, f"VENTA: https://polygonscan.com/tx/{tx_hash.hex()}")
        registrar_swap(wallet, "VENTA", AMOUNT_USDT)
        invalidar_balance(wallet)  # fuerza re-lectura del balance
        stop_event.wait(15)
        return float(route['data']['routeSummary']['amountOut']) / 10**6

    log_estado(estado, f"BOT INICIADO — {wallet[:6]}...{wallet[-4:]}")
    log_estado(estado, f"Compra en: ${RANGO_BAJO}")
    log_estado(estado, f"Vende en:  ${RANGO_ALTO}")
    log_estado(estado, f"Capital:   ${AMOUNT_USDT}")

    if not aprobar_tokens():
        estado["activo"] = False
        eliminar_bot_activo(wallet)
        return

    if stop_event.is_set():
        estado["activo"] = False
        eliminar_bot_activo(wallet)
        log_estado(estado, "Bot detenido durante aprobacion.")
        return

    # Forzar lectura fresca de balances al arrancar
    invalidar_balance(wallet)
    time.sleep(1)
    usdt_actual = get_balance_usdt()
    time.sleep(1)
    cnkt_actual = get_balance_cnkt()
    time.sleep(1)
    log_estado(estado, f"USDT: ${round(usdt_actual, 2)}")
    log_estado(estado, f"CNKT: {round(cnkt_actual, 2)}")

    if usdt_actual < AMOUNT_USDT and cnkt_actual < cnkt_necesario:
        log_estado(estado, "ERROR: No tienes USDT ni CNKT suficiente")
        estado["activo"] = False
        eliminar_bot_activo(wallet)
        return

    estado["ganancia_total"] = cargar_ganancia_acumulada(wallet)
    hora_compra_actual   = None
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
            estado["usdt"]   = round(usdt, 2)
            estado["cnkt"]   = round(cnkt, 2)

            if precio <= RANGO_BAJO:
                estado["modo"] = "COMPRA"
            elif precio >= RANGO_ALTO:
                estado["modo"] = "VENTA"

            modo = estado["modo"]
            log_estado(estado, f"${precio} | USDT:${round(usdt,2)} | CNKT:{round(cnkt,0)} | {modo} | Ciclos:{estado['ciclos']}")

            if precio < STOP_ABAJO or precio > STOP_ARRIBA:
                log_estado(estado, "PRECIO FUERA DE RANGO — BOT DETENIDO")
                estado["activo"] = False
                eliminar_bot_activo(wallet)
                break

            if precio <= RANGO_BAJO and modo == "COMPRA":
                if usdt >= AMOUNT_USDT:
                    log_estado(estado, "Senal de COMPRA!")
                    hora_compra_actual   = hora_cdmx()
                    precio_compra_actual = precio
                    estado["cnkt_comprados"] = comprar()
                    if stop_event.is_set(): break
                    cnkt_comprado_actual = estado["cnkt_comprados"]
                    log_estado(estado, f"CNKT recibidos: {round(estado['cnkt_comprados'], 2)}")
                else:
                    log_estado(estado, "Esperando USDT suficiente...")

            elif precio >= RANGO_ALTO and modo == "VENTA":
                cnkt_comp = estado["cnkt_comprados"]
                if cnkt_comp > 0 and cnkt >= cnkt_comp:
                    log_estado(estado, "Senal de VENTA!")
                    hora_venta    = hora_cdmx()
                    usdt_recibido = vender(cnkt_comp)
                    if stop_event.is_set(): break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ganancia_total"] += ganancia
                    estado["ciclos"] += 1
                    log_estado(estado, f"Ganancia ciclo: ${round(ganancia, 4)}")
                    log_estado(estado, f"Ganancia total: ${round(estado['ganancia_total'], 4)}")
                    guardar_ciclo(wallet, hora_compra_actual, precio_compra_actual,
                                  hora_venta, precio, cnkt_comprado_actual, cnkt_comp,
                                  ganancia, AMOUNT_USDT)
                    estado["cnkt_comprados"] = 0
                    cnkt_comprado_actual      = 0
                    hora_compra_actual        = None
                    precio_compra_actual      = None

                elif cnkt_comp == 0 and cnkt >= cnkt_necesario:
                    log_estado(estado, "Senal de VENTA! (CNKT previo)")
                    hora_venta    = hora_cdmx()
                    usdt_recibido = vender(cnkt_necesario)
                    if stop_event.is_set(): break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ganancia_total"] += ganancia
                    estado["ciclos"] += 1
                    log_estado(estado, f"Ganancia ciclo: ${round(ganancia, 4)}")
                    log_estado(estado, f"Ganancia total: ${round(estado['ganancia_total'], 4)}")
                    guardar_ciclo(wallet, "previo", precio, hora_venta, precio,
                                  0, cnkt_necesario, ganancia, AMOUNT_USDT)
                else:
                    log_estado(estado, "Esperando CNKT suficiente...")
            else:
                log_estado(estado, "Esperando...")

            stop_event.wait(INTERVALO)

        except Exception as e:
            log_estado(estado, f"Error: {e}")
            stop_event.wait(30)

    estado["activo"] = False
    eliminar_bot_activo(wallet)
    log_estado(estado, "Bot detenido.")

def iniciar_bot_thread(wallet, private_key, rango_bajo, rango_alto, amount_usdt, stop_zona):
    estado = nuevo_estado()
    estado.update({
        "activo": True, "RANGO_BAJO": rango_bajo,
        "RANGO_ALTO": rango_alto, "AMOUNT_USDT": amount_usdt, "STOP_ZONA": stop_zona,
    })
    stop_event = threading.Event()
    t = threading.Thread(target=loop_bot,
                         args=(wallet, private_key, estado, stop_event), daemon=True)
    t.start()
    with bots_lock:
        bots_activos[wallet] = {"thread": t, "stop_event": stop_event, "estado": estado}

def restaurar_bots():
    print("Restaurando bots activos...")
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT ba.wallet, ba.rango_bajo, ba.rango_alto, ba.amount_usdt, ba.stop_zona,
                   u.private_key_enc
            FROM bots_activos ba JOIN usuarios u ON ba.wallet = u.wallet
            WHERE u.private_key_enc != 'local'
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        for i, row in enumerate(rows):
            try:
                pk = fernet.decrypt(row["private_key_enc"].encode()).decode()
                threading.Timer(i * 3, iniciar_bot_thread, args=[
                    row["wallet"], pk, row["rango_bajo"],
                    row["rango_alto"], row["amount_usdt"], row["stop_zona"]
                ]).start()
                print(f"Bot restaurado: {row['wallet'][:6]}...")
            except Exception as e:
                print(f"Error restaurando: {e}")
        print(f"{len(rows)} bots restaurados!")
    except Exception as e:
        print(f"Error restaurando bots: {e}")

def aprobar_tokens_inicial(private_key, wallet):
    """Aprueba USDT y CNKT con cantidad infinita. Se llama una sola vez al registrarse."""
    MAX_UINT256 = 2**256 - 1
    try:
        w3      = get_w3()
        account = w3.eth.account.from_key(private_key)
        usdt_c  = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
        cnkt_c  = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)
        resultados = {}
        for nombre, contract in [("USDT", usdt_c), ("CNKT", cnkt_c)]:
            try:
                gas_price = int(llamada_rpc(lambda: w3.eth.gas_price) * 1.5)
                nonce     = llamada_rpc(lambda: w3.eth.get_transaction_count(account.address))
                tx = contract.functions.approve(KYBER_ROUTER, MAX_UINT256).build_transaction({
                    "from": account.address, "nonce": nonce,
                    "gasPrice": gas_price, "chainId": 137
                })
                tx_hash = llamada_rpc(lambda: w3.eth.send_raw_transaction(
                    account.sign_transaction(tx).raw_transaction))
                resultados[nombre] = tx_hash.hex()
                print(f"[Aprobacion] {nombre} aprobado: {tx_hash.hex()}")
                time.sleep(5)  # pequeña pausa entre las dos aprobaciones
            except Exception as e:
                print(f"[Aprobacion] Error en {nombre}: {e}")
                resultados[nombre] = f"error: {e}"
        return resultados
    except Exception as e:
        print(f"[Aprobacion] Error general: {e}")
        return {}

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — AUTENTICACIÓN
# ══════════════════════════════════════════════════════════════
@app.route("/registro", methods=["POST"])
def registro():
    data        = request.json or {}
    wallet      = data.get("wallet", "").lower().strip()
    private_key = data.get("private_key", "").strip()
    nombre      = data.get("nombre", "Anonimo").strip() or "Anonimo"
    password    = data.get("password", "")
    if not wallet or not private_key or not password:
        return jsonify({"ok": False, "msg": "Faltan campos obligatorios"})
    if len(password) < 6:
        return jsonify({"ok": False, "msg": "La contrasena debe tener al menos 6 caracteres"})
    try:
        w3      = get_w3()
        account = w3.eth.account.from_key(private_key)
        if account.address.lower() != wallet:
            return jsonify({"ok": False, "msg": "La private key no corresponde a esta wallet"})
    except:
        return jsonify({"ok": False, "msg": "Private key invalida"})
    pk_enc   = fernet.encrypt(private_key.encode()).decode()
    pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT wallet FROM usuarios WHERE nombre = %s AND wallet != %s", (nombre, wallet))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"ok": False, "msg": "Ese nombre ya esta en uso, elige otro"})
        cur.execute("""
            INSERT INTO usuarios (wallet, nombre, private_key_enc, password_hash)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (wallet) DO UPDATE SET
                private_key_enc=EXCLUDED.private_key_enc,
                nombre=EXCLUDED.nombre,
                password_hash=EXCLUDED.password_hash
        """, (wallet, nombre, pk_enc, pwd_hash))
        conn.commit(); cur.close(); conn.close()
        # Aprobar tokens en background (no bloquea el registro)
        threading.Thread(target=aprobar_tokens_inicial, args=(private_key, wallet), daemon=True).start()
        return jsonify({"ok": True, "msg": "Usuario registrado! Aprobando tokens en background...", "nombre": nombre, "wallet": wallet})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error: {e}"})

@app.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    wallet   = data.get("wallet", "").lower().strip()
    password = data.get("password", "")
    if not wallet or not password:
        return jsonify({"ok": False, "msg": "Faltan wallet y contrasena"})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT nombre, password_hash FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error DB: {e}"})
    if not row:
        return jsonify({"ok": False, "msg": "Wallet no registrada"})
    if not row["password_hash"]:
        return jsonify({"ok": False, "msg": "Re-registrate para crear tu contrasena"})
    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
    return jsonify({"ok": True, "nombre": row["nombre"], "wallet": wallet})

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — BOT
# ══════════════════════════════════════════════════════════════
@app.route("/start/<wallet>", methods=["POST"])
def start(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet = %s", (wallet,))
        row = cur.fetchone(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error DB: {e}"})
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
    min_pct   = 0.03 if amount_usdt <= 10 else 0.04
    pct_rango = (rango_alto - rango_bajo) / rango_bajo
    if pct_rango < min_pct:
        return jsonify({"ok": False,
                        "msg": f"Margen demasiado pequeno (minimo {int(min_pct*100)}%)"})
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

@app.route("/status/<wallet>", methods=["GET"])
def status(wallet):
    wallet = wallet.lower()
    with bots_lock:
        if wallet in bots_activos:
            est = bots_activos[wallet]["estado"]
            return jsonify({
                "activo": est["activo"], "modo": est["modo"],
                "precio": est["precio"], "usdt": est["usdt"], "cnkt": est["cnkt"],
                "ciclos": est["ciclos"], "ganancia_total": est["ganancia_total"],
                "ultimo_log": est["ultimo_log"],
                "config": {
                    "RANGO_BAJO": est["RANGO_BAJO"], "RANGO_ALTO": est["RANGO_ALTO"],
                    "AMOUNT_USDT": est["AMOUNT_USDT"], "STOP_ZONA": est["STOP_ZONA"]
                },
                "wallet": wallet
            })
    ganancia_db = cargar_ganancia_acumulada(wallet)
    return jsonify({
        "activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
        "ciclos": 0, "ganancia_total": ganancia_db, "ultimo_log": "", "wallet": wallet,
        "config": {"RANGO_BAJO": None, "RANGO_ALTO": None, "AMOUNT_USDT": None, "STOP_ZONA": None}
    })

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
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT fecha, hora_compra, precio_compra, hora_venta, precio_venta,
                   cnkt_comprado, cnkt_vendido, ganancia_usdt,
                   to_char(creado_en AT TIME ZONE 'America/Mexico_City','HH24:MI:SS') as hora_registro
            FROM ciclos WHERE wallet = %s ORDER BY creado_en DESC LIMIT 50
        """, (wallet,))
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"historial": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"historial": [], "error": str(e)})

@app.route("/ciclos_hoy/<wallet>", methods=["GET"])
def ciclos_hoy(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as ciclos_hoy, COALESCE(SUM(ganancia_usdt),0) as ganancia_hoy
            FROM ciclos WHERE wallet = %s
            AND fecha = (NOW() AT TIME ZONE 'America/Mexico_City')::date
        """, (wallet,))
        hoy = cur.fetchone()
        cur.execute("SELECT COUNT(*) as ciclos_total FROM ciclos WHERE wallet = %s", (wallet,))
        total = cur.fetchone()
        cur.execute("SELECT ganancia_acumulada FROM usuarios WHERE wallet = %s", (wallet,))
        acum = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({
            "ok": True,
            "ciclos_hoy":   int(hoy["ciclos_hoy"]) if hoy else 0,
            "ciclos_total": int(total["ciclos_total"]) if total else 0,
            "ganancia_hoy": float(hoy["ganancia_hoy"]) if hoy else 0,
            "ganancia_acum": float(acum["ganancia_acumulada"]) if acum else 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — PRECIO Y SEÑALES
# ══════════════════════════════════════════════════════════════
@app.route("/precio", methods=["GET"])
def precio_endpoint():
    precio = get_precio_actual()
    return jsonify({"ok": precio > 0, "precio": precio})

@app.route("/senal", methods=["GET"])
def get_senal():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT emisor, categoria, mensaje,
                   to_char(creado_en AT TIME ZONE 'America/Mexico_City','HH24:MI DD/MM') as hora
            FROM senales
            WHERE creado_en >= NOW() - INTERVAL '24 hours'
            ORDER BY creado_en DESC LIMIT 1
        """)
        row = cur.fetchone(); cur.close(); conn.close()
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
        conn = get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM senales")
        cur.execute("INSERT INTO senales (emisor, categoria, mensaje) VALUES (%s,%s,%s)",
                    (emisor, categoria, mensaje))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True, "msg": "Senal publicada!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — LEADERBOARD
# ══════════════════════════════════════════════════════════════
@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    try:
        conn = get_db(); cur = conn.cursor()
        def query_lb(order_by, periodo_filter=""):
            cur.execute(f"""
                SELECT u.nombre, u.wallet,
                       COALESCE(SUM(c.ganancia_usdt),0) as ganancia_total,
                       COUNT(c.id) as ciclos_total
                FROM usuarios u LEFT JOIN ciclos c ON u.wallet=c.wallet {periodo_filter}
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
        hoy_f  = "AND c.fecha=(NOW() AT TIME ZONE 'America/Mexico_City')::date"
        mes_f  = "AND DATE_TRUNC('month',c.creado_en AT TIME ZONE 'America/Mexico_City')=DATE_TRUNC('month',NOW() AT TIME ZONE 'America/Mexico_City')"
        result = {
            "hoy_ganancias":      query_lb("ganancia_total", hoy_f),
            "hoy_ciclos":         query_lb("ciclos_total",   hoy_f),
            "mes_ganancias":      query_lb("ganancia_total", mes_f),
            "mes_ciclos":         query_lb("ciclos_total",   mes_f),
            "historico_ganancias": query_lb("ganancia_total"),
            "historico_ciclos":   query_lb("ciclos_total"),
        }
        cur.close(); conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — MASTER / PADAWAN
# ══════════════════════════════════════════════════════════════
@app.route("/master/status", methods=["GET"])
def master_status():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM master_config WHERE id=1")
        mc = cur.fetchone()
        cur.execute("SELECT COUNT(*) as total FROM padawans WHERE activo=TRUE")
        padawans_activos = cur.fetchone()["total"]
        cur.close(); conn.close()
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
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet=%s", (wallet_master,))
        master_row = cur.fetchone()
        if not master_row or master_row["private_key_enc"] == "local":
            cur.close(); conn.close()
            return jsonify({"ok": False, "msg": "Wallet master no registrada con key en el servidor"})
        cur.execute("""
            UPDATE master_config SET activo=TRUE, wallet_master=%s, rango_bajo=%s,
            rango_alto=%s, amount_usdt=%s, stop_zona=%s, actualizado_en=NOW() WHERE id=1
        """, (wallet_master, rango_bajo, rango_alto, amount_usdt, stop_zona))
        cur.execute("SELECT wallet FROM padawans WHERE activo=TRUE")
        padawan_wallets = [r["wallet"] for r in cur.fetchall()]
        conn.commit(); cur.close(); conn.close()
        pk_master = fernet.decrypt(master_row["private_key_enc"].encode()).decode()
        with bots_lock:
            if wallet_master not in bots_activos or not bots_activos[wallet_master]["estado"]["activo"]:
                guardar_bot_activo(wallet_master, rango_bajo, rango_alto, amount_usdt, stop_zona)
                iniciar_bot_thread(wallet_master, pk_master, rango_bajo, rango_alto, amount_usdt, stop_zona)
        for i, w in enumerate(padawan_wallets):
            threading.Timer(i * 5, _arrancar_padawan,
                            args=[w, rango_bajo, rango_alto, amount_usdt, stop_zona]).start()
        return jsonify({"ok": True, "msg": f"Modo master iniciado! {len(padawan_wallets)} padawans arrancando."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/master/stop", methods=["POST"])
def master_stop():
    data = request.json or {}
    if data.get("password") != BOT_PASSWORD:
        return jsonify({"ok": False, "msg": "No autorizado"}), 401
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE master_config SET activo=FALSE, actualizado_en=NOW() WHERE id=1")
        cur.execute("SELECT wallet_master FROM master_config WHERE id=1")
        mc = cur.fetchone()
        cur.execute("SELECT wallet FROM padawans WHERE activo=TRUE")
        padawan_wallets = [r["wallet"] for r in cur.fetchall()]
        conn.commit(); cur.close(); conn.close()
        detenidos = 0
        wallets_a_detener = padawan_wallets[:]
        if mc and mc["wallet_master"]:
            wallets_a_detener.append(mc["wallet_master"])
        with bots_lock:
            for w in wallets_a_detener:
                if w in bots_activos and bots_activos[w]["estado"]["activo"]:
                    bots_activos[w]["stop_event"].set()
                    eliminar_bot_activo(w)
                    detenidos += 1
        return jsonify({"ok": True, "msg": f"Modo master detenido. {detenidos} padawans detenidos."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/padawan/activar/<wallet>", methods=["POST"])
def padawan_activar(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT * FROM master_config WHERE id=1 AND activo=TRUE")
        mc = cur.fetchone()
        if not mc:
            cur.close(); conn.close()
            return jsonify({"ok": False, "msg": "El modo master no esta activo"})
        cur.execute("INSERT INTO padawans (wallet, activo) VALUES (%s,TRUE) "
                    "ON CONFLICT (wallet) DO UPDATE SET activo=TRUE, actualizado_en=NOW()", (wallet,))
        conn.commit(); cur.close(); conn.close()
        _arrancar_padawan(wallet, mc["rango_bajo"], mc["rango_alto"], mc["amount_usdt"], mc["stop_zona"])
        return jsonify({"ok": True, "msg": "Modo padawan activado!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/padawan/desactivar/<wallet>", methods=["POST"])
def padawan_desactivar(wallet):
    wallet = wallet.lower()
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE padawans SET activo=FALSE, actualizado_en=NOW() WHERE wallet=%s", (wallet,))
        conn.commit(); cur.close(); conn.close()
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
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT activo FROM padawans WHERE wallet=%s", (wallet,))
        row = cur.fetchone()
        cur.execute("SELECT activo, rango_bajo, rango_alto, amount_usdt, wallet_master FROM master_config WHERE id=1")
        mc = cur.fetchone(); cur.close(); conn.close()
        return jsonify({
            "ok": True,
            "es_padawan":      bool(row and row["activo"]),
            "master_activo":   bool(mc and mc["activo"]),
            "es_wallet_master": bool(mc and mc["activo"] and mc["wallet_master"] and
                                    mc["wallet_master"].lower() == wallet),
            "master_config":   dict(mc) if mc else {}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def _arrancar_padawan(wallet, rango_bajo, rango_alto, amount_usdt_master, stop_zona):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT private_key_enc FROM usuarios WHERE wallet=%s", (wallet,))
        row = cur.fetchone(); cur.close(); conn.close()
        if not row or row["private_key_enc"] == "local":
            print(f"Padawan {wallet[:6]} sin key en servidor")
            return
        usdt_contract = get_w3().eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
        try:
            balance = llamada_rpc(lambda: usdt_contract.functions.balanceOf(
                Web3.to_checksum_address(wallet)).call()) / 10**6
        except:
            balance = 0
        capital = min(int(balance // 5) * 5, amount_usdt_master)
        if capital < 5:
            print(f"Padawan {wallet[:6]} sin capital suficiente")
            return
        pk = fernet.decrypt(row["private_key_enc"].encode()).decode()
        with bots_lock:
            if wallet in bots_activos and bots_activos[wallet]["estado"]["activo"]:
                return
        guardar_bot_activo(wallet, rango_bajo, rango_alto, capital, stop_zona)
        iniciar_bot_thread(wallet, pk, rango_bajo, rango_alto, capital, stop_zona)
        print(f"Padawan arrancado: {wallet[:6]} capital: ${capital}")
    except Exception as e:
        print(f"Error arrancando padawan {wallet[:6]}: {e}")

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — ADMIN
# ══════════════════════════════════════════════════════════════
@app.route("/admin", methods=["GET"])
def admin():
    if request.args.get("password") != BOT_PASSWORD:
        return jsonify({"error": "No autorizado"}), 401
    try:
        conn = get_db(); cur = conn.cursor()
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
        total_swaps_usdt     = float(cur.fetchone()["total"])
        comisiones_estimadas = total_swaps_usdt * 0.002
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
        # Info del pool de RPCs
        cur.execute("""
            SELECT COUNT(*) as total FROM bot_estados
            WHERE actualizado_en >= NOW() - INTERVAL '5 minutes' AND evento = 'online'
        """)
        bots_online_count = cur.fetchone()["total"]
        cur.close(); conn.close()
        for u in usuarios:
            w = u["wallet"].lower()
            with bots_lock:
                u["bot_activo"] = w in bots_activos and bots_activos[w]["estado"]["activo"]
        return jsonify({
            "resumen": {
                "total_usuarios":      total_usuarios,
                "bots_activos":        bots_en_memoria,
                "bots_online":         bots_online_count,
                "ciclos_totales":      ciclos_totales,
                "compras_totales":     compras_totales,
                "ventas_totales":      ventas_totales,
                "volumen_24h":         volumen_24h,
                "comisiones_estimadas": comisiones_estimadas,
            },
            "rpc_info": {
                "pool_size": len(RPC_POOL),
                "rpc_actual": get_rpc_url().split('/v2/')[0] + '/v2/***',
                "fallos": {k.split('/v2/')[0]: v for k, v in _rpc_fallos.items()},
            },
            "usuarios":       usuarios,
            "ultimos_ciclos": ultimos_ciclos,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/bots_online", methods=["GET"])
def bots_online():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT wallet, nombre, evento, version, actualizado_en
            FROM bot_estados
            WHERE actualizado_en >= NOW() - INTERVAL '5 minutes' AND evento = 'online'
            ORDER BY actualizado_en DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT COUNT(*) as total FROM bot_estados
            WHERE actualizado_en >= NOW() - INTERVAL '5 minutes' AND evento = 'online'
        """)
        total = cur.fetchone()["total"]
        cur.close(); conn.close()
        return jsonify({"ok": True, "bots_online": rows, "total": total})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — VERSIÓN Y UTILIDADES
# ══════════════════════════════════════════════════════════════
@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": VERSION_ACTUAL})

@app.route("/rpc/status", methods=["GET"])
def rpc_status():
    """Info del pool de RPCs para diagnóstico."""
    return jsonify({
        "ok":       True,
        "rpc_actual": get_rpc_url().split('/v2/')[0] + '/v2/***',
        "pool_size": len(RPC_POOL),
        "fallos":   {k.split('/v2/')[0]: v for k, v in _rpc_fallos.items()},
    })

# ══════════════════════════════════════════════════════════════
#  ENDPOINTS — CICLOS REPORTADOS (para compatibilidad)
# ══════════════════════════════════════════════════════════════
@app.route("/reportar_ciclo", methods=["POST"])
def reportar_ciclo():
    data   = request.json or {}
    wallet = data.get("wallet", "").lower()
    ciclo  = data.get("ciclo", {})
    if not wallet or not ciclo:
        return jsonify({"ok": False, "msg": "Faltan datos"})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO ciclos (wallet, fecha, hora_compra, precio_compra, hora_venta,
                                precio_venta, cnkt_comprado, cnkt_vendido, ganancia_usdt, amount_usdt)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (wallet, ciclo.get("fecha"), ciclo.get("hora_compra","previo"),
              ciclo.get("precio_compra",0), ciclo.get("hora_venta"),
              ciclo.get("precio_venta",0), ciclo.get("cnkt_comprado",0),
              ciclo.get("cnkt_vendido",0), ciclo.get("ganancia_usdt",0),
              ciclo.get("amount_usdt",0)))
        cur.execute("UPDATE usuarios SET ganancia_acumulada = ganancia_acumulada + %s WHERE wallet = %s",
                    (ciclo.get("ganancia_usdt",0), wallet))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/registro_externo", methods=["POST"])
def registro_externo():
    data   = request.json or {}
    wallet = data.get("wallet","").lower()
    nombre = data.get("nombre","Anonimo").strip() or "Anonimo"
    if not wallet:
        return jsonify({"ok": False, "msg": "Falta wallet"})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuarios (wallet, nombre, private_key_enc)
            VALUES (%s,%s,'local')
            ON CONFLICT (wallet) DO UPDATE SET nombre=EXCLUDED.nombre
        """, (wallet, nombre))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ══════════════════════════════════════════════════════════════
#  FRONTEND
# ══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    try:
        return open(os.path.join(BASE_DIR, "control.html"), encoding="utf-8").read(), \
               200, {"Content-Type": "text/html"}
    except:
        return jsonify({"msg": "EVOX Bot 2.0 API corriendo"})

@app.route("/admin-panel", methods=["GET"])
def admin_panel():
    try:
        return open(os.path.join(BASE_DIR, "admin.html"), encoding="utf-8").read(), \
               200, {"Content-Type": "text/html"}
    except Exception as e:
        return f"admin.html no encontrado: {e}", 404

for _img in ["icon", "evox", "charlie", "susan"]:
    def _make_route(name):
        @app.route(f"/{name}.png", methods=["GET"], endpoint=f"img_{name}")
        def _img_route():
            try:
                return open(os.path.join(BASE_DIR, f"{name}.png"), "rb").read(), \
                       200, {"Content-Type": "image/png"}
            except:
                return "", 404
    _make_route(_img)

# ══════════════════════════════════════════════════════════════
#  INICIO — corre tanto con gunicorn como directo
# ══════════════════════════════════════════════════════════════
init_db()

threading.Thread(target=loop_precio_global, daemon=True).start()
print("Loop de precio iniciado!")

threading.Thread(target=loop_balance_global, daemon=True).start()
print("Loop de balance global iniciado!")

print(f"Pool de RPCs: {len(RPC_POOL)} endpoints disponibles")
for i, rpc in enumerate(RPC_POOL):
    label = rpc.split('/v2/')[0] if '/v2/' in rpc else rpc
    print(f"  [{i+1}] {label}{'...' if '/v2/' in rpc else ''}")

restaurar_bots()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"EVOX Bot 2.0 corriendo en puerto {port}")
    app.run(host="0.0.0.0", port=port)