---
name: stepfunctions
---

## state_machine

- A ASL tolera semântica at-least-once, ou há Task com efeito colateral não idempotente — escrita sem chave de deduplicação, notificação, cobrança — que a reexecução do Express duplicaria?
- O loop de espera existe por limitação real da integração, ou por hábito onde .sync ou callback serviria?
- Retry e Catch cobrem falha transitória, ou escondem erro recorrente que repaga trabalho já cobrado a cada tentativa?
