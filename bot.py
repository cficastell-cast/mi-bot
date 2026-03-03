import os
import threading
import time
import requests
from web3 import Web3
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "cnkt1234")

def check_auth():
    token = request.headers.get("X-Password") or request.args.get("password")
    return token == BOT_PASSWORD

RPC_URL = os.environ.get("RPC_URL")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

private_key = os.environ.get("PRIVATE_KEY")
account = w3.eth.account.from_key(private_key)

USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
CNKT_ADDRESS = "0x87bdfbe98ba55104701b2f2e999982a317905637"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
FEE_RECEIVER = "0x1C02ADbA08aA59Be60fB6d4DD79eD82F986Df918"

TOKEN_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

usdt_contract = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
cnkt_contract = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)

estado = {
    "activo": False,
    "modo": "COMPRA",
    "precio": 0,
    "usdt": 0,
    "cnkt": 0,
    "ciclos": 0,
    "ganancia_total": 0,
    "ultimo_log": "",
    "logs": [],
    "cnkt_comprados": 0,
    "RANGO_BAJO": None,
    "RANGO_ALTO": None,
    "AMOUNT_USDT": None,
    "STOP_ZONA": None,
}

bot_thread = None
stop_event = threading.Event()

def log(msg):
    hora = time.strftime('%H:%M:%S')
    linea = hora + " | " + msg
    print(linea)
    estado["ultimo_log"] = linea
    estado["logs"].append(linea)
    if len(estado["logs"]) > 100:
        estado["logs"] = estado["logs"][-100:]

def get_precio():
    params = {"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": 10 * 10**6}
    r = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes", params=params).json()
    amount_out = float(r['data']['routeSummary']['amountOut']) / 10**18
    amount_in_usd = float(r['data']['routeSummary']['amountInUsd'])
    return amount_in_usd / amount_out

def get_balance_usdt():
    return usdt_contract.functions.balanceOf(account.address).call() / 10**6

def get_balance_cnkt():
    return cnkt_contract.functions.balanceOf(account.address).call() / 10**18

def aprobar_tokens():
    log("Aprobando tokens...")
    gas_price = int(w3.eth.gas_price * 1.5)
    tx_usdt = usdt_contract.functions.approve(
        KYBER_ROUTER, 1000 * 10**6
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gasPrice": gas_price,
        "chainId": 137
    })
    w3.eth.send_raw_transaction(account.sign_transaction(tx_usdt).raw_transaction)
    time.sleep(15)
    log("USDT aprobado!")
    tx_cnkt = cnkt_contract.functions.approve(
        KYBER_ROUTER, 10000000 * 10**18
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gasPrice": gas_price,
        "chainId": 137
    })
    w3.eth.send_raw_transaction(account.sign_transaction(tx_cnkt).raw_transaction)
    time.sleep(15)
    log("CNKT aprobado!")

def comprar():
    amount_in = int(estado["AMOUNT_USDT"] * 10**6)
    route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
        params={
            "tokenIn": USDT_ADDRESS,
            "tokenOut": CNKT_ADDRESS,
            "amountIn": amount_in,
            "feeAmount": "20",
            "isInBps": "true",
            "feeReceiver": FEE_RECEIVER,
            "chargeFeeBy": "currency_in"
        }).json()
    build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
        json={
            "routeSummary": route['data']['routeSummary'],
            "sender": account.address,
            "recipient": account.address,
            "slippageTolerance": 50
        }).json()
    tx = {
        "from": account.address,
        "to": build['data']['routerAddress'],
        "data": build['data']['data'],
        "value": int(build['data']['transactionValue']),
        "nonce": w3.eth.get_transaction_count(account.address),
        "gasPrice": w3.eth.gas_price,
        "gas": int(build['data']['gas']) + 50000,
        "chainId": 137
    }
    tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    log("COMPRA: https://polygonscan.com/tx/" + tx_hash.hex())
    time.sleep(15)
    return float(route['data']['routeSummary']['amountOut']) / 10**18

