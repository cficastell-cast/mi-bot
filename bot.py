import os
from web3 import Web3
import requests
import time

# Conexion
RPC_URL = os.environ.get("RPC_URL")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Wallet
private_key = os.environ.get("PRIVATE_KEY")
account = w3.eth.account.from_key(private_key)

# Tokens
USDT_ADDRESS = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
CNKT_ADDRESS = "0x87bdfbe98ba55104701b2f2e999982a317905637"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"

# Configuracion (se define en Railway como variables de entorno)
RANGO_BAJO  = float(os.environ.get("RANGO_BAJO",  "0.001850"))
RANGO_ALTO  = float(os.environ.get("RANGO_ALTO",  "0.001901"))
AMOUNT_USDT = float(os.environ.get("AMOUNT_USDT", "10"))
STOP_ZONA   = float(os.environ.get("STOP_ZONA",   "0.03"))

STOP_ABAJO  = RANGO_BAJO * (1 - STOP_ZONA)
STOP_ARRIBA = RANGO_ALTO * (1 + STOP_ZONA)
INTERVALO   = 30

# ABI
TOKEN_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

usdt_contract = w3.eth.contract(address=USDT_ADDRESS, abi=TOKEN_ABI)
cnkt_contract = w3.eth.contract(address=Web3.to_checksum_address(CNKT_ADDRESS), abi=TOKEN_ABI)

if w3.is_connected():
    print("Conectado a Polygon!")
    print("Wallet: " + account.address)
else:
    print("Error de conexion")
    exit()

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
    print("Aprobando tokens...")
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
    print("USDT aprobado!")
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
    print("CNKT+ aprobado!")

def comprar():
    amount_in = int(AMOUNT_USDT * 10**6)
    route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
        params={"tokenIn": USDT_ADDRESS, "tokenOut": CNKT_ADDRESS, "amountIn": amount_in}).json()
    build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
        json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
              "recipient": account.address, "slippageTolerance": 50}).json()
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
    print("COMPRA: https://polygonscan.com/tx/" + tx_hash.hex())
    time.sleep(15)
    return float(route['data']['routeSummary']['amountOut']) / 10**18

def vender(cantidad_cnkt):
    amount_in = int(cantidad_cnkt * 10**18)
    route = requests.get("https://aggregator-api.kyberswap.com/polygon/api/v1/routes",
        params={"tokenIn": CNKT_ADDRESS, "tokenOut": USDT_ADDRESS, "amountIn": amount_in}).json()
    build = requests.post("https://aggregator-api.kyberswap.com/polygon/api/v1/route/build",
        json={"routeSummary": route['data']['routeSummary'], "sender": account.address,
              "recipient": account.address, "slippageTolerance": 50}).json()
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
    print("VENTA: https://polygonscan.com/tx/" + tx_hash.hex())
    time.sleep(15)
    return float(route['data']['routeSummary']['amountOut']) / 10**6

# Inicio del bot
print("CONFIGURACION:")
print("Compra en:   $" + str(RANGO_BAJO))
print("Vende en:    $" + str(RANGO_ALTO))
print("Stop abajo:  $" + str(round(STOP_ABAJO, 6)))
print("Stop arriba: $" + str(round(STOP_ARRIBA, 6)))
print("Capital:     $" + str(AMOUNT_USDT))

aprobar_tokens()

usdt_actual = get_balance_usdt()
cnkt_actual = get_balance_cnkt()
cnkt_necesario = AMOUNT_USDT / RANGO_ALTO
tiene_usdt = usdt_actual >= AMOUNT_USDT
tiene_cnkt = cnkt_actual >= cnkt_necesario

print("Balances detectados:")
print("USDT:  $" + str(round(usdt_actual, 2)))
print("CNKT+: " + str(round(cnkt_actual, 2)))

if not tiene_usdt and not tiene_cnkt:
    print("ERROR: No tienes USDT ni CNKT+ suficiente")
    exit()

precio_inicial = get_precio()
mitad_rango = (RANGO_BAJO + RANGO_ALTO) / 2

if precio_inicial >= mitad_rango:
    modo = "VENTA"
    print("Precio en zona alta - iniciando en modo VENTA")
else:
    modo = "COMPRA"
    print("Precio en zona baja - iniciando en modo COMPRA")

print("BOT INICIADO")
print("=" * 40)
ciclos = 0
cnkt_comprados = 0
ganancia_total_usdt = 0

while True:
    try:
        precio = get_precio()
        if precio < 0.000001:
            print("Precio invalido, reintentando...")
            time.sleep(10)
            continue

        usdt = get_balance_usdt()
        cnkt = get_balance_cnkt()
        hora = time.strftime('%H:%M:%S')
        print(hora + " | $" + str(round(precio,6)) + " | USDT: $" + str(round(usdt,2)) + " | CNKT+: " + str(round(cnkt,0)) + " | Modo: " + modo + " | Ciclos: " + str(ciclos))

        if precio < STOP_ABAJO or precio > STOP_ARRIBA:
            print("PRECIO FUERA DE RANGO - BOT DETENIDO")
            print("Ganancia total: $" + str(round(ganancia_total_usdt, 4)) + " USDT")
            break

        if precio <= RANGO_BAJO and modo == "COMPRA":
            if usdt >= AMOUNT_USDT:
                print("Senal de COMPRA!")
                cnkt_comprados = comprar()
                print("CNKT+ recibidos: " + str(round(cnkt_comprados, 2)))
                modo = "VENTA"
            else:
                print("Sin USDT, cambiando a modo VENTA")
                modo = "VENTA"

        elif precio >= RANGO_ALTO and modo == "VENTA":
            if cnkt >= cnkt_comprados and cnkt_comprados > 0:
                print("Senal de VENTA!")
                usdt_recibido = vender(cnkt_comprados)
                ganancia_ciclo = usdt_recibido - AMOUNT_USDT
                ganancia_total_usdt += ganancia_ciclo
                ciclos += 1
                print("Ganancia ciclo: $" + str(round(ganancia_ciclo, 4)) + " USDT")
                print("Ganancia total: $" + str(round(ganancia_total_usdt, 4)) + " USDT")
                print("Ciclo " + str(ciclos) + " completado!")
                cnkt_comprados = 0
                modo = "COMPRA"
            elif cnkt_comprados == 0:
                if cnkt >= cnkt_necesario:
                    print("Senal de VENTA!")
                    usdt_recibido = vender(cnkt_necesario)
                    ganancia_ciclo = usdt_recibido - AMOUNT_USDT
                    ganancia_total_usdt += ganancia_ciclo
                    ciclos += 1
                    print("Ganancia ciclo: $" + str(round(ganancia_ciclo, 4)) + " USDT")
                    print("Ganancia total: $" + str(round(ganancia_total_usdt, 4)) + " USDT")
                    print("Ciclo " + str(ciclos) + " completado!")
                    modo = "COMPRA"
                else:
                    print("Sin CNKT+, cambiando a modo COMPRA")
                    modo = "COMPRA"
            else:
                print("Sin CNKT+ suficiente, cambiando a modo COMPRA")
                modo = "COMPRA"

        else:
            print("Esperando...")

        time.sleep(INTERVALO)

    except Exception as e:
        print("Error: " + str(e))
        time.sleep(30)

