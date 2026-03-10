import os
import sys
import json
import threading
import time
import webbrowser
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

# ─── INSTANCIA ÚNICA (lock file) ─────────────────────────────────────────────
import socket as _socket

_lock_socket = None

def _adquirir_lock():
    """Evita múltiples instancias usando un socket en un puerto fijo."""
    global _lock_socket
    try:
        _lock_socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", 5099))
        return _lock_socket
    except OSError:
        print("=" * 50)
        print("  EVOX Bot ya esta corriendo!")
        print("  Cierra la instancia anterior primero.")
        print("=" * 50)
        import sys; sys.exit(0)


if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "evox_config.json")

# ─── SERVIDOR CENTRAL (Railway) ───────────────────────────────────────────────
CENTRAL_URL = os.environ.get("CENTRAL_URL", "https://evoxbot.evoxverse.com")

# ── SISTEMA DE VERSIONES ──────────────────────────────────────
VERSION_LOCAL = "1.0.0"
# ─────────────────────────────────────────────────────────────

# ─── CONTRATOS POLYGON ────────────────────────────────────────────────────────
USDT_ADDRESS  = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
CNKT_ADDRESS  = "0x87bdfbe98ba55104701b2f2e999982a317905637"
KYBER_ROUTER  = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
FEE_RECEIVER  = "0x1C02ADbA08aA59Be60fB6d4DD79eD82F986Df918"

TOKEN_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

# ─── RPCs POLYGON DISPONIBLES ─────────────────────────────────────────────────
RPC_PRESETS = [
    {"nombre": "PublicNode (recomendado)", "url": "https://polygon-bor-rpc.publicnode.com"},
    {"nombre": "1RPC (gratis)",            "url": "https://1rpc.io/matic"},
    {"nombre": "Blast (gratis)",           "url": "https://polygon-mainnet.blastapi.io"},
    {"nombre": "Alchemy (requiere key)",   "url": "https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY"},
    {"nombre": "QuickNode (requiere key)", "url": "https://your-endpoint.quiknode.pro/YOUR_KEY"},
]

CDMX = pytz.timezone('America/Mexico_City')

# ─── CONFIG LOCAL ─────────────────────────────────────────────────────────────
def cargar_config():
    defaults = {
        "rpc_url": "https://polygon-bor-rpc.publicnode.com",
        "encrypt_key": "",
        "wallet": "",
        "nombre": "",
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
    except:
        pass
    if not defaults["encrypt_key"]:
        defaults["encrypt_key"] = Fernet.generate_key().decode()
        guardar_config(defaults)
    return defaults

def guardar_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Error guardando config:", e)

config = cargar_config()

def get_fernet():
    key = config.get("encrypt_key", "")
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)

def get_rpc_url():
    return config.get("rpc_url", "https://polygon-rpc.com")

# ─── PRECIO GLOBAL ────────────────────────────────────────────────────────────
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
                precio_global["valor"]      = round(precio, 6)
                precio_global["actualizado"] = time.time()
        except Exception as e:
            print("Error precio:", e)
        time.sleep(15)

def get_precio_actual():
    with precio_lock:
        return precio_global["valor"]

# ─── WEB3 (se recrea si cambia el RPC) ────────────────────────────────────────
_w3      = None
_w3_lock = threading.Lock()
_w3_rpc  = None

def get_w3():
    global _w3, _w3_rpc
    rpc = get_rpc_url()
    with _w3_lock:
        if _w3 is None or _w3_rpc != rpc:
            _w3     = Web3(Web3.HTTPProvider(rpc))
            _w3_rpc = rpc
        return _w3

# ─── UTILIDADES ───────────────────────────────────────────────────────────────
def hora_cdmx():
    return datetime.now(CDMX).strftime('%H:%M:%S')

# ─── USUARIO LOCAL (guardado en evox_user.json encriptado) ───────────────────
USER_FILE = os.path.join(BASE_DIR, "evox_user.json")

