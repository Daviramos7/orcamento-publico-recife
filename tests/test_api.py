"""Testes da API contra um mini star schema (tests/fixtures/marts_minimo.sql).

Fixture (liquidado): Saúde jan 100 + fev 300 = 400 | Educação jan 100 + fev (-50 + 50 intra) = 100
Total = 500. Receita arrecadada: jan 200, fev 400 + 50 intra = 650.
"""
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from orcamento.config import dsn

BANCO_TESTE = "despesas_test"
D = Decimal


@pytest.fixture(scope="module")
def cliente(monkeypatch_module):
    try:
        with psycopg.connect(dsn(), autocommit=True, connect_timeout=3) as admin:
            admin.execute(f"drop database if exists {BANCO_TESTE} with (force)")
            admin.execute(f"create database {BANCO_TESTE}")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres indisponível: {e}")
    monkeypatch_module.setenv("PGDATABASE", BANCO_TESTE)
    with psycopg.connect(dsn(), autocommit=True) as conn:
        conn.execute((Path(__file__).parent / "fixtures" / "marts_minimo.sql").read_text("utf-8"))
    from orcamento.api.app import app
    return TestClient(app)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_orgaos_ranking_total_e_participacao(cliente):
    r = cliente.get("/despesas/orgaos").json()
    assert D(r["total_geral"]) == 500
    assert [(o["orgao_codigo"], D(o["valor"]), D(o["participacao_pct"])) for o in r["orgaos"]] == [
        ("48", 400, D("80.00")), ("14", 100, D("20.00"))]


def test_orgaos_filtra_periodo_e_medida(cliente):
    r = cliente.get("/despesas/orgaos", params={"mes_inicio": 1, "mes_fim": 1, "medida": "pago"}).json()
    assert D(r["total_geral"]) == 180  # 80 + 100


def test_periodo_invertido_e_medida_invalida_sao_rejeitados(cliente):
    assert cliente.get("/despesas/orgaos", params={"mes_inicio": 5, "mes_fim": 2}).status_code == 422
    assert cliente.get("/despesas/orgaos", params={"medida": "gasto"}).status_code == 422
    assert cliente.get("/despesas/orgaos", params={"mes_fim": 13}).status_code == 422


def test_evolucao_mensal_lag_e_acumulado(cliente):
    meses = cliente.get("/despesas/evolucao-mensal").json()["meses"]
    assert [(m["ano_mes"], D(m["valor"]), D(m["acumulado_ano"])) for m in meses] == [
        ("2023-01", 200, 200), ("2023-02", 300, 500)]
    assert meses[0]["variacao_abs"] is None
    assert D(meses[1]["variacao_abs"]) == 100 and D(meses[1]["variacao_pct"]) == D("50.00")


def test_evolucao_por_orgao_e_orgao_inexistente(cliente):
    r = cliente.get("/despesas/evolucao-mensal", params={"orgao_codigo": "14"}).json()
    assert r["orgao_nome"] == "SECRETARIA DE EDUCAÇÃO"
    assert [D(m["valor"]) for m in r["meses"]] == [100, 0]  # -50 + 50 no mês 2
    assert cliente.get("/despesas/evolucao-mensal", params={"orgao_codigo": "999"}).status_code == 404


def test_ranking_credores_consolida_por_nome_e_exclui_nao_informado(cliente):
    cred = cliente.get("/despesas/credores/ranking", params={"medida": "liquidado"}).json()["credores"]
    assert [(c["credor_nome"], c["credor_codigos"], D(c["valor"])) for c in cred] == [
        ("EMPRESA A", ["111", "222"], 400), ("EMPRESA B", ["333"], 150)]
    com = cliente.get("/despesas/credores/ranking",
                      params={"medida": "liquidado", "incluir_nao_informado": True}).json()["credores"]
    assert "CREDOR NÃO INFORMADO" in [c["credor_nome"] for c in com]


def test_funcoes(cliente):
    f = cliente.get("/despesas/funcoes").json()
    assert [(x["funcao_nome"], D(x["valor"])) for x in f["funcoes"]] == [("SAÚDE", 400), ("EDUCAÇÃO", 100)]


def test_orgaos_busca_sem_acento(cliente):
    assert [o["orgao_codigo"] for o in cliente.get("/orgaos", params={"busca": "saude"}).json()] == ["48"]


