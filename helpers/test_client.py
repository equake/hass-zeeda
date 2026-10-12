#!/usr/bin/env python3
"""
Cliente de validação da API Zeeda (GWM) — engenharia reversa do APK oficial 1.4.0.

Objetivo: confirmar, contra o servidor REAL, o protocolo extraído estaticamente do app
(login por token + endpoints REST + WebSocket de tempo real), usando SUA conta.

Uso:
    pip install pycryptodome            # para o AES da senha (ou: pip install cryptography)
    pip install websocket-client        # opcional, só para --ws

    export ZEEDA_EMAIL="seu@email.com"
    export ZEEDA_PASSWORD="suasenha"
    python3 zeeda_client.py             # login + lista carregadores + status
    python3 zeeda_client.py --ws        # além disso, escuta o websocket de tempo real

Nada aqui é destrutivo: só faz login e leitura (GET de status/listagem).
"""

import argparse
import binascii
import json
import os
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Constantes extraídas do APK
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("ZEEDA_BASE_URL", "https://hwiot.shzhida.com/")
WS_URL_TMPL = "wss://hwiot.shzhida.com/ws/charge-service/websocket/user/{device_code}"

# LoginFragment.toLogin(): AesUtil.encrypt(pwd, "WXZhidaCX_V1.0.0", "20180101Abcdefg!")
# AesUtil: AES/CBC/PKCS5Padding, chave e IV usados como bytes UTF-8 (já têm 16 chars),
# saída em hex.
AES_KEY = b"WXZhidaCX_V1.0.0"
AES_IV = b"20180101Abcdefg!"

USER_AGENT = "okhttp/4.11.0"  # UA padrão do OkHttp usado pelo app (não há UA customizado)


# ---------------------------------------------------------------------------
# AES da senha (tenta pycryptodome, depois cryptography)
# ---------------------------------------------------------------------------
def _aes_cbc_encrypt(data: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(data)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
        return enc.update(data) + enc.finalize()
    except ImportError:
        sys.exit("Falta um backend de AES. Rode: pip install pycryptodome  (ou: pip install cryptography)")


def encrypt_password(plaintext: str) -> str:
    raw = plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad  # PKCS5/PKCS7
    # HexUtil.byte2hex do app usa .toUpperCase() — o servidor compara a string hex,
    # então o case importa. Tem que ser MAIÚSCULO.
    return binascii.hexlify(_aes_cbc_encrypt(raw)).decode().upper()


# ---------------------------------------------------------------------------
# Camada HTTP (envelope Request + header Authorization)
# ---------------------------------------------------------------------------
class ZeedaClient:
    def __init__(self, base_url=BASE_URL, language="en", verbose=False):
        self.base = base_url.rstrip("/") + "/"
        self.language = language
        self.token = None
        self.verbose = verbose

    def _post(self, path, data=None, page=None):
        envelope = {"accessType": "android", "data": data if data is not None else {}}
        if page is not None:
            envelope["page"] = page
        body = json.dumps(envelope).encode()
        headers = {
            "Content-Type": "application/json",
            "Language": self.language,
            "accessType": "android",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = self.token
        url = self.base + path.lstrip("/")
        if self.verbose:
            print(f"--> POST {url}\n    {body.decode()}", file=sys.stderr)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            auth = resp.headers.get("Authorization")
            payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code} em {path}: {e.read().decode()[:500]}")
        # O app também captura o token do header Authorization da resposta.
        if auth:
            self.token = auth
        return payload

    # --- fluxo do app ------------------------------------------------------
    def resolve_server(self, timezone):
        """
        Replica o SplashActivity: pergunta ao servidor global qual backend usar
        para o fuso informado e troca a base URL para o retornado.
        Body: {"accessType":"android","data":"America/Sao_Paulo"}
        Não é autenticado e NÃO gasta tentativa de login.
        """
        resp = self._post("charge-service/app/getServerUrlByTimeZone", timezone)
        data = resp.get("data") if isinstance(resp, dict) else None
        server_url = None
        if isinstance(data, dict):
            server_url = data.get("serverUrl") or data.get("server_url") or data.get("url")
        elif isinstance(data, str):
            server_url = data
        if server_url:
            self.base = server_url.rstrip("/") + "/"
            print(f"Servidor resolvido p/ fuso {timezone}: {self.base}")
        else:
            print(f"getServerUrlByTimeZone não retornou serverUrl. Resposta: "
                  f"{json.dumps(resp, ensure_ascii=False)[:400]}")
        return resp

    def login(self, email, password):
        """POST user-service/app/loginByEmail"""
        resp = self._post("user-service/app/loginByEmail", {
            "account": email,
            "accountType": "email",
            "password": encrypt_password(password),
        })
        # Resposta tende a ser {code,msg,data:{token,expireTime,...}} ou o LoginBean direto.
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        token = (data or {}).get("token") if isinstance(data, dict) else None
        if token:
            # token do corpo tem prioridade; alguns backends mandam sem o prefixo "Bearer"
            self.token = token
        if not self.token:
            raise SystemExit(f"Login sem token. Resposta: {json.dumps(resp, ensure_ascii=False)[:600]}")
        return resp

    def get_user_info(self):
        # data = token (string), igual ao app (LoginPresent.getPersonInfo)
        return self._post("user-service/app/getUserInfo", self.token)

    def get_user_charge(self, user_id, org_id):
        # data = {userId, orgId}, igual ao PilePresent.getPersonPile
        return self._post("charge-service/app/getUserCharge",
                          {"userId": user_id, "orgId": org_id})

    def get_charge_point_detail(self, charge_point_code):
        # data = chargePointCode (STRING pura), não objeto
        return self._post("charge-service/app/getChargePointDetail", charge_point_code)

    def get_charge_status_info(self, charge_point_code, connector_id=1):
        # data = {deviceCode, connectorId}  (ChargeDetailActivity, linha ~1595)
        return self._post("charge-service/charge/getChargeStatusInfo",
                          {"deviceCode": charge_point_code, "connectorId": connector_id})

    def get_current_list(self, charge_point_code):
        # data = chargePointCode (string) — valores de corrente/medição ao vivo
        return self._post("charge-service/app/currentList", charge_point_code)

    def get_charging_duration(self, charge_point_code):
        # data = chargePointCode (string)
        return self._post("charge-service/app/getChargingDuration", charge_point_code)


def _pp(title, obj):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:4000])


