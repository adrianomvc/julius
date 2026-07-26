"""Limiares compartilhados pelos detectores ativos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    """Limiares determinísticos compartilhados pelos detectores ativos."""

    # Alvo de utilização de CPU para dimensionamento (acima disso, saudável).
    utilization_target: float = 0.75
    # CPU média abaixo disso sinaliza superdimensionamento.
    low_cpu: float = 0.45
    # Idle timeout (min) considerado alto para sessões interativas.
    session_idle_timeout_high_min: int = 60
    # DPU mínima útil de uma interactive session.
    session_min_dpu: int = 2
    # Bytes escaneados/execução no Athena considerado "excessivo" (256 GB).
    athena_high_scan_bytes: int = 256 * 1024**3
    # Nº mínimo de execuções observadas para confiança não-baixa.
    min_runs: int = 5
    # Cobertura mínima (dias) da janela para confiança não-baixa.
    min_coverage_days: int = 14
    # Taxa de falha (incl. retries) acima da qual o desperdício de DPU é sinalizado.
    high_failure_rate: float = 0.10
    # Base é considerada "sem uso" com até este nº de toques na janela.
    unused_touches_max: int = 0
    # Base "pouco usada": toques acima de unused_touches_max e até este limite.
    low_touches_max: int = 30
    # Nº mínimo de execuções/mês para o job escritor ser considerado recorrente.
    recurring_runs_min: int = 4
    # Timeout é "excessivo" se > ratio × duração média (e acima do mínimo absoluto).
    timeout_excess_ratio: float = 4.0
    timeout_excess_min_minutes: int = 120
    # Worker type é "grande demais" quando a CPU fica abaixo disto (types G.4X/G.8X).
    worker_type_low_cpu: float = 0.30
    # Evidência complementar obrigatória antes de reduzir capacidade.
    worker_memory_pressure_high: float = 0.75
    worker_disk_pressure_high: float = 0.70
    worker_utilization_low: float = 0.45
    executor_gap_high: float = 0.35
    task_skew_high: float = 1.0
    task_duration_cv_high: float = 0.75
    high_job_frequency_monthly: int = 100
    # Concorrência e telemetria Glue: sempre geram investigação, nunca uma
    # alteração automática, porque paralelismo e streaming podem ser intencionais.
    glue_overlapping_runs_min: int = 2
    glue_streaming_no_input_min_hours: float = 1.0
    glue_small_files_min_count: int = 100
    glue_small_file_max_bytes: int = 32 * 1024**2
    # Estimativas conservadoras: parte do desperdício medido que esperamos capturar.
    conservative_realization: float = 0.70
    # Alocação de custo Glue: fração máxima de DPU-hora apenas estimada (sem
    # DPUSeconds reportado) tolerada para considerar a alocação reconciliada.
    glue_estimated_dpu_tolerance: float = 0.05
    # Banda aceita entre o consumo modelado e a cobrança do bucket no Cost
    # Explorer (mesma disciplina da reconciliação Athena/CloudWatch).
    glue_reconciliation_low: float = 0.95
    glue_reconciliation_high: float = 1.05
    # Step Functions: volume/duração para avaliar Standard → Express.
    sfn_express_min_executions: int = 5000
    sfn_short_duration_sec: int = 300
    # Retry acima disso deixa de parecer resiliência e passa a merecer pergunta.
    sfn_retry_attempts_high: int = 3
    # SageMaker: idle shutdown "alto" (min) e horas ociosas/dia relevantes.
    sm_idle_shutdown_high_min: int = 120
    sm_idle_hours_min: float = 1.0
    # SageMaker: endpoint "sem uso" com até este nº de invocações/mês.
    sm_endpoint_unused_invocations: int = 50