def test_metadados(cliente):
    m = cliente.get("/metadados").json()
    assert m["anos_disponiveis"] == [2023] and m["meses_disponiveis"] == [1, 2]
    assert m["tem_receitas"] is True


def test_receitas_origens_e_execucao(cliente):
    r = cliente.get("/receitas/origens").json()
    assert D(r["total_arrecadado"]) == 650 and D(r["total_previsto_atualizado"]) == 1000
    assert D(r["total_execucao_pct"]) == D("65.00")


def test_receitas_itens_ano_inexistente(cliente):
    assert cliente.get("/receitas/itens", params={"busca": "IPTU", "ano": 2022}).status_code == 404


def test_receitas_itens_por_sigla_e_vazio(cliente):
    iptu = cliente.get("/receitas/itens", params={"busca": "IPTU"}).json()["itens"]
    assert [(i["alinea_codigo"], D(i["arrecadado"])) for i in iptu] == [("50", 200)]
    assert cliente.get("/receitas/itens", params={"busca": "inexistente"}).json()["itens"] == []


def test_comparativo_exclui_intra_por_padrao(cliente):
    sem = cliente.get("/comparativo/receita-despesa").json()
    assert (D(sem["receita_arrecadada"]), D(sem["despesa"]), D(sem["saldo"])) == (600, 450, 150)
    com = cliente.get("/comparativo/receita-despesa",
                      params={"excluir_intraorcamentarias": False}).json()
    assert (D(com["receita_arrecadada"]), D(com["despesa"]), D(com["saldo"])) == (650, 500, 150)


@pytest.mark.parametrize("rota", [
    "/despesas/orgaos", "/despesas/evolucao-mensal", "/despesas/credores/ranking",
    "/despesas/funcoes", "/despesas/subfuncoes", "/despesas/tipos-de-gasto",
    "/receitas/origens", "/receitas/evolucao-mensal", "/comparativo/receita-despesa"])
def test_ano_inexistente_retorna_404_explicando(cliente, rota):
    r = cliente.get(rota, params={"ano": 2022})
    assert r.status_code == 404
    assert "2022" in r.json()["detail"] and "2023" in r.json()["detail"]
    assert cliente.get(rota, params={"ano": 2023}).status_code == 200


def test_ano_existente_nao_muda_resultado(cliente):
    sem = cliente.get("/despesas/orgaos").json()
    com = cliente.get("/despesas/orgaos", params={"ano": 2023}).json()
    assert sem == com


def test_ranking_credores_busca_por_nome_sem_acento(cliente):
    cred = cliente.get("/despesas/credores/ranking",
                       params={"medida": "liquidado", "busca": "empresa b"}).json()["credores"]
    assert [(c["credor_nome"], D(c["valor"])) for c in cred] == [("EMPRESA B", 150)]
    assert cred[0]["posicao"] == 2  # posição continua sendo a do ranking geral


@pytest.mark.parametrize("rota", [
    "/despesas/orgaos", "/despesas/evolucao-mensal", "/despesas/credores/ranking",
    "/despesas/funcoes", "/despesas/subfuncoes", "/despesas/tipos-de-gasto",
    "/receitas/origens", "/receitas/evolucao-mensal", "/comparativo/receita-despesa"])
def test_toda_resposta_diz_de_que_ano_sao_os_dados(cliente, rota):
    assert cliente.get(rota).json()["anos_dos_dados"] == [2023]


def test_comparativo_avisa_que_nao_sao_totais(cliente):
    sem = cliente.get("/comparativo/receita-despesa").json()
    assert "SEM" in sem["observacao"] and "não são os totais" in sem["observacao"]
    com = cliente.get("/comparativo/receita-despesa", params={"excluir_intraorcamentarias": False}).json()
    assert "COM" in com["observacao"]


# --------------------------------------------------------------- subfunção e tipo de gasto
# Fixture (liquidado): subfunção 131 = Saúde fev 300 + Educação 100 = 400 | 301 = Saúde jan 100.
# Tipo de gasto: Serv. publicidade 350 (100 + 300 - 50) | Publicidade oficial 50 | Combustíveis 100.
def test_subfuncoes_soma_a_mesma_subfuncao_de_funcoes_diferentes(cliente):
    r = cliente.get("/despesas/subfuncoes").json()
    assert [(x["subfuncao_codigo"], D(x["valor"])) for x in r["subfuncoes"]] == [("131", 400), ("301", 100)]
    assert D(r["total_geral"]) == 500 and r["total_filtrado"] is None
    assert D(r["subfuncoes"][0]["participacao_pct"]) == 80


