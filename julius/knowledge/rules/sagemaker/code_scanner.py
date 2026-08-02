"""Scanner estático conservador para padrões de custo em scripts SageMaker.

O que o SageMaker cobra é instância-hora. Isso muda o que vale procurar num
script: no Glue a pergunta é quanta capacidade distribuída o código usa; aqui é
se o código usa o hardware que a conta está pagando, e por quanto tempo o deixa
parado.

Os dois padrões de maior valor saem daí. Um script que nunca toca a GPU num job
de família `p`/`g` paga aceleração que não existe no código — é o análogo exato
do `GLUE-SPARK-TO-PYTHON-SHELL`, e a diferença de tarifa entre `p3.2xlarge` e um
`c5` equivalente é a maior do serviço. E um script sem checkpoint não bloqueia
só a resiliência: bloqueia o managed spot, que é a economia mais direta que o
treino tem.

Como todo scanner daqui, ele não interpreta ausência de sinal como segurança.
Um script pode importar torch sob condição, e a AST não desenrola condição.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from julius.knowledge.rules.code_ast import (
    call_lines,
    calls_inside_loops,
    external_io_in_row_functions,
    matching_lines,
    parse,
    swallowed_exception_lines,
)


@dataclass(frozen=True)
class CodeFinding:
    rule_id: str
    signal: str
    lines: tuple[int, ...]


#: Uso explícito de acelerador. Qualquer um deles basta para o script deixar de
#: parecer CPU-only — e a lista é generosa de propósito: dizer "não usa GPU" de
#: um script que usa é um falso positivo caro, e o inverso só perde um achado.
_GPU_MARKERS = (
    r"\btorch\.cuda\b",
    r"\.cuda\s*\(",
    r"\bdevice\s*=\s*[\"']cuda",
    r"\bto\s*\(\s*[\"']cuda",
    r"tf\.device\s*\(\s*[\"']/(?:GPU|gpu)",
    r"\bcupy\b",
    r"\btensorrt\b",
    r"gpu_hist|device\s*=\s*[\"']cuda[\"']|tree_method\s*=\s*[\"']gpu",
    r"\bXGBClassifier\([^)]*gpu",
    r"jax\.devices\s*\(\s*[\"']gpu",
    r"\bautocast\b",
)

#: Treino distribuído declarado. Sem nenhum deles, mais de uma instância é
#: capacidade que o script não sabe que existe.
_DISTRIBUTED_MARKERS = (
    r"\btorch\.distributed\b",
    r"\bDistributedDataParallel\b",
    r"\bsmdistributed\b",
    r"\bhorovod\b",
    r"\bhvd\.",
    r"MultiWorkerMirroredStrategy",
    r"\bMirroredStrategy\b",
    r"\bdeepspeed\b",
    r"\baccelerate\b",
)

#: Caminhos que o SageMaker monta no contêiner. São contrato, não convenção:
#: é por eles que o serviço sobe o checkpoint e entrega os dados de entrada.
_CHECKPOINT_PATH = r"/opt/ml/checkpoints"
_INPUT_PATH = r"/opt/ml/input/data"

_EARLY_STOP_MARKERS = (
    r"\bEarlyStopping\b",
    r"\bearly_stopping\b",
    r"\bpatience\s*=",
    r"\bbreak\b",
)


def scan_sagemaker_script(source: str, *, gpu_instance: bool, instances: int) -> list[CodeFinding]:
    """Sinais estáticos do script, à luz da instância que a conta paga.

    `gpu_instance` e `instances` entram porque o mesmo script é achado ou não
    dependendo do hardware: não usar GPU só custa dinheiro em instância com
    GPU, e não distribuir só custa quando há mais de uma.
    """
    findings: dict[str, CodeFinding] = {}
    lines = source.splitlines()
    tree = parse(source)

    def add(rule_id: str, signal: str, numeros: list[int]) -> None:
        limpo = tuple(sorted({numero for numero in numeros if numero > 0})) or (1,)
        findings[rule_id] = CodeFinding(rule_id, signal, limpo)

    usa_gpu = _tem(lines, _GPU_MARKERS)
    if gpu_instance and not usa_gpu:
        add(
            "SM-CODE-CPU-ONLY-ON-GPU",
            "nenhuma API de acelerador detectada em job de instância com GPU",
            [1],
        )

    if instances > 1 and not _tem(lines, _DISTRIBUTED_MARKERS):
        add(
            "SM-CODE-SINGLE-DEVICE-MULTI-INSTANCE",
            "nenhuma API de treino distribuído detectada com mais de uma instância",
            [1],
        )

    if not re.search(_CHECKPOINT_PATH, source):
        add(
            "SM-CODE-NO-CHECKPOINT",
            "nenhuma escrita em /opt/ml/checkpoints detectada",
            matching_lines(lines, r"\bsave\b|\btorch\.save\b|\bsave_model\b") or [1],
        )

    carga_total = _carga_total_de_entrada(tree, lines)
    if carga_total:
        add(
            "SM-CODE-FULL-DATASET-LOAD",
            "diretório de entrada carregado inteiro antes do treino",
            carga_total,
        )

    epocas = _epocas_fixas(tree, lines)
    if epocas:
        add(
            "SM-CODE-FIXED-EPOCHS",
            "laço de épocas com contagem fixa e sem parada antecipada observável",
            epocas,
        )

    externo = external_io_in_row_functions(
        tree, appliers={"map", "apply", "applymap", "foreach", "starmap"}
    )
    externo += calls_inside_loops(tree, {"get_object", "download_file", "put_object"})
    if externo:
        add(
            "SM-CODE-ROW-EXTERNAL-IO",
            "chamada externa dentro de laço ou função aplicada por registro",
            sorted(set(externo)),
        )

    engolidas = swallowed_exception_lines(tree)
    if engolidas:
        add(
            "SM-CODE-SWALLOWED-EXCEPTION",
            "exceção descartada sem falha ou registro observável",
            engolidas,
        )

    return sorted(findings.values(), key=lambda finding: finding.rule_id)


def _tem(lines: list[str], padroes: tuple[str, ...]) -> bool:
    return bool(matching_lines(lines, "|".join(padroes)))


def _carga_total_de_entrada(tree: ast.AST | None, lines: list[str]) -> list[int]:
    """Leitura do diretório de entrada inteiro, e sem modo de streaming.

    FastFile e Pipe existem porque a instância fica ligada e cobrando enquanto
    o dado desce. O sinal é a leitura do diretório montado sem nenhuma marca de
    leitura incremental por perto.
    """
    if not any(re.search(_INPUT_PATH, linha) for linha in lines):
        return []
    if _tem(lines, (r"\bIterableDataset\b", r"\bchunksize\s*=", r"\bstream\b")):
        return []
    leituras = call_lines(tree, {"read_csv", "read_parquet", "read_json", "load"})
    entrada = set(matching_lines(lines, _INPUT_PATH))
    # Só conta quando a leitura e o caminho montado aparecem na mesma linha: um
    # `read_csv` qualquer num script que também menciona o diretório não prova
    # que ele lê o diretório.
    return sorted(entrada & set(leituras)) or sorted(entrada & {n + 1 for n in leituras})


def _epocas_fixas(tree: ast.AST | None, lines: list[str]) -> list[int]:
    """`for epoch in range(N)` sem nada que interrompa antes do fim."""
    if tree is None or _tem(lines, _EARLY_STOP_MARKERS):
        return []
    resultado = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if "epoch" not in node.target.id.lower():
            continue
        iterador = node.iter
        if (
            isinstance(iterador, ast.Call)
            and getattr(iterador.func, "id", "") == "range"
            and iterador.args
            and isinstance(iterador.args[0], ast.Constant)
        ):
            resultado.append(node.lineno)
    return sorted(set(resultado))
