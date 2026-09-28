"""
Lâmpada Tuya falsa em 127.0.0.1, para testar o tinytuya de verdade sem hardware. Fala os protocolos
3.3, 3.4 (com a negociação da chave de sessão) e 3.5 (AES-GCM) usando as funções de empacotamento e de
criptografia do próprio tinytuya. Guarda cada comando recebido em `comandos`. Com manda_estado=False,
ela só confirma os comandos, sem mandar o estado de volta, como alguns firmwares fazem.
"""
import hmac
import json
import os
import socket
import struct
import threading
import time
from hashlib import sha256

CHAVE = "0123456789abcdef"
ID = "lampadafalsa00000001"
ESTADO_PADRAO = {"20": False, "21": "white", "22": 500, "23": 0, "24": "000003e803e8",
                 "25": "000e0d0000000000000000c80000", "26": 0}


class LampadaTuyaFalsa:
    def __init__(self, versao: float, dps: dict | None = None, chave: str = CHAVE, manda_estado: bool = True):
        from tinytuya.core import header
        from tinytuya.core.crypto_helper import AESCipher
        from tinytuya.core.message_helper import TuyaMessage, pack_message, parse_header, unpack_message
        self._h, self._aes, self._msg = header, AESCipher, TuyaMessage
        self._pack, self._unpack, self._parse = pack_message, unpack_message, parse_header
        self.versao = versao
        self.chave = chave.encode()
        self.manda_estado = manda_estado
        self.dps = dict(ESTADO_PADRAO if dps is None else dps)
        self.comandos: list[dict] = []
        self._servidor = socket.socket()
        self._servidor.bind(("127.0.0.1", 0))
        self._servidor.listen(5)
        self.porta = self._servidor.getsockname()[1]
        threading.Thread(target=self._aceitar, daemon=True).start()

    def parar(self) -> None:
        self._servidor.close()

    def _aceitar(self) -> None:
        while True:
            try:
                conexao, _ = self._servidor.accept()
            except OSError:
                return
            threading.Thread(target=_Conexao(self, conexao).rodar, daemon=True).start()


class _Conexao:
    def __init__(self, lampada: LampadaTuyaFalsa, sock: socket.socket):
        self.l, self.sock = lampada, sock
        self.chave = lampada.chave  # nos protocolos 3.4 e 3.5 vira a chave de sessão negociada
        self.seq = 100
        self.nonce_local = None
        self.nonce_remoto = os.urandom(16)
        self.cabecalho = {3.3: lampada._h.PROTOCOL_33_HEADER, 3.4: lampada._h.PROTOCOL_34_HEADER,
                          3.5: lampada._h.PROTOCOL_35_HEADER}[lampada.versao]

    def enviar(self, cmd: int, payload: bytes) -> None:
        l = self.l
        self.seq += 1
        if l.versao >= 3.5:
            msg = l._msg(self.seq, cmd, 0, payload, 0, True, l._h.PREFIX_6699_VALUE, True)
            dados = l._pack(msg, hmac_key=self.chave)
        else:
            msg = l._msg(self.seq, cmd, 0, struct.pack(">I", 0) + payload, 0, True, l._h.PREFIX_55AA_VALUE, False)
            dados = l._pack(msg, hmac_key=self.chave if l.versao >= 3.4 else None)
        self.sock.sendall(dados)

    def cifrar(self, bruto: bytes, com_cabecalho: bool) -> bytes:
        if self.l.versao == 3.3:
            cifrado = self.l._aes(self.chave).encrypt(bruto, False)
            return self.cabecalho + cifrado if com_cabecalho else cifrado
        if self.l.versao == 3.4:
            return self.l._aes(self.chave).encrypt(self.cabecalho + bruto if com_cabecalho else bruto, False)
        return self.cabecalho + bruto if com_cabecalho else bruto  # 3.5: o GCM é feito no empacotamento

    def decifrar(self, payload: bytes, com_cabecalho: bool) -> bytes:
        if self.l.versao == 3.3:
            if com_cabecalho:
                payload = payload[len(self.cabecalho):]
            return self.l._aes(self.chave).decrypt(payload, False, decode_text=False)
        bruto = self.l._aes(self.chave).decrypt(payload, False, decode_text=False) if self.l.versao == 3.4 else payload
        return bruto[len(self.cabecalho):] if com_cabecalho else bruto

    def estado_json(self, dps: dict, formato_novo: bool) -> bytes:
        if formato_novo:
            return json.dumps({"protocol": 4, "t": int(time.time()), "data": {"dps": dps}}).encode()
        return json.dumps({"devId": ID, "dps": dps, "t": int(time.time())}).encode()

    def tratar(self, msg) -> None:
        l, chave_original = self.l, self.l.chave
        if msg.cmd == 3:  # início da negociação da chave de sessão (3.4 e 3.5)
            nonce = msg.payload if l.versao >= 3.5 else l._aes(chave_original).decrypt(
                msg.payload, False, decode_text=False)
            self.nonce_local = nonce
            resposta = self.nonce_remoto + hmac.new(chave_original, nonce, sha256).digest()
            if l.versao == 3.4:
                resposta = l._aes(chave_original).encrypt(resposta, False)
            self.enviar(4, resposta)
        elif msg.cmd == 5:  # fim da negociação
            xor = bytes(a ^ b for a, b in zip(self.nonce_local, self.nonce_remoto))
            if l.versao == 3.4:
                self.chave = l._aes(chave_original).encrypt(xor, False, pad=False)
            else:
                self.chave = l._aes(chave_original).encrypt(xor, use_base64=False, pad=False,
                                                            iv=self.nonce_local[:12])[12:28]
        elif msg.cmd in (10, 16):  # leitura de estado
            self.enviar(msg.cmd, self.cifrar(self.estado_json(l.dps, False), False))
        elif msg.cmd in (7, 13):  # comando
            pedido = json.loads(self.decifrar(msg.payload, True))
            dps = pedido.get("dps") or pedido.get("data", {}).get("dps", {})
            l.comandos.append(dps)
            l.dps.update(dps)
            self.enviar(msg.cmd, b"")
            if l.manda_estado:
                self.enviar(8, self.cifrar(self.estado_json(dps, l.versao >= 3.4), True))
        elif msg.cmd == 9:
            self.enviar(9, b"")

    def rodar(self) -> None:
        l, buffer = self.l, b""
        while True:
            try:
                dados = self.sock.recv(4096)
            except OSError:
                break
            if not dados:
                break
            buffer += dados
            while len(buffer) >= 16:
                try:
                    cabecalho = l._parse(buffer)
                except Exception:
                    buffer = b""
                    break
                if len(buffer) < cabecalho.total_length:
                    break
                quadro, buffer = buffer[:cabecalho.total_length], buffer[cabecalho.total_length:]
                try:
                    msg = l._unpack(quadro, hmac_key=self.chave if l.versao >= 3.4 else None,
                                    header=cabecalho, no_retcode=True)
                    self.tratar(msg)
                except Exception:  # chave ou versão erradas do lado do cliente: fecha, como a lâmpada real
                    self.sock.close()
                    return
        self.sock.close()
