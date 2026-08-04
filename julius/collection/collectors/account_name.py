"""O nome da conta, lido da própria conta.

O nome lógico recorta o Glue Catalog: dele sai
`database_db_compartilhado_consumer_<conta>`. Sem ele a coleta percorre todos os
bancos do catálogo — inclusive os compartilhados por outras contas, que custam uma
chamada cada e devolvem tabela sobre a qual esta conta não pode agir.

O último degrau da cascata em `collection/targets.py` era o apelido do perfil SSO,
que é escolha de quem rodou `aws configure sso` e não tem relação com a conta. Numa
máquina sem `~/.julius-accounts.json`, era ele que decidia o escopo.

**`iam:ListAccountAliases` seria a API certa** — existe exatamente para dar um nome
legível à conta, e não carrega dado pessoal nenhum. Foi tentada primeiro e devolve
`[]` na organização onde isto foi verificado; por isso o caminho é o contato.

**E é por isso que este módulo é do tamanho que é.** `GetContactInformation`
devolve nome, endereço, telefone e empresa do contato da conta. Só `FullName` sai
daqui. O resto da resposta não é atribuído a variável, não volta para quem chamou,
não vai para o dataset nem para a saúde, e não entra em log — existe pelo tempo de
uma expressão. Numa conta onde esse campo guarde o nome de uma pessoa de verdade, é
esse valor que chega, e ele vira nome de banco ou nada.
"""

from __future__ import annotations

from typing import Any

from julius.collection.session import make_client


def collect_account_name(session: Any) -> str:
    """O `FullName` do contato da conta, ou `""` quando não dá para saber.

    `""` e não exceção: nome de banco errado degrada o escopo do catálogo e a
    saúde da coleta declara isso — não é motivo para interromper um scan que
    ainda tem trinta e nove fontes para ler. Permissão negada, campo em branco e
    API indisponível caem todos aqui, e quem chama trata os três igual.
    """
    resposta = make_client(session, "account").get_contact_information()
    return str((resposta.get("ContactInformation") or {}).get("FullName") or "").strip()
