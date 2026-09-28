"""
Testes do módulo da lâmpada (celular_servidor/lampada.py). As regras rodam sem rede, com um dispositivo
falso; os testes de ponta a ponta usam o tinytuya de verdade contra uma lâmpada Tuya falsa em 127.0.0.1.
"""
import importlib.util
import socket
import threading
import time
import unittest

from tests.auxiliares import PASTA_SERVIDOR

spec = importlib.util.spec_from_file_location("lampada_em_teste", PASTA_SERVIDOR / "lampada.py")
lampada = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lampada)

TEM_TINYTUYA = importlib.util.find_spec("tinytuya") is not None
EXEMPLO = {"id": "ID-DA-LAMPADA", "chave_local": "CHAVE-LOCAL-DA-LAMPADA", "ip": "IP-DA-LAMPADA",
           "versao": "VERSAO-DO-SCAN"}
VALIDA = {"id": "eb1234567890abcdef12", "chave_local": "0123456789abcdef", "ip": "192.168.0.20", "versao": "3.3"}
BRANCO = {"20": True, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"}
COR = {"20": True, "21": "colour", "22": 1000, "23": 0, "24": "00f003e80190"}  # azul com 40%


class TestConfig(unittest.TestCase):
    def test_sem_bloco_nao_ha_lampada(self):
        self.assertEqual(lampada.ler_config(None, EXEMPLO), (None, []))

    def test_valores_de_exemplo_ou_vazios_contam_como_nao_preenchidos(self):
        self.assertEqual(lampada.ler_config(dict(EXEMPLO), EXEMPLO), (None, list(lampada.CAMPOS)))
        # Hoje o dono tem só o id e a chave: o ip e a versão saem do scan, em casa.
        parcial = {**EXEMPLO, "id": VALIDA["id"], "chave_local": VALIDA["chave_local"], "ip": ""}
        self.assertEqual(lampada.ler_config(parcial, EXEMPLO), (None, ["ip", "versao"]))

    def test_config_valida(self):
        config, faltando = lampada.ler_config({**VALIDA, "id": f"  {VALIDA['id']} "}, EXEMPLO)
        self.assertEqual(faltando, [])
        self.assertEqual(config, {**VALIDA, "versao": 3.3})
        for versao in (3.5, "3.4", 3.1):
            with self.subTest(versao=versao):
                self.assertEqual(lampada.ler_config({**VALIDA, "versao": versao}, EXEMPLO)[0]["versao"], float(versao))

    def test_formato_errado_explica_o_campo(self):
        casos = [({"chave_local": "curta"}, '"chave_local" da lâmpada precisa ter 16 caracteres'),
                 ({"chave_local": "0123456789abcdeç"}, '"chave_local" da lâmpada precisa ter 16 caracteres'),
                 ({"ip": "192.168.0"}, '"ip" da lâmpada precisa ser o endereço dela na rede'),
                 ({"ip": "0.0.0.0"}, '"ip" da lâmpada precisa ser o endereço dela na rede'),  # o tinytuya varreria a rede
                 ({"ip": "224.0.0.1"}, '"ip" da lâmpada precisa ser o endereço dela na rede'),
                 ({"versao": "3.2"}, '"versao" da lâmpada precisa ser 3.1, 3.3, 3.4 ou 3.5'),
                 ({"versao": "três"}, '"versao" da lâmpada precisa ser 3.1, 3.3, 3.4 ou 3.5'),
                 ({"id": "eb12 34"}, '"id" da lâmpada precisa ser o "id" do devices.json')]
        for mudanca, mensagem in casos:
            with self.subTest(mudanca=mudanca), self.assertRaises(ValueError) as erro:
                lampada.ler_config({**VALIDA, **mudanca}, EXEMPLO)
            self.assertIn(mensagem, str(erro.exception))
        with self.assertRaises(ValueError):
            lampada.ler_config(["não", "é", "objeto"], EXEMPLO)


class TestComandos(unittest.TestCase):
    def test_cor_no_formato_do_dp_24(self):
        self.assertEqual(lampada.hsv16(240, 1000, 400), "00f003e80190")
        self.assertEqual(lampada.ler_hsv16("00f003e80190"), (240, 1000, 400))
        for invalido in (None, "", "00f003e8019", "zzzzzzzzzzzz", 123):
            self.assertIsNone(lampada.ler_hsv16(invalido))

    def test_desligar_ignora_o_resto(self):
        self.assertEqual(lampada.montar_comando(BRANCO, ligar=False, cor="azul"), ({"20": False}, "desliguei a lâmpada"))

    def test_ligar(self):
        self.assertEqual(lampada.montar_comando(BRANCO, ligar=True), ({"20": True}, "liguei a lâmpada"))

    def test_cor_mantem_o_brilho_atual(self):
        comando, feito = lampada.montar_comando(BRANCO, cor="azul")  # branco com brilho 500 -> azul com V 500
        self.assertEqual(comando, {"20": True, "21": "colour", "24": "00f003e801f4"})
        self.assertEqual(feito, "deixei a lâmpada em azul")

    def test_cor_com_brilho(self):
        comando, feito = lampada.montar_comando(BRANCO, cor="vermelho", brilho=30)
        self.assertEqual(comando, {"20": True, "21": "colour", "24": "000003e8012c"})
        self.assertEqual(feito, "deixei a lâmpada em vermelho com 30% de brilho")

    def test_brancos_usam_a_temperatura(self):
        for cor, temperatura in (("branco", 1000), ("branco frio", 1000), ("branco neutro", 500),
                                 ("branco quente", 0)):
            with self.subTest(cor=cor):
                comando, _ = lampada.montar_comando(COR, cor=cor)  # vindo do azul com 40%: mantém 40%
                self.assertEqual(comando, {"20": True, "21": "white", "22": 400, "23": temperatura})

    def test_so_brilho_respeita_o_modo_atual(self):
        self.assertEqual(lampada.montar_comando(BRANCO, brilho=80), ({"20": True, "22": 800},
                                                                     "mudei o brilho da lâmpada para 80%"))
        comando, _ = lampada.montar_comando(COR, brilho=10)  # no modo cor, muda só o V e mantém o azul
        self.assertEqual(comando, {"20": True, "24": "00f003e80064"})
        self.assertEqual(lampada.montar_comando(BRANCO, brilho=1)[0]["22"], 10)  # o mínimo da Tuya
        # Numa cena do app, o DP 22 não aparece na luz: volta para o branco com o brilho pedido.
        self.assertEqual(lampada.montar_comando({**BRANCO, "21": "scene"}, brilho=30)[0],
                         {"20": True, "21": "white", "22": 300})

    def test_modelo_sem_os_dps_v2(self):
        with self.assertRaises(lampada.ErroNaLampada) as erro:
            lampada.montar_comando({"1": True, "2": "white"}, ligar=True)
        self.assertIn("ainda não é suportado", str(erro.exception))

    def test_erros_do_tinytuya_viram_portugues(self):
        casos = [({"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None}, "não respondeu"),
                 ({"Error": "Network Error: Unable to Connect", "Err": "901", "Payload": None},
                  'não aceitou a conexão: confira se ela está ligada e na mesma rede, e se a "versao"'),
                 ({"Error": "Check device key or version", "Err": "914", "Payload": None}, 'confira a "chave_local"'),
                 ({"Error": "Unexpected Payload from Device", "Err": "904", "Payload": None}, 'confira a "chave_local"'),
                 ({"Error": "Function Not Supported by Device", "Err": "907", "Payload": None}, "o erro 907"),
                 (None, "não respondeu")]
        for resposta, trecho in casos:
            with self.subTest(resposta=resposta), self.assertRaises(lampada.ErroNaLampada) as erro:
                lampada.conferir(resposta)
            self.assertIn(trecho, str(erro.exception))
        self.assertEqual(lampada.conferir({"dps": {"20": True}}), {"dps": {"20": True}})


class DispositivoFalso:
    ECO = object()  # set_multiple_values devolve os próprios DPS, como quando a lâmpada manda o estado

    def __init__(self, estado=None, falha=None, resposta=ECO):
        self.estado = {"devId": "x", "dps": dict(BRANCO)} if estado is None else estado
        self.falha = falha
        self.resposta = resposta
        self.enviados = []

    def status(self):
        if self.falha:
            raise self.falha
        return self.estado

    def set_multiple_values(self, dps):
        self.enviados.append(dps)
        return {"dps": dps} if self.resposta is self.ECO else self.resposta


class TestLampadaComDispositivoFalso(unittest.TestCase):
    def criar(self, dispositivo):
        self.parametros = []
        return lampada.Lampada({**VALIDA, "versao": 3.3},
                               fabrica=lambda **kw: self.parametros.append(kw) or dispositivo)

    def test_controlar_le_o_estado_e_manda_um_comando_so(self):
        dispositivo = DispositivoFalso()
        feito = self.criar(dispositivo).controlar(cor="azul", brilho=50)
        self.assertEqual(feito, "deixei a lâmpada em azul com 50% de brilho")
        self.assertEqual(dispositivo.enviados, [{"20": True, "21": "colour", "24": "00f003e801f4"}])
        # Espera curta e só uma nova tentativa: com os padrões do tinytuya seriam até 90 s com a lâmpada desligada.
        self.assertEqual(self.parametros, [{"dev_id": VALIDA["id"], "address": "192.168.0.20",
                                            "local_key": VALIDA["chave_local"], "version": 3.3, "port": 6668,
                                            "connection_timeout": 2.0, "connection_retry_limit": 1,
                                            "connection_retry_delay": 0}])

    def test_estado(self):
        self.assertEqual(self.criar(DispositivoFalso()).estado(), BRANCO)

    def test_pedido_que_nao_muda_nada_nao_manda_comando(self):
        dispositivo = DispositivoFalso()  # já está ligada
        self.assertEqual(self.criar(dispositivo).controlar(ligar=True), "liguei a lâmpada")
        self.assertEqual(dispositivo.enviados, [])

    def test_confirmacao_sem_estado_conta_como_sucesso(self):
        # O tinytuya devolve None quando a lâmpada confirma o comando sem mandar o estado de volta.
        dispositivo = DispositivoFalso(resposta=None)
        self.assertEqual(self.criar(dispositivo).controlar(ligar=False), "desliguei a lâmpada")
        self.assertEqual(dispositivo.enviados, [{"20": False}])

    def test_comando_recusado(self):
        dispositivo = DispositivoFalso(resposta={"Error": "Check device key or version", "Err": "914", "Payload": None})
        with self.assertRaises(lampada.ErroNaLampada) as erro:
            self.criar(dispositivo).controlar(cor="verde")
        self.assertIn('confira a "chave_local"', str(erro.exception))

    def test_erro_ao_criar_o_dispositivo(self):
        def quebrar(**parametros):
            raise RuntimeError("Unable to find device on network (specify IP address)")

        objeto = lampada.Lampada({**VALIDA, "versao": 3.3}, fabrica=quebrar)
        for chamada in (objeto.estado, lambda: objeto.controlar(ligar=True)):
            with self.assertRaises(lampada.ErroNaLampada) as erro:
                chamada()
            self.assertEqual(str(erro.exception), "não consegui falar com a lâmpada")
            self.assertIn("Unable to find device", erro.exception.detalhe)

    def test_lampada_offline_nao_manda_comando(self):
        dispositivo = DispositivoFalso({"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None})
        with self.assertRaises(lampada.ErroNaLampada) as erro:
            self.criar(dispositivo).controlar(ligar=True)
        self.assertIn("confira se o interruptor dela está ligado", str(erro.exception))
        self.assertIn("905", erro.exception.detalhe)
        self.assertEqual(dispositivo.enviados, [])

    def test_excecao_do_tinytuya_vira_erro_em_portugues(self):
        dispositivo = DispositivoFalso(falha=RuntimeError("Bulb not configured"))
        with self.assertRaises(lampada.ErroNaLampada) as erro:
            self.criar(dispositivo).controlar(ligar=True)
        self.assertEqual(str(erro.exception), "não consegui falar com a lâmpada")
        self.assertIn("Bulb not configured", erro.exception.detalhe)

    def test_estado_sem_dps(self):
        with self.assertRaises(lampada.ErroNaLampada):
            self.criar(DispositivoFalso({"devId": "x"})).estado()

    def test_pedidos_simultaneos_sao_um_de_cada_vez(self):
        # A lâmpada aceita uma conexão por vez: dois pedidos juntos não podem se sobrepor.
        ativos, maximo, trava = [0], [0], threading.Lock()

        class Lento(DispositivoFalso):
            def status(self):
                with trava:
                    ativos[0] += 1
                    maximo[0] = max(maximo[0], ativos[0])
                time.sleep(0.05)
                with trava:
                    ativos[0] -= 1
                return super().status()

        objeto = lampada.Lampada({**VALIDA, "versao": 3.3}, fabrica=lambda **kw: Lento())
        threads = [threading.Thread(target=objeto.controlar, kwargs={"ligar": True}) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(maximo[0], 1)


@unittest.skipUnless(TEM_TINYTUYA, "instale celular_servidor/requirements.txt (tinytuya) para os testes de ponta a ponta")
class TestLampadaDePontaAPonta(unittest.TestCase):
    """O tinytuya de verdade, com criptografia e protocolo, contra a lâmpada falsa em 127.0.0.1."""

    def lampada_real(self, falsa, **mudancas):
        from tests.lampada_falsa import CHAVE, ID
        config = {"id": ID, "chave_local": CHAVE, "ip": "127.0.0.1", "versao": falsa.versao, **mudancas}
        return lampada.Lampada(config, porta=falsa.porta)

    def test_protocolos_3_3_3_4_e_3_5(self):
        from tests.lampada_falsa import LampadaTuyaFalsa
        for versao in (3.3, 3.4, 3.5):
            with self.subTest(versao=versao):
                falsa = LampadaTuyaFalsa(versao)
                self.addCleanup(falsa.parar)
                objeto = self.lampada_real(falsa)
                self.assertEqual(objeto.controlar(cor="azul", brilho=50), "deixei a lâmpada em azul com 50% de brilho")
                self.assertEqual(objeto.controlar(ligar=False), "desliguei a lâmpada")
                self.assertEqual(falsa.comandos, [{"20": True, "21": "colour", "24": "00f003e801f4"}, {"20": False}])
                self.assertEqual(objeto.estado()["20"], False)

    def test_lampada_que_so_confirma_o_comando(self):
        from tests.lampada_falsa import LampadaTuyaFalsa
        for versao in (3.3, 3.4, 3.5):
            with self.subTest(versao=versao):
                falsa = LampadaTuyaFalsa(versao, manda_estado=False)
                self.addCleanup(falsa.parar)
                objeto = self.lampada_real(falsa)
                self.assertEqual(objeto.controlar(ligar=True), "liguei a lâmpada")
                self.assertEqual(objeto.controlar(ligar=True), "liguei a lâmpada")  # já ligada: não manda de novo
                self.assertEqual(falsa.comandos, [{"20": True}])

    def test_chave_ou_versao_erradas(self):
        from tests.lampada_falsa import LampadaTuyaFalsa
        casos = [(3.3, {"chave_local": "fedcba9876543210"}, 'a lâmpada recusou a conexão: confira a "chave_local"'),
                 (3.3, {"versao": 3.4}, 'a lâmpada recusou a conexão: confira a "chave_local" e a "versao"'),
                 (3.4, {"versao": 3.3}, 'a lâmpada recusou a conexão: confira a "chave_local" e a "versao"'),
                 (3.5, {"versao": 3.3}, 'e se a "versao" no config_servidor.json é a do tinytuya scan')]
        for versao, mudanca, mensagem in casos:
            with self.subTest(lampada=versao, mudanca=mudanca):
                falsa = LampadaTuyaFalsa(versao)
                self.addCleanup(falsa.parar)
                with self.assertRaises(lampada.ErroNaLampada) as erro:
                    self.lampada_real(falsa, **mudanca).controlar(ligar=True)
                self.assertIn(mensagem, str(erro.exception))
                self.assertEqual(falsa.comandos, [], "com a chave ou a versão erradas, nada pode chegar à lâmpada")

    def test_lampada_desligada_responde_rapido(self):
        with socket.socket() as s:  # uma porta que ninguém escuta
            s.bind(("127.0.0.1", 0))
            porta = s.getsockname()[1]
        objeto = lampada.Lampada({**VALIDA, "ip": "127.0.0.1", "versao": 3.3}, porta=porta)
        inicio = time.monotonic()
        with self.assertRaises(lampada.ErroNaLampada) as erro:
            objeto.controlar(ligar=True)
        # O Linux recusa na hora (erro 901) e o Windows insiste até o tempo acabar (905): as duas
        # mensagens mandam conferir se a lâmpada está ligada e na mesma rede.
        self.assertIn("na mesma rede", str(erro.exception))
        self.assertLess(time.monotonic() - inicio, 6, "com os padrões do tinytuya seriam até 90 s")


if __name__ == "__main__":
    unittest.main()
