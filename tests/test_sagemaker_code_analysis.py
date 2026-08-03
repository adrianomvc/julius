"""Análise de código do SageMaker: coleta, gate e o que ela recusa a afirmar.

O serviço cobra instância-hora, e é isso que decide o que vale procurar num
script: não quanta capacidade distribuída ele usa, mas se ele usa o hardware que
a conta paga — e por quanto tempo o deixa parado.

O pacote vem de um bucket que a coleta não controla, então metade destes testes
é sobre recusar: tar com caminho de fuga, membro grande demais, pacote maior que
o limite do bundle. Nenhuma dessas recusas pode derrubar a coleta, e nenhuma
pode virar um script pela metade que o scanner leria como se fosse inteiro.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from julius.collection.artifacts import CodeArtifact
from julius.collection.collectors.glue.scripts import _read_code
from julius.collection.collectors.sagemaker_extended import _apply_code_location
from julius.collection.models import Account, SageMakerJob
from julius.config import DEFAULT_CONFIG
from julius.knowledge.rules.sagemaker import code as sagemaker_code
from julius.knowledge.rules.sagemaker.code_scanner import scan_sagemaker_script

_TREINO_CPU = """
import pandas as pd

def main():
    dados = pd.read_csv("/opt/ml/input/data/train/base.csv")
    modelo = treinar(dados)
    modelo.save("/opt/ml/model/model.pkl")
"""

_TREINO_GPU = """
import torch

def main():
    modelo = Rede().to("cuda")
    torch.save(modelo.state_dict(), "/opt/ml/checkpoints/ck.pt")
"""


class _Corpo:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data if n < 0 else self._data[:n]


class _S3Falso:
    """O mínimo de `get_object` que a leitura usa, sem rede."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.chamadas = 0

    def get_object(self, **kwargs):
        self.chamadas += 1
        return {"Body": _Corpo(self._data)}