def guardar_usuario_local(wallet, nombre, pk_enc, pwd_hash):
    try:
        data = {"wallet": wallet, "nombre": nombre, "pk_enc": pk_enc, "pwd_hash": pwd_hash}
        with open(USER_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Error guardando usuario:", e)

def cargar_usuario_local():
    try:
        if os.path.exists(USER_FILE):
            with open(USER_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return None

# ─── CICLOS / HISTORIAL LOCAL ─────────────────────────────────────────────────
HISTORIAL_FILE = os.path.join(BASE_DIR, "evox_historial.json")

def cargar_historial():
    try:
        if os.path.exists(HISTORIAL_FILE):
            with open(HISTORIAL_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {"ciclos": [], "ganancia_acumulada": 0}

def guardar_ciclo_local(ciclo):
    h = cargar_historial()
    h["ciclos"].insert(0, ciclo)
    h["ciclos"] = h["ciclos"][:200]
    h["ganancia_acumulada"] = h.get("ganancia_acumulada", 0) + ciclo["ganancia_usdt"]
    with open(HISTORIAL_FILE, "w") as f:
        json.dump(h, f, indent=2)
    return h["ganancia_acumulada"]

def reportar_ciclo_central(wallet, nombre, ciclo):
    try:
        requests.post(
            CENTRAL_URL + "/reportar_ciclo",
            json={"wallet": wallet, "nombre": nombre, "ciclo": ciclo},
            timeout=10
        )
    except:
        pass

# ─── BOT ──────────────────────────────────────────────────────────────────────
bots_activos = {}
bots_lock    = threading.Lock()

def nuevo_estado():
    return {
        "activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
        "ciclos": 0, "ganancia_total": 0, "ultimo_log": "", "logs": [], "cnkt_comprados": 0,
        "RANGO_BAJO": None, "RANGO_ALTO": None, "AMOUNT_USDT": None, "STOP_ZONA": None,
        "rpc_url": get_rpc_url(),
    }

def log_estado(estado, msg):
    hora  = hora_cdmx()
    linea = hora + " | " + msg
    print(linea)
    estado["ultimo_log"] = linea
    estado["logs"].append(linea)
    if len(estado["logs"]) > 100:
        estado["logs"] = estado["logs"][-100:]

def loop_bot(wallet, private_key, estado, stop_event):
    w3             = get_w3()
    account        = w3.eth.account.from_key(private_key)
    usdt_contract  = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
    cnkt_contract  = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)

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
        for token_name, contract, amount in [
            ("USDT", usdt_contract, 1000 * 10**6),
            ("CNKT", cnkt_contract, 10000000 * 10**18)
        ]:
            aprobado = False
            for intento in range(3):
                try:
                    gas_price = int(w3.eth.gas_price * 1.5)
                    tx = contract.functions.approve(KYBER_ROUTER, amount).build_transaction({
                        "from": account.address,
                        "nonce": w3.eth.get_transaction_count(account.address),
                        "gasPrice": gas_price, "chainId": 137
                    })
                    w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
                    log_estado(estado, token_name + " aprobado!")
                    aprobado = True
                    break
                except Exception as e:
                    if "429" in str(e):
                        espera = (intento + 1) * 20
                        log_estado(estado, "Rate limit RPC, reintentando en " + str(espera) + "s...")
                        stop_event.wait(espera)
                        if stop_event.is_set():
                            return False
                    else:
                        log_estado(estado, "Error aprobando " + token_name + ": " + str(e))
                        break
            if not aprobado:
                log_estado(estado, "No se pudo aprobar " + token_name + ". Cambia el RPC e intenta de nuevo.")
                return False
            stop_event.wait(15)
            if stop_event.is_set():
                return False
        return True

    def comprar():
        amount_in = int(AMOUNT_USDT * 10**6)
        route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
            params={"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": amount_in,
                    "feeAmount": "20", "isInBps": "true", "feeReceiver": FEE_RECEIVER,
                    "chargeFeeBy": "currency_in"}).json()
        build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
            json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
                  "recipient": account.address, "slippageTolerance": 50}).json()
        tx = {"from": account.address, "to": build['data']['routerAddress'],
              "data": build['data']['data'],
              "value": int(build['data']['transactionValue']),
              "nonce": w3.eth.get_transaction_count(account.address),
              "gasPrice": w3.eth.gas_price,
              "gas": int(build['data']['gas']) + 50000, "chainId": 137}
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
        log_estado(estado, "COMPRA: https://polygonscan.com/tx/" + tx_hash.hex())
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
        tx = {"from": account.address, "to": build['data']['routerAddress'],
              "data": build['data']['data'],
              "value": int(build['data']['transactionValue']),
              "nonce": w3.eth.get_transaction_count(account.address),
              "gasPrice": w3.eth.gas_price,
              "gas": int(build['data']['gas']) + 50000, "chainId": 137}
        tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
        log_estado(estado, "VENTA: https://polygonscan.com/tx/" + tx_hash.hex())
        stop_event.wait(15)
        return float(route['data']['routeSummary']['amountOut']) / 10**6

    log_estado(estado, "BOT INICIADO — " + wallet[:6] + "..." + wallet[-4:])
    log_estado(estado, "RPC: " + estado["rpc_url"])
    log_estado(estado, "Compra en: $" + str(RANGO_BAJO))
    log_estado(estado, "Vende en:  $" + str(RANGO_ALTO))
    log_estado(estado, "Capital:   $" + str(AMOUNT_USDT))

    if not aprobar_tokens():
        estado["activo"] = False
        return

    if stop_event.is_set():
        estado["activo"] = False
        return

    usdt_actual = get_balance_usdt()
    cnkt_actual = get_balance_cnkt()
    log_estado(estado, "USDT: $" + str(round(usdt_actual, 2)))
    log_estado(estado, "CNKT: " + str(round(cnkt_actual, 2)))

    if usdt_actual < AMOUNT_USDT and cnkt_actual < cnkt_necesario:
        log_estado(estado, "ERROR: No tienes USDT ni CNKT suficiente")
        estado["activo"] = False
        return

    h = cargar_historial()
    estado["ganancia_total"] = h.get("ganancia_acumulada", 0)

    hora_compra_actual  = None
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
            log_estado(estado, "$" + str(precio) +
                       " | USDT: $" + str(round(usdt, 2)) +
                       " | CNKT: " + str(round(cnkt, 0)) +
                       " | Modo: " + modo +
                       " | Ciclos: " + str(estado["ciclos"]))

            if precio < STOP_ABAJO or precio > STOP_ARRIBA:
                log_estado(estado, "PRECIO FUERA DE RANGO — BOT DETENIDO")
                estado["activo"] = False
                break

            if precio <= RANGO_BAJO and modo == "COMPRA":
                if usdt >= AMOUNT_USDT:
                    log_estado(estado, "Senal de COMPRA!")
                    hora_compra_actual   = hora_cdmx()
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
                    hora_venta    = hora_cdmx()
                    usdt_recibido = vender(cnkt_comp)
                    if stop_event.is_set():
                        break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ciclos"] += 1
                    ciclo = {
                        "fecha": datetime.now(CDMX).strftime('%Y-%m-%d'),
                        "hora_compra": hora_compra_actual or "previo",
                        "precio_compra": precio_compra_actual or 0,
                        "hora_venta": hora_venta,
                        "precio_venta": precio,
                        "cnkt_comprado": cnkt_comprado_actual,
                        "cnkt_vendido": cnkt_comp,
                        "ganancia_usdt": ganancia,
                        "amount_usdt": AMOUNT_USDT,
                    }
                    estado["ganancia_total"] = guardar_ciclo_local(ciclo)
                    usuario = cargar_usuario_local()
                    if usuario:
                        reportar_ciclo_central(wallet, usuario.get("nombre", "Anonimo"), ciclo)
                    log_estado(estado, "Ganancia ciclo: $" + str(round(ganancia, 4)))
                    log_estado(estado, "Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                    estado["cnkt_comprados"]  = 0
                    cnkt_comprado_actual       = 0
                    hora_compra_actual         = None
                    precio_compra_actual       = None

                elif cnkt_comp == 0 and cnkt >= cnkt_necesario:
                    log_estado(estado, "Senal de VENTA! (CNKT previo)")
                    hora_venta    = hora_cdmx()
                    usdt_recibido = vender(cnkt_necesario)
                    if stop_event.is_set():
                        break
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ciclos"] += 1
                    ciclo = {
                        "fecha": datetime.now(CDMX).strftime('%Y-%m-%d'),
                        "hora_compra": "previo",
                        "precio_compra": 0,
                        "hora_venta": hora_venta,
                        "precio_venta": precio,
                        "cnkt_comprado": 0,
                        "cnkt_vendido": cnkt_necesario,
                        "ganancia_usdt": ganancia,
                        "amount_usdt": AMOUNT_USDT,
                    }
                    estado["ganancia_total"] = guardar_ciclo_local(ciclo)
                    usuario = cargar_usuario_local()
                    if usuario:
                        reportar_ciclo_central(wallet, usuario.get("nombre", "Anonimo"), ciclo)
                    log_estado(estado, "Ganancia ciclo: $" + str(round(ganancia, 4)))
                    log_estado(estado, "Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                else:
                    log_estado(estado, "Esperando CNKT suficiente...")
            else:
                log_estado(estado, "Esperando...")

            stop_event.wait(INTERVALO)

        except Exception as e:
            log_estado(estado, "Error: " + str(e))
            stop_event.wait(30)

    estado["activo"] = False
    log_estado(estado, "Bot detenido.")

def iniciar_bot_thread(wallet, private_key, rango_bajo, rango_alto, amount_usdt, stop_zona):
    estado = nuevo_estado()
    estado["activo"]      = True
    estado["RANGO_BAJO"]  = rango_bajo
    estado["RANGO_ALTO"]  = rango_alto
    estado["AMOUNT_USDT"] = amount_usdt
    estado["STOP_ZONA"]   = stop_zona
    estado["rpc_url"]     = get_rpc_url()
    stop_event = threading.Event()
    t = threading.Thread(target=loop_bot, args=(wallet, private_key, estado, stop_event), daemon=True)
    t.start()
    with bots_lock:
        bots_activos[wallet] = {"thread": t, "stop_event": stop_event, "estado": estado}


# ─── PING DE ESTADO AL SERVIDOR ──────────────────────────────────────────────
def reportar_estado_central(wallet, nombre, evento):
    """Avisa al servidor cuando el bot se prende o apaga. evento: 'online'|'offline'"""
    try:
        requests.post(
            CENTRAL_URL + "/bot_estado",
            json={"wallet": wallet, "nombre": nombre, "evento": evento, "version": VERSION_LOCAL},
            timeout=5
        )
    except:
        pass

# ── SISTEMA DE VERSIONES ──────────────────────────────────────
def chequear_version():
    """Consulta el servidor central y avisa si hay nueva version disponible."""
    try:
        r = requests.get(CENTRAL_URL + "/version", timeout=8)
        data = r.json()
        version_servidor = data.get("version", "")
        if version_servidor and version_servidor != VERSION_LOCAL:
            print("=" * 50)
            print("  !NUEVA VERSION DISPONIBLE: " + version_servidor + "!")
            print("  Tu version: " + VERSION_LOCAL)
            print("  Descarga en: evoxbot.evoxverse.com")
            print("=" * 50)
        else:
            print("EVOX Bot v" + VERSION_LOCAL + " — version al dia.")
    except Exception as e:
        print("No se pudo verificar version: " + str(e))
# ─────────────────────────────────────────────────────────────

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

@app.route("/precio", methods=["GET"])
def precio_endpoint():
    precio = get_precio_actual()
    return jsonify({"ok": precio > 0, "precio": precio})

@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": VERSION_LOCAL})

@app.route("/rpc/list", methods=["GET"])
def rpc_list():
    return jsonify({
        "ok": True,
        "rpc_actual": get_rpc_url(),
        "presets": RPC_PRESETS
    })

@app.route("/rpc/test", methods=["POST"])
def rpc_test():
    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "msg": "URL vacia"})
    try:
        inicio = time.time()
        w3     = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
        bloque = w3.eth.block_number
        ms     = int((time.time() - inicio) * 1000)
        return jsonify({"ok": True, "msg": "RPC funciona!", "bloque": bloque, "latencia_ms": ms})
    except Exception as e:
        return jsonify({"ok": False, "msg": "RPC no responde: " + str(e)})