def vender(cantidad_cnkt):
    amount_in = int(cantidad_cnkt * 10**18)
    route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
        params={
            "tokenIn": CNKT_ADDRESS,
            "tokenOut": USDT_ADDRESS,
            "amountIn": amount_in,
            "feeAmount": "20",
            "isInBps": "true",
            "feeReceiver": FEE_RECEIVER,
            "chargeFeeBy": "currency_in"
        }).json()
    build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
        json={
            "routeSummary": route['data']['routeSummary'],
            "sender": account.address,
            "recipient": account.address,
            "slippageTolerance": 50
        }).json()
    tx = {
        "from": account.address,
        "to": build['data']['routerAddress'],
        "data": build['data']['data'],
        "value": int(build['data']['transactionValue']),
        "nonce": w3.eth.get_transaction_count(account.address),
        "gasPrice": w3.eth.gas_price,
        "gas": int(build['data']['gas']) + 50000,
        "chainId": 137
    }
    tx_hash = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    log("VENTA: https://polygonscan.com/tx/" + tx_hash.hex())
    time.sleep(15)
    return float(route['data']['routeSummary']['amountOut']) / 10**6

def loop_bot():
    RANGO_BAJO  = estado["RANGO_BAJO"]
    RANGO_ALTO  = estado["RANGO_ALTO"]
    AMOUNT_USDT = estado["AMOUNT_USDT"]
    STOP_ZONA   = estado["STOP_ZONA"]
    STOP_ABAJO  = RANGO_BAJO * (1 - STOP_ZONA)
    STOP_ARRIBA = RANGO_ALTO * (1 + STOP_ZONA)
    INTERVALO   = 30

    cnkt_necesario = AMOUNT_USDT / RANGO_ALTO

    log("BOT INICIADO")
    log("Compra en: $" + str(RANGO_BAJO))
    log("Vende en:  $" + str(RANGO_ALTO))
    log("Capital:   $" + str(AMOUNT_USDT))

    aprobar_tokens()

    usdt_actual = get_balance_usdt()
    cnkt_actual = get_balance_cnkt()
    tiene_usdt = usdt_actual >= AMOUNT_USDT
    tiene_cnkt = cnkt_actual >= cnkt_necesario

    log("USDT: $" + str(round(usdt_actual, 2)))
    log("CNKT: " + str(round(cnkt_actual, 2)))

    if not tiene_usdt and not tiene_cnkt:
        log("ERROR: No tienes USDT ni CNKT suficiente")
        estado["activo"] = False
        return

    precio_inicial = get_precio()
    mitad_rango = (RANGO_BAJO + RANGO_ALTO) / 2
    if precio_inicial >= mitad_rango:
        estado["modo"] = "VENTA"
        log("Precio en zona alta - modo VENTA")
    else:
        estado["modo"] = "COMPRA"
        log("Precio en zona baja - modo COMPRA")

    while not stop_event.is_set():
        try:
            precio = get_precio()
            if precio < 0.000001:
                log("Precio invalido, reintentando...")
                time.sleep(10)
                continue

            usdt = get_balance_usdt()
            cnkt = get_balance_cnkt()
            modo = estado["modo"]

            estado["precio"] = round(precio, 6)
            estado["usdt"] = round(usdt, 2)
            estado["cnkt"] = round(cnkt, 2)

            log("$" + str(round(precio,6)) + " | USDT: $" + str(round(usdt,2)) + " | CNKT: " + str(round(cnkt,0)) + " | Modo: " + modo + " | Ciclos: " + str(estado["ciclos"]))

            if precio < STOP_ABAJO or precio > STOP_ARRIBA:
                log("PRECIO FUERA DE RANGO - BOT DETENIDO")
                log("Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                estado["activo"] = False
                break

            if precio <= RANGO_BAJO and modo == "COMPRA":
                if usdt >= AMOUNT_USDT:
                    log("Senal de COMPRA!")
                    estado["cnkt_comprados"] = comprar()
                    log("CNKT recibidos: " + str(round(estado["cnkt_comprados"], 2)))
                    estado["modo"] = "VENTA"
                else:
                    log("Sin USDT, cambiando a modo VENTA")
                    estado["modo"] = "VENTA"

            elif precio >= RANGO_ALTO and modo == "VENTA":
                cnkt_comp = estado["cnkt_comprados"]
                if cnkt >= cnkt_comp and cnkt_comp > 0:
                    log("Senal de VENTA!")
                    usdt_recibido = vender(cnkt_comp)
                    ganancia = usdt_recibido - AMOUNT_USDT
                    estado["ganancia_total"] += ganancia
                    estado["ciclos"] += 1
                    log("Ganancia ciclo: $" + str(round(ganancia, 4)))
                    log("Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                    estado["cnkt_comprados"] = 0
                    estado["modo"] = "COMPRA"
                elif cnkt_comp == 0:
                    if cnkt >= cnkt_necesario:
                        log("Senal de VENTA!")
                        usdt_recibido = vender(cnkt_necesario)
                        ganancia = usdt_recibido - AMOUNT_USDT
                        estado["ganancia_total"] += ganancia
                        estado["ciclos"] += 1
                        log("Ganancia ciclo: $" + str(round(ganancia, 4)))
                        log("Ganancia total: $" + str(round(estado["ganancia_total"], 4)))
                        estado["modo"] = "COMPRA"
                    else:
                        log("Sin CNKT, cambiando a modo COMPRA")
                        estado["modo"] = "COMPRA"
                else:
                    log("Sin CNKT suficiente, cambiando a modo COMPRA")
                    estado["modo"] = "COMPRA"
            else:
                log("Esperando...")

            time.sleep(INTERVALO)

        except Exception as e:
            log("Error: " + str(e))
            time.sleep(30)

    estado["activo"] = False
    log("Bot detenido.")

@app.route("/status", methods=["GET"])
def status():
    if not check_auth():
        return jsonify({"error": "No autorizado"}), 401
    return jsonify({
        "activo": estado["activo"],
        "modo": estado["modo"],
        "precio": estado["precio"],
        "usdt": estado["usdt"],
        "cnkt": estado["cnkt"],
        "ciclos": estado["ciclos"],
        "ganancia_total": estado["ganancia_total"],
        "ultimo_log": estado["ultimo_log"],
        "config": {
            "RANGO_BAJO": estado["RANGO_BAJO"],
            "RANGO_ALTO": estado["RANGO_ALTO"],
            "AMOUNT_USDT": estado["AMOUNT_USDT"],
            "STOP_ZONA": estado["STOP_ZONA"],
        },
        "wallet": account.address
    })

@app.route("/logs", methods=["GET"])
def logs():
    if not check_auth():
        return jsonify({"error": "No autorizado"}), 401
    return jsonify({"logs": estado["logs"]})

@app.route("/start", methods=["POST"])
def start():
    if not check_auth():
        return jsonify({"error": "No autorizado"}), 401
    global bot_thread
    if estado["activo"]:
        return jsonify({"ok": False, "msg": "El bot ya esta corriendo"})

    data = request.json or {}
    try:
        estado["RANGO_BAJO"]  = float(data["rango_bajo"])
        estado["RANGO_ALTO"]  = float(data["rango_alto"])
        estado["AMOUNT_USDT"] = float(data["amount_usdt"])
        estado["STOP_ZONA"]   = float(data.get("stop_zona", 0.03))
    except (KeyError, ValueError):
        return jsonify({"ok": False, "msg": "Faltan parametros: rango_bajo, rango_alto, amount_usdt"})

    estado["activo"] = True
    estado["ciclos"] = 0
    estado["ganancia_total"] = 0
    estado["cnkt_comprados"] = 0
    stop_event.clear()

    bot_thread = threading.Thread(target=loop_bot, daemon=True)
    bot_thread.start()
    return jsonify({"ok": True, "msg": "Bot iniciado!"})

@app.route("/stop", methods=["POST"])
def stop():
    if not check_auth():
        return jsonify({"error": "No autorizado"}), 401
    if not estado["activo"]:
        return jsonify({"ok": False, "msg": "El bot no esta corriendo"})
    stop_event.set()
    return jsonify({"ok": True, "msg": "Deteniendo bot..."})

@app.route("/config", methods=["POST"])
def config():
    data = request.json or {}
    for key in ["RANGO_BAJO", "RANGO_ALTO", "AMOUNT_USDT", "STOP_ZONA"]:
        if key.lower() in data:
            estado[key] = float(data[key.lower()])
    return jsonify({"ok": True, "msg": "Config actualizada"})

@app.route("/", methods=["GET"])
def home():
    try:
        return open("control.html").read(), 200, {"Content-Type": "text/html"}
    except:
        return jsonify({"msg": "CNKT Bot API corriendo", "wallet": account.address})

@app.route("/api", methods=["GET"])
def api_info():
    return jsonify({"msg": "CNKT Bot API corriendo", "wallet": account.address})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("API corriendo en puerto " + str(port))
    print("Wallet: " + account.address)
    app.run(host="0.0.0.0", port=port)

