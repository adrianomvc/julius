---
name: glue
---

## glue_job

- O script justifica a capacidade configurada — worker type, número de workers, autoscaling?
- O schedule é compatível com a natureza da fonte, ou há execução que não encontra dado novo?
- O modo de escrita casa com o filtro de quem lê a tabela a jusante?
- Há reprocessamento evitável — bookmark, overwrite total, retry silencioso?
- O SLA tolera início adiado e capacidade não garantida, ou alguém a jusante trava esperando este job terminar na hora?