@app.route("/rpc/set", methods=["POST"])
def rpc_set():
    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "msg": "URL vacia"})
    config["rpc_url"] = url
    guardar_config(config)
    global _w3, _w3_rpc
    with _w3_lock:
        _w3     = None
        _w3_rpc = None
    return jsonify({"ok": True, "msg": "RPC actualizado: " + url})

@app.route("/registro", methods=["POST"])
def registro():
    data       = request.json or {}
    wallet     = data.get("wallet", "").lower()
    private_key = data.get("private_key", "")
    nombre     = data.get("nombre", "Anonimo").strip() or "Anonimo"
    password   = data.get("password", "")
    if not wallet or not private_key or not password:
        return jsonify({"ok": False, "msg": "Faltan campos obligatorios"})
    if len(password) < 6:
        return jsonify({"ok": False, "msg": "La contrasena debe tener al menos 6 caracteres"})
    try:
        w3      = get_w3()
        account = w3.eth.account.from_key(private_key)
        if account.address.lower() != wallet.lower():
            return jsonify({"ok": False, "msg": "La private key no corresponde a esta wallet"})
    except:
        return jsonify({"ok": False, "msg": "Private key invalida"})
    fernet   = get_fernet()
    pk_enc   = fernet.encrypt(private_key.encode()).decode()
    pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    guardar_usuario_local(wallet, nombre, pk_enc, pwd_hash)
    config["wallet"] = wallet
    config["nombre"] = nombre
    guardar_config(config)
    try:
        requests.post(CENTRAL_URL + "/registro_externo",
                      json={"wallet": wallet, "nombre": nombre}, timeout=8)
    except:
        pass
    return jsonify({"ok": True, "msg": "Usuario registrado!"})