def _tar(arquivos: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for nome, conteudo in arquivos.items():
            info = tarfile.TarInfo(name=nome)
            info.size = len(conteudo)
            tar.addfile(info, io.BytesIO(conteudo))
    return buffer.getvalue()


def _job(**overrides) -> SageMakerJob:
    defaults = dict(
        name="treino",
        kind="training",
        instance_type="ml.p3.2xlarge",
        instance_count=1,
        duration_seconds=3600.0,
        modeled_cost=4.0,
        cost_quality="modeled",
        coverage_days=30,
        detailed_metrics=True,
        gpu_p95=1.0,
        code_location="s3://bucket/sourcedir.tar.gz",
        code_entry_point="train.py",
        code_kind="sourcedir_tar",
    )
    defaults.update(overrides)
    return SageMakerJob(**defaults)


def _artefato(content: str, *, truncated: bool = False) -> CodeArtifact:
    return CodeArtifact(
        asset_name="treino",
        source="s3://bucket/sourcedir.tar.gz",
        content=content,
        sha256="a" * 64,
        truncated=truncated,
        kind="sagemaker_script",
    )


# --- extração dos ponteiros, do payload que o describe já trazia -------------


def test_script_mode_pointers_come_from_hyperparameters():
    """O SDK empacota diretório e programa em hiperparâmetros, com aspas JSON."""
    job = SageMakerJob(name="t", kind="training")

    _apply_code_location(
        job,
        "training",
        {
            "HyperParameters": {
                "sagemaker_submit_directory": '"s3://bucket/src/sourcedir.tar.gz"',
                "sagemaker_program": '"train.py"',
            }
        },
    )

    assert job.code_location == "s3://bucket/src/sourcedir.tar.gz"
    assert job.code_entry_point == "train.py"
    assert job.code_kind == "sourcedir_tar"
    assert job.code_unavailable_reason == ""


def test_a_managed_algorithm_is_an_answer_not_a_failure():
    """XGBoost gerenciado não tem script do cliente — e isso precisa ser dito."""
    job = SageMakerJob(name="t", kind="training")

    _apply_code_location(
        job, "training", {"AlgorithmSpecification": {"AlgorithmName": "xgboost"}}
    )

    assert job.code_kind == "builtin_algorithm"
    assert "gerenciado" in job.code_unavailable_reason
    assert job.code_location == ""


def test_processing_code_comes_from_the_input_named_code():
    job = SageMakerJob(name="p", kind="processing")

    _apply_code_location(
        job,
        "processing",
        {
            "ProcessingInputs": [
                {"InputName": "dados", "S3Input": {"S3Uri": "s3://bucket/dados/"}},
                {"InputName": "code", "S3Input": {"S3Uri": "s3://bucket/proc.py"}},
            ]
        },
    )

    assert job.code_location == "s3://bucket/proc.py"
    assert job.code_kind == "s3_object"


# --- leitura do pacote, tratando o tar como entrada hostil -------------------


def test_the_entry_point_is_extracted_from_the_package():
    data = _tar({"train.py": b"print('treino')\n", "requirements.txt": b"torch\n"})

    conteudo, truncado = _read_code(
        _S3Falso(data), "s3://b/sourcedir.tar.gz", entry_point="train.py", max_bytes=100_000
    )

    assert "print('treino')" in conteudo
    assert truncado is False


def test_a_member_that_escapes_the_package_is_refused():
    """Caminho com `..` sai do destino em qualquer extração ingênua."""
    data = _tar({"../../etc/train.py": b"x = 1\n"})

    with pytest.raises(ValueError):
        _read_code(
            _S3Falso(data), "s3://b/sourcedir.tar.gz", entry_point="../../etc/train.py",
            max_bytes=100_000,
        )


def test_a_member_larger_than_the_cap_is_refused_while_reading():
    """O tamanho é medido ao ler: o cabeçalho do tar pode mentir."""
    data = _tar({"train.py": b"#" * 5000})

    with pytest.raises(ValueError):
        _read_code(
            _S3Falso(data), "s3://b/sourcedir.tar.gz", entry_point="train.py", max_bytes=100
        )


def test_a_truncated_download_never_becomes_half_a_script():
    """Tar cortado não descomprime, e adivinhar o resto enganaria o scanner."""
    data = _tar({"train.py": b"print(1)\n"})
    s3 = _S3Falso(data)

    with pytest.raises(ValueError):
        # `max_bytes` menor que o pacote faz `_read_s3_bytes` sinalizar corte.
        _read_code(s3, "s3://b/sourcedir.tar.gz", entry_point="train.py", max_bytes=10)


# --- o gate: o que o script prova e o que ele só sugere ----------------------


def test_a_gpu_instance_running_cpu_code_becomes_a_figure():
    """GPU p95 quase zerada corrobora o que a AST não viu: nada usa acelerador."""
    account = Account(account_id="123456789012", sagemaker_jobs=[_job()])

    found, signals = sagemaker_code.detect(
        account, [_artefato(_TREINO_CPU)], DEFAULT_CONFIG, "scan"
    )

    achado = next(i for i in found if i.rule_id == "SM-CODE-CPU-ONLY-ON-GPU")
    assert achado.estimated_gain.monthly_expected > 0
    assert any("train.py" in item for item in achado.evidence)
    assert any("GPU p95" in item for item in achado.evidence)
    assert not any(s.rule_id == "SM-CODE-CPU-ONLY-ON-GPU" for s in signals)


def test_without_telemetry_the_same_script_is_only_a_question():
    account = Account(
        account_id="123456789012",
        sagemaker_jobs=[_job(detailed_metrics=False, gpu_p95=None)],
    )

    found, signals = sagemaker_code.detect(
        account, [_artefato(_TREINO_CPU)], DEFAULT_CONFIG, "scan"
    )

    assert not any(i.rule_id == "SM-CODE-CPU-ONLY-ON-GPU" for i in found)
    sinal = next(s for s in signals if s.rule_id == "SM-CODE-CPU-ONLY-ON-GPU")
    assert sinal.artifact_sha256
    assert sinal.missing_evidence


def test_a_script_that_uses_the_gpu_is_not_flagged():
    account = Account(account_id="123456789012", sagemaker_jobs=[_job(gpu_p95=80.0)])

    found, signals = sagemaker_code.detect(
        account, [_artefato(_TREINO_GPU)], DEFAULT_CONFIG, "scan"
    )

    ids = {i.rule_id for i in found} | {s.rule_id for s in signals}
    assert "SM-CODE-CPU-ONLY-ON-GPU" not in ids
    assert "SM-CODE-NO-CHECKPOINT" not in ids


def test_extra_instances_without_distributed_code_need_no_measurement():
    """Configuração fecha sozinha: o cluster é cobrado e o script não o usa."""
    account = Account(
        account_id="123456789012",
        sagemaker_jobs=[
            _job(instance_count=4, gpu_p95=90.0, modeled_cost=16.0, instance_type="ml.g5.xlarge")
        ],
    )

    found, _ = sagemaker_code.detect(
        account, [_artefato(_TREINO_GPU)], DEFAULT_CONFIG, "scan"
    )

    achado = next(i for i in found if i.rule_id == "SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE")
    # Três de quatro instâncias sem trabalho atribuível pelo script.
    assert achado.estimated_gain.monthly_expected == pytest.approx(12.0, rel=0.05)


def test_a_missing_target_rate_produces_no_figure_instead_of_a_zero():
    """Zero aqui se leria como "não compensa trocar", que é outra afirmação."""
    account = Account(
        account_id="123456789012",
        # `p3` é família com GPU, mas a tabela da região não tem este tamanho
        # nem o `m5` equivalente — não há com o que comparar.
        sagemaker_jobs=[_job(instance_type="ml.p3.16xlarge", modeled_cost=100.0)],
    )

    found, signals = sagemaker_code.detect(
        account, [_artefato(_TREINO_CPU)], DEFAULT_CONFIG, "scan"
    )

    assert not any(i.rule_id == "SM-CODE-CPU-ONLY-ON-GPU" for i in found)
    assert any(s.rule_id == "SM-CODE-CPU-ONLY-ON-GPU" for s in signals)


def test_coverage_separates_a_missing_script_from_no_script_at_all():
    """Contar um algoritmo gerenciado como não coberto seria inventar lacuna."""
    account = Account(
        account_id="123456789012",
        sagemaker_jobs=[
            _job(name="com-script"),
            SageMakerJob(
                name="gerenciado",
                kind="training",
                code_kind="builtin_algorithm",
                code_unavailable_reason="algoritmo gerenciado ou imagem própria",
            ),
        ],
    )

    assert [job.name for job in sagemaker_code.analysable_jobs(account)] == ["com-script"]

    gaps = sagemaker_code.coverage_gaps(account, [])
    assert any("com-script" in gap for gap in gaps)
    assert any("sem script para analisar" in gap for gap in gaps)


def test_the_scanner_reads_the_instance_before_calling_a_pattern():
    """O mesmo script é achado ou não conforme o hardware que a conta paga."""
    em_gpu = {f.rule_id for f in scan_sagemaker_script(_TREINO_CPU, gpu_instance=True, instances=1)}
    em_cpu = {f.rule_id for f in scan_sagemaker_script(_TREINO_CPU, gpu_instance=False, instances=1)}

    assert "SM-CODE-CPU-ONLY-ON-GPU" in em_gpu
    assert "SM-CODE-CPU-ONLY-ON-GPU" not in em_cpu