def test_subfuncoes_dentro_de_uma_funcao_e_funcao_inexistente(cliente):
    r = cliente.get("/despesas/subfuncoes", params={"funcao_codigo": "10"}).json()
    assert r["funcao_nome"] == "SAÚDE"
    assert [(x["subfuncao_nome"], D(x["valor"])) for x in r["subfuncoes"]] == [
        ("COMUNICAÇÃO SOCIAL", 300), ("ATENÇÃO BÁSICA", 100)]
    assert cliente.get("/despesas/subfuncoes", params={"funcao_codigo": "99"}).status_code == 404


def test_subfuncoes_busca_sem_acento_traz_total_filtrado(cliente):
    r = cliente.get("/despesas/subfuncoes", params={"busca": "comunicacao"}).json()
    assert [x["subfuncao_codigo"] for x in r["subfuncoes"]] == ["131"]
    assert D(r["total_filtrado"]) == 400 and D(r["total_geral"]) == 500


def test_tipos_de_gasto_por_subelemento_e_elemento(cliente):
    r = cliente.get("/despesas/tipos-de-gasto").json()
    assert [(x["subelemento_nome"], D(x["valor"])) for x in r["itens"]] == [
        ("SERVIÇOS DE PUBLICIDADE E PROPAGANDA", 350), ("COMBUSTÍVEIS", 100), ("PUBLICIDADE OFICIAL/LEGAL", 50)]
    e = cliente.get("/despesas/tipos-de-gasto", params={"nivel": "elemento"}).json()
    assert [(x["elemento_nome"], x["subelemento_nome"], D(x["valor"])) for x in e["itens"]] == [
        ("OUTROS SERVIÇOS DE TERCEIROS - PESSOA JURÍDICA", None, 400), ("MATERIAL DE CONSUMO", None, 100)]


def test_tipos_de_gasto_busca_soma_todos_os_itens_do_tema(cliente):
    r = cliente.get("/despesas/tipos-de-gasto", params={"busca": "publicidade"}).json()
    assert len(r["itens"]) == 2 and D(r["total_filtrado"]) == 400 and D(r["total_geral"]) == 500
    vazio = cliente.get("/despesas/tipos-de-gasto", params={"busca": "helicoptero"}).json()
    assert vazio["itens"] == [] and D(vazio["total_filtrado"]) == 0 and D(vazio["total_geral"]) == 500


def test_tipos_de_gasto_por_orgao_e_nivel_invalido(cliente):
    r = cliente.get("/despesas/tipos-de-gasto", params={"orgao_codigo": "14"}).json()
    assert r["orgao_nome"] == "SECRETARIA DE EDUCAÇÃO"
    assert [(x["subelemento_nome"], D(x["valor"])) for x in r["itens"]] == [
        ("COMBUSTÍVEIS", 100), ("PUBLICIDADE OFICIAL/LEGAL", 50), ("SERVIÇOS DE PUBLICIDADE E PROPAGANDA", -50)]
    assert cliente.get("/despesas/tipos-de-gasto", params={"nivel": "acao"}).status_code == 422


def test_busca_aceita_plural_e_varias_palavras(cliente):
    # Bug pego pelo eval desp_combustivel: 'combustivel' não achava 'COMBUSTÍVEIS'.
    r = cliente.get("/despesas/tipos-de-gasto", params={"busca": "combustível"}).json()
    assert [x["subelemento_nome"] for x in r["itens"]] == ["COMBUSTÍVEIS"] and D(r["total_filtrado"]) == 100
    r = cliente.get("/despesas/tipos-de-gasto", params={"busca": "publicidade e propaganda"}).json()
    assert [x["subelemento_nome"] for x in r["itens"]] == ["SERVIÇOS DE PUBLICIDADE E PROPAGANDA"]
    r = cliente.get("/despesas/subfuncoes", params={"busca": "atencao basica"}).json()
    assert [x["subfuncao_codigo"] for x in r["subfuncoes"]] == ["301"]