@app.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    wallet   = data.get("wallet", "").lower()
    password = data.get("password", "")
    if not wallet or not password:
        return jsonify({"ok": False, "msg": "Faltan wallet y contrasena"})
    usuario = cargar_usuario_local()
    if not usuario or usuario.get("wallet") != wallet:
        return jsonify({"ok": False, "msg": "Wallet no registrada en este dispositivo"})
    if not bcrypt.checkpw(password.encode(), usuario["pwd_hash"].encode()):
        return jsonify({"ok": False, "msg": "Contrasena incorrecta"})
    return jsonify({"ok": True, "nombre": usuario["nombre"], "wallet": wallet})

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
                "rpc_url": est.get("rpc_url", get_rpc_url()),
                "wallet": wallet
            })
    h = cargar_historial()
    return jsonify({"activo": False, "modo": "COMPRA", "precio": 0, "usdt": 0, "cnkt": 0,
                    "ciclos": 0, "ganancia_total": h.get("ganancia_acumulada", 0),
                    "ultimo_log": "", "wallet": wallet,
                    "rpc_url": get_rpc_url(),
                    "config": {"RANGO_BAJO": None, "RANGO_ALTO": None,
                               "AMOUNT_USDT": None, "STOP_ZONA": None}})

@app.route("/logs/<wallet>", methods=["GET"])
def logs(wallet):
    wallet = wallet.lower()
    with bots_lock:
        if wallet in bots_activos:
            return jsonify({"logs": bots_activos[wallet]["estado"]["logs"]})
    return jsonify({"logs": []})

