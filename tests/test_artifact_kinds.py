"""Os quatro tipos de artefato, cada um lido por quem sabe lê-lo.

`collect-artifacts` coleta script Glue, script SageMaker, SQL do Athena e definição
ASL. Só o pacote da análise contextual recebia os quatro; o pipeline determinístico
carregava dois, e os outros existiam sem que ninguém dissesse quantos vieram de
quantos esperados — um bundle sem nenhuma SQL e um bundle completo produziam o mesmo
silêncio.

E havia um caminho pelo qual SQL entrava no scanner de AST: `glue/code/rules.py`
confiava em `job_by_name` devolver `None` em vez de filtrar por `kind`, então bastava
um `query_id` coincidir com o nome de um job.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from julius.analysis.context_builder import _artifact_kind, _signals_context
from julius.collection.artifacts import CodeArtifact
from julius.findings.signal import Signal
from julius.knowledge.rules.glue.code import rules as glue_code


def _bundle(tmp_path: Path, account_id: str, artefatos: list[dict]) -> Path:
    raiz = tmp_path / "artifacts"
    raiz.mkdir(parents=True, exist_ok=True)
    entradas = []
    for item in artefatos:
        nome = f"{item['kind']}-{item['asset_name']}.txt"
        (raiz / nome).write_text(item["content"], encoding="utf-8")
        entradas.append(
            {
                "kind": item["kind"],
                "asset_name": item["asset_name"],
                "file": nome,
                "source": "teste",
                "sha256": hashlib.sha256(
                    item["content"].encode("utf-8")
                ).hexdigest(),
                "truncated": False,
            }
        )
    manifesto = raiz / "manifest.json"
    manifesto.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "account_id": account_id,
                "read_only": True,
                "artifacts": entradas,
            }
        ),
        encoding="utf-8",
    )
    return manifesto


def test_sql_never_enters_the_python_scanner(monkeypatch):
    """O acidente que o filtro de `kind` fecha.

    Uma consulta Athena cujo `query_id` coincida com o nome de um job Glue passava
    por `job_by_name` e ia inteira para `ast.parse`.
    """
    chamadas = []

    def espia(source):
        chamadas.append(source)
        return []

    monkeypatch.setattr(glue_code, "scan_glue_script", espia)

    class _Job:
        name = "colisao"
        script_location = "s3://b/k.py"
        job_bookmark = False

    class _Conta:
        account_id = "123456789012"

        def job_by_name(self, nome):
            return _Job() if nome == "colisao" else None

    sql = CodeArtifact(
        asset_name="colisao",
        source="athena://wg/colisao",
        content="SELECT * FROM tabela",
        sha256="a" * 64,
        kind="athena_sql",
    )
    glue_code.detect(_Conta(), [sql], None, "scan")
    assert chamadas == [], "SQL foi entregue ao scanner de AST do Glue"


def test_a_glue_script_still_reaches_the_scanner(monkeypatch):
    """A outra metade: o filtro não pode ter fechado o caminho legítimo."""
    chamadas = []
    monkeypatch.setattr(
        glue_code, "scan_glue_script", lambda source: chamadas.append(source) or []
    )

    class _Job:
        name = "etl"
        script_location = "s3://b/k.py"
        job_bookmark = False

    class _Conta:
        account_id = "123456789012"

        def job_by_name(self, nome):
            return _Job() if nome == "etl" else None

    script = CodeArtifact(
        asset_name="etl",
        source="s3://b/k.py",
        content="import pyspark",
        sha256="b" * 64,
        kind="glue_script",
    )
    glue_code.detect(_Conta(), [script], None, "scan")
    assert chamadas == ["import pyspark"]


def test_the_pipeline_reports_coverage_for_all_four_kinds(tmp_path):
    from julius.collection.normalizers import load_account
    from julius.pipeline import analyze

    conta = load_account("data/sample/consumer-avi.json")
    manifesto = _bundle(
        tmp_path,
        conta.account_id,
        [{"kind": "glue_script", "asset_name": "agrega_vendas", "content": "import pyspark"}],
    )
    analise = analyze("data/sample/consumer-avi.json", artifacts_manifest=str(manifesto))
    fontes = {item.source for item in analise.account.collection_health}
    assert {"Glue Scripts", "SageMaker Scripts", "Athena SQL", "Step Functions ASL"} <= fontes


def test_the_hash_is_matched_by_kind_and_name(tmp_path):
    """Nome não é único entre tipos, e o `dict` guardava o último a entrar.

    Uma máquina de estados e um job Glue podem se chamar igual. O sinal receberia o
    hash do artefato errado, o validador exigiria esse hash de volta, e a análise
    concluiria sobre um arquivo que não é o dela.
    """

    class _Analise:
        signals = [
            Signal(
                kind="config",
                rule_id="SFN-STANDARD-TO-EXPRESS",
                asset_type="state_machine",
                asset_name="homonimo",
                observation="",
                question="",
            )
        ]

    artefatos = [
        {"kind": "glue_script", "asset_name": "homonimo", "sha256": "g" * 64},
        {"kind": "stepfunctions_asl", "asset_name": "homonimo", "sha256": "s" * 64},
    ]
    saida = _signals_context(_Analise(), artefatos)
    assert saida[0]["artifact_sha256"] == "s" * 64


def test_each_asset_type_knows_its_artifact_kind():
    assert _artifact_kind("glue_job") == "glue_script"
    assert _artifact_kind("athena_query") == "athena_sql"
    assert _artifact_kind("state_machine") == "stepfunctions_asl"
    assert _artifact_kind("sagemaker_training_job") == "sagemaker_script"
    assert _artifact_kind("redshift_cluster") == ""