def _extract_device_codes(user_charge_resp):
    """Tenta achar os códigos de carregador na resposta de getUserCharge (campo exato varia)."""
    codes = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("chargePointCode", "deviceCode", "pileCode") and isinstance(v, str):
                    codes.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(user_charge_resp)
    return list(dict.fromkeys(codes))


def listen_ws(token, device_code, language="en"):
    try:
        import websocket  # websocket-client
    except ImportError:
        print("\n[--ws] Instale: pip install websocket-client", file=sys.stderr)
        return
    url = WS_URL_TMPL.format(device_code=device_code)
    header = [f"Authorization: {token}", "accessType: android", f"Language: {language}"]
    print(f"\n===== WebSocket =====\nConectando: {url}")

    def on_message(ws, msg):
        print("<<", msg)

    def on_open(ws):
        print("WS aberto. Aguardando mensagens (Ctrl-C para sair)...")

    def on_error(ws, err):
        print("WS erro:", err)

    ws = websocket.WebSocketApp(url, header=header,
                                on_open=on_open, on_message=on_message, on_error=on_error)
    ws.run_forever(ping_interval=30)  # app manda heartbeat a cada 30s


def main():
    ap = argparse.ArgumentParser(description="Validador da API Zeeda")
    ap.add_argument("--email", default=os.environ.get("ZEEDA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("ZEEDA_PASSWORD"))
    ap.add_argument("--token", default=os.environ.get("ZEEDA_TOKEN"),
                    help="usa um token existente (de spUtils.xml) e pula o login")
    ap.add_argument("--language", default="en")
    ap.add_argument("--timezone", default=os.environ.get("ZEEDA_TZ", "America/Sao_Paulo"),
                    help="fuso p/ resolver o servidor (igual TimeZone.getDefault().getID())")
    ap.add_argument("--no-resolve", action="store_true",
                    help="não resolver servidor por fuso; usar a base URL padrão/--base-url")
    ap.add_argument("--base-url", default=os.environ.get("ZEEDA_BASE_URL"),
                    help="força uma base URL REST específica")
    ap.add_argument("--ws", action="store_true", help="escutar o websocket de tempo real")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    c = ZeedaClient(language=args.language, verbose=args.verbose)

    # Descobrir o servidor certo para o fuso (como o app faz no splash),
    # a não ser que uma base URL tenha sido forçada.
    if args.base_url:
        c.base = args.base_url.rstrip("/") + "/"
        print("Base URL forçada:", c.base)
    elif not args.no_resolve:
        c.resolve_server(args.timezone)

    if args.token:
        # Caminho recomendado: token tirado de spUtils.xml, pula o login (não gasta tentativa).
        c.token = args.token.strip()
        print("Usando token fornecido (login pulado).")
    else:
        if not args.email or not args.password:
            sys.exit("Defina ZEEDA_EMAIL/ZEEDA_PASSWORD, ou passe --token (de spUtils.xml).")
        # O app faz .trim() no e-mail e na senha (LoginFragment.toLogin).
        args.email = args.email.strip()
        args.password = args.password.strip()
        print("Senha cifrada (AES hex):", encrypt_password(args.password)[:32], "...")
        _pp("LOGIN", c.login(args.email, args.password))
        print("\nToken:", (c.token or "")[:40], "...")

    info = c.get_user_info()
    _pp("USER INFO", info)
    uinfo = info.get("data", {}) if isinstance(info, dict) else {}
    user_id = uinfo.get("userId")
    org_id = uinfo.get("orgId", 0)
    print(f"\nuserId={user_id} orgId={org_id}")

    chargers = c.get_user_charge(user_id, org_id)
    _pp("MEUS CARREGADORES", chargers)

    # extrair os códigos SÓ do data de sucesso (não do envelope de erro)
    codes = []
    if isinstance(chargers, dict) and chargers.get("success"):
        codes = _extract_device_codes(chargers.get("data"))
    print("\nCódigos detectados:", codes or "(nenhum — veja o JSON acima p/ ajustar o campo)")

    for code in codes[:3]:
        _pp(f"DETALHE {code}", c.get_charge_point_detail(code))
        _pp(f"STATUS {code}", c.get_charge_status_info(code, connector_id=1))
        _pp(f"CURRENT LIST {code}", c.get_current_list(code))
        _pp(f"CHARGING DURATION {code}", c.get_charging_duration(code))

    if args.ws and codes:
        listen_ws(c.token, codes[0], args.language)


if __name__ == "__main__":
    main()