@app.route("/historial/<wallet>", methods=["GET"])
def historial(wallet):
    h = cargar_historial()
    return jsonify({"historial": h.get("ciclos", [])})

@app.route("/ciclos_hoy/<wallet>", methods=["GET"])
def ciclos_hoy(wallet):
    h    = cargar_historial()
    hoy  = datetime.now(CDMX).strftime('%Y-%m-%d')
    ciclos_de_hoy = [c for c in h.get("ciclos", []) if c.get("fecha") == hoy]
    return jsonify({
        "ok": True,
        "ciclos_hoy":    len(ciclos_de_hoy),
        "ciclos_total":  len(h.get("ciclos", [])),
        "ganancia_hoy":  sum(c["ganancia_usdt"] for c in ciclos_de_hoy),
        "ganancia_acum": h.get("ganancia_acumulada", 0),
    })

@app.route("/start/<wallet>", methods=["POST"])
def start(wallet):
    wallet  = wallet.lower()
    usuario = cargar_usuario_local()
    if not usuario or usuario.get("wallet") != wallet:
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
    min_pct  = 0.03 if amount_usdt <= 10 else 0.04
    pct_rango = (rango_alto - rango_bajo) / rango_bajo
    if pct_rango < min_pct:
        return jsonify({"ok": False, "msg": "Margen demasiado pequeño (minimo " + str(int(min_pct * 100)) + "%)"})
    try:
        fernet      = get_fernet()
        private_key = fernet.decrypt(usuario["pk_enc"].encode()).decode()
    except:
        return jsonify({"ok": False, "msg": "Error desencriptando key"})
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
    usuario_local = cargar_usuario_local()
    nombre = usuario_local.get("nombre", "Anonimo") if usuario_local else "Anonimo"
    reportar_estado_central(wallet, nombre, "offline")
    return jsonify({"ok": True, "msg": "Deteniendo bot..."})

