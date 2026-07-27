"""Regras de S3 sobre objetos que ninguém consome mais.

A restrição do ambiente define o catálogo: não é possível alterar
infraestrutura de S3, só criar e apagar arquivos. Isso tira de cena o lever
convencional do serviço — lifecycle e transição de classe por configuração — e
deixa o que se resolve escrevendo e apagando objeto.

Acontece que é ali que mora a maior parte do desperdício de um data lake:
resultado de query que ninguém apaga, upload que nunca terminou, staging de
execução que falhou, log sem prazo. Todos com a mesma propriedade: existem
porque ninguém os removeu, não porque alguém decidiu mantê-los.

**O Julius recomenda; quem apaga é o time dono.** Nenhuma regra daqui executa
exclusão, e a coleta que as alimenta é read-only.
"""
