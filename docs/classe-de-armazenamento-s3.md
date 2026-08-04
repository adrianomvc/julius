# Oportunidades de classe de armazenamento S3

> Por que `LastModified` não prova que um objeto está sem uso, e o que a estimativa de transição separa. Extraído do README para manter lá o que todo leitor precisa.

O Julius nunca usa `LastModified` como prova de que um objeto está sem uso:
essa data mede escrita, não leitura. A coleta agrega por prefixo conhecido a
distribuição de objetos por classe, tamanho e idade de escrita, sem persistir
chaves. Marcadores de diretório com zero byte não distorcem o tamanho médio.

Quando Server Access Logging já está habilitado, o coletor lê de forma limitada
o bucket de destino configurado e guarda somente `last_read_at`, quantidade e
bytes lidos na janela, cobertura e qualidade. IP, requester, e-mail, user-agent,
linha bruta e chave do objeto não entram no dataset. Entrega best-effort ou
listagem parcial aparece como lacuna e não vira “zero leitura”.

A regra `S3-STORAGE-CLASS-TRANSITION`, inclusive no perfil Consumer, só recomenda
sobre `table_location`, evita
prefixos sobrepostos e respeita filtros de lifecycle. A estimativa v2 separa
custo pontual de transição, economia recorrente, resultado do primeiro mês e
break-even; usa o preço PUT/COPY da classe de destino, aplica o tamanho mínimo
faturável por objeto e usa a cobrança
Standard do Cost Explorer como baseline quando ela está reconciliada. Glacier
Flexible Retrieval permanece bloqueado até o time confirmar que o SLA aceita
recuperação em horas. Toda transição é apenas recomendação para o time dono; o
Julius não copia nem altera objetos. Inventário parcial, bucket versionado,
evidência de leitura sem cobertura integral ou pricing S3 não verificado
bloqueiam a cifra e mantêm o caso fora do portfólio. Lifecycle, exclusão e
aborto de multipart uploads continuam apenas como sinais no perfil Consumer.