# ─── SEÑALES (proxy al servidor central) ─────────────────────
@app.route("/senal", methods=["GET"])
def get_senal():
    try:
        r = requests.get(CENTRAL_URL + "/senal", timeout=8)
        return jsonify(r.json())
    except:
        return jsonify({"ok": False})

# ─── LEADERBOARD (proxy al servidor central) ──────────────────────────────────
@app.route("/leaderboard", methods=["GET"])
def leaderboard():
    try:
        r = requests.get(CENTRAL_URL + "/leaderboard", timeout=8)
        return jsonify(r.json())
    except:
        return jsonify({})

@app.route("/padawan/status/<wallet>", methods=["GET"])
def padawan_status(wallet):
    try:
        r = requests.get(CENTRAL_URL + "/padawan/status/" + wallet, timeout=8)
        return jsonify(r.json())
    except:
        return jsonify({"ok": False, "es_padawan": False, "master_activo": False, "es_wallet_master": False})

# ─── FRONTEND ─────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    html_path = os.path.join(BASE_DIR, "control.html")
    try:
        return open(html_path, encoding="utf-8").read(), 200, {"Content-Type": "text/html"}
    except:
        return "<h1>EVOX Bot corriendo</h1><p>control.html no encontrado</p>", 200, {"Content-Type": "text/html"}

for img in ["icon", "evox", "charlie", "susan"]:
    def make_route(name):
        @app.route(f"/{name}.png", methods=["GET"], endpoint=f"img_{name}")
        def img_route():
            try:
                return open(os.path.join(BASE_DIR, f"{name}.png"), "rb").read(), 200, {"Content-Type": "image/png"}
            except:
                return "", 404
    make_route(img)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _lock_handle = _adquirir_lock()
    t_precio = threading.Thread(target=loop_precio_global, daemon=True)
    t_precio.start()
    chequear_version()
    # Reportar que el bot está online
    usuario_local = cargar_usuario_local()
    if usuario_local:
        reportar_estado_central(
            usuario_local.get("wallet",""),
            usuario_local.get("nombre","Anonimo"),
            "online"
        )
    print("=" * 50)
    print("  EVOX Bot — corriendo localmente")
    print("  RPC: " + get_rpc_url())
    print("  Abriendo http://localhost:5000 ...")
    print("=" * 50)
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)