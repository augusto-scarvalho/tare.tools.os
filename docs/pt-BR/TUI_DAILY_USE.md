# Uso diário do TUI — guia rápido

> Versão em português. A referência é a [versão em inglês](../guides/TUI_DAILY_USE.md).

Guia rápido para o trabalho do dia a dia na interface de terminal (TUI) do tare. Ele substitui as
partes de uso diário do desatualizado `FLOWS_TUI_USER_GUIDE.md`. Grafos e flows são **ajudas
opcionais**, não pré-requisitos. A execução de uma Work individual aceita grafos de projeto
selecionados explicitamente; sequências ainda usam o grafo de controle.

## 1. Abrir um projeto

Rode `tare` na pasta do projeto ou passe outra pasta:

```powershell
cd C:/projects/my-project
tare
tare "C:/projects/another project"
```

A configuração do comando no Windows está descrita em [instalação do produto](PRODUCT_INSTALLATION.md).
O comando usa a release gerenciada e suas atualizações automáticas; abra o TUI de novo
depois de uma atualização. `tare console --root PATH` e o atalho da área de trabalho continuam funcionando.
O histórico fica fora dos projetos, no armazenamento configurado da instalação.

Para uma conversa salva com cota finita, um operador pode aumentar explicitamente
os totais dela ao abrir o TUI:
`tare console --root PATH --resume-coordinator-seconds 3600 --resume-coordinator-calls 90`.
Eles valem só ao enviar uma continuação para uma conversa salva com o assistente. O
total de chamadas é o pool do OS compartilhado com os workers; o teto efetivo de chamadas do runtime
segue a concessão explícita, enquanto a identidade original do orçamento e o journal continuam fixos. Chamadas usadas e tempo ativo continuam
contabilizados, e repetir o mesmo total não repõe nenhuma das cotas. O
agente não pode conceder mais orçamento a si mesmo. As opções públicas equivalentes de `agent` são
`--active-time-budget` e `--active-call-budget`.

Esperas concluídas pela sua resposta não contam para o prazo de uma conversa finita.
Uma invocação inacabada ou incerta não recebe esse tempo de volta
automaticamente. Depois de uma interrupção, confira os efeitos salvos antes de tentar de novo: uma
implementação interrompida não é repetida nem aceita em silêncio.

Depois de uma auditoria somente leitura cancelada, um novo pedido humano pode iniciar uma
revisão limitada quando o processo anterior tiver confirmado a limpeza local. O candidato e
o uso anterior continuam registrados, inclusive cobranças remotas desconhecidas. A nova revisão
usa a escolha atual de revisor; vereditos aceitos ou adversos continuam finais.

Para uma entrega salva, `/recheck` repete as verificações do candidato e a auditoria independente
sem chamar o coordenador nem o implementador. Use `/retry_delivery` quando um achado de auditoria
exigir outra tentativa de implementação, e `/repair_plan` quando o plano salvo
precisar de uma correção de escopo. Esses comandos preservam o candidato, os achados anteriores,
o histórico e o uso já cobrado. Eles ainda exigem as permissões existentes e orçamento
restante; uma verificação ou auditoria com falha mantém a entrega bloqueada.

O símbolo ao lado do status fica animado enquanto um modelo ou ferramenta trabalha, inclusive
nas esperas silenciosas antes do primeiro token. A aba do terminal acompanha a mesma atividade:
`⠋ - tare - my-project`. `○` significa pronto, `?` espera sua entrada, `✓` marca um
turno concluído, `■` significa parado e `!` pede atenção. Um turno concluído não
significa, por si só, que uma Work está verificada ou resolvida. O cancelamento continua animado até
o runner parar. Fechar o tare restaura o título do shell onde há suporte.

### Escolher as conexões para uma abertura

Acrescente uma opção a `tare` ou `tare PATH`. Ela vale só para este TUI e o trabalho
delegado por ele. Ao fechá-lo, o roteamento habitual volta; as configurações salvas da máquina, as preferências
de modelo e os prompts das conversas não são substituídos por essas opções.

| Opção | Efeito |
| --- | --- |
| `--subscription_only` | Usa as assinaturas: as CLIs do Codex, Claude e AGY e a assinatura do OpenCode. O Jev hospedado continua disponível. |
| `--local_only` | Usa modelos locais; troca o Jev hospedado pelo juiz local. As APIs de busca continuam disponíveis. |
| `--api_only` | Usa APIs de inferência. O Jev hospedado e as APIs de busca continuam disponíveis. |
| `--pro_planner` | Usa o OpenAI Sol PRO, depois o Astra PRO se disponível, só para o planejador separado, com raciocínio no mínimo alto. Exige uma chave de API. |
| `--prefer_api` | Prefere as APIs de inferência disponíveis para o planejador e os auditores; recorre a outras conexões permitidas. |
| `--prefer_subscription` | Prefere as assinaturas disponíveis (CLIs, a assinatura do OpenCode) para o planejador e os auditores; recorre a outras conexões permitidas. |
| `--prefer_local` | Mantém coordenador, explorador, implementador e pesquisador na inferência local; usa planejador e auditores externos qualificados. O Jev hospedado continua disponível. |
| `--disable_judge` | Desliga o Jev hospedado e o juiz local. Comandos protegidos exigem aprovação humana quando necessário; o YOLO pula essas perguntas. Verificações, auditorias finais, sandbox e o Auto-review nativo do Codex continuam. |

```powershell
tare --subscription_only
tare "C:/projects/my-project" --prefer_local
tare --prefer_subscription --pro_planner
tare --local_only --disable_judge
```

Escolha no máximo um modo `*_only` e um modo `--prefer_*`. Combinações conflitantes
são recusadas antes de o TUI abrir. O PRO não pode ser combinado com local-only nem
subscription-only. Grafias com hífen, como `--local-only`, também funcionam.
O TUI mostra as opções ativas da abertura acima da conversa. Exclusões,
orçamentos, independência de auditoria e leases de GPU já existentes continuam valendo. Se nenhuma
conexão permitida estiver disponível, o tare informa o bloqueio em vez de iniciar outro tipo
de inferência. Uma conversa salva fixada numa conexão excluída precisa continuar numa
conexão permitida; o histórico original e o prefixo de cache dela ficam intactos.

O juiz local só é usado quando já está carregado e ocioso. Se a classificação de comandos
precisar dele e ele não estiver disponível, o tare pede aprovação (ou respeita o YOLO); ele não
carrega outro modelo por cima de um lease de GPU ativo. As otimizações em lote do Jev hospedado continuam desligadas
no modo local-only; o fallback determinístico/explorador já existente cuida do trabalho.

Numa instalação a partir dos fontes, rode `tare PATH` no ambiente ativado; o histórico dela
fica em `.reconciliation` ao lado do clone, a menos que `TARE_STORAGE_ROOT` esteja definido.

Para investigações de roadmap/backlog, veja [leituras de dados do projeto](../guides/PROJECT_DATA_READS.md).
Use `/backlog` para selecionar explicitamente o grafo do projeto e `/new` para as ferramentas
de leitura de JSON atualizadas. Conversas existentes mantêm o contrato de ferramentas original.

## Consulta opcional ao SpecGraph

Nas configurações do workspace (F2 em terminais pequenos), **SpecGraph advisory lookup** vem
desligado por padrão. Ative-o antes de iniciar uma nova conversa. A ferramenta `context`
consulta apenas snapshots já publicados; ela não indexa, não roda outro LLM
nem ativa o bloqueio de edição. Conversas salvas mantêm suas capacidades, parâmetros de modelo
e limites independentemente do padrão para novas conversas; não é preciso mudar
configuração para retomá-las. A CLI continua recusando flags explícitas de inicialização
como `--specgraph-context` ou `--no-context` junto com `--resume`.

Publique um índice explicitamente com a CLI do SpecGraph instalada:

```powershell
Set-Location -LiteralPath "C:\projects\my-project"
python -m specgraph.cli index
tare agent --root "C:\projects\my-project" --specgraph-context --instruction "Find callers of parse"
```

O padrão legado da CLI e `--no-context` mantêm o comportamento. A nova
flag `--specgraph-context` ativa a consulta consultiva independentemente da edição.
Um acerto no índice é metadado de snapshot, não prova do conteúdo atual dos fontes nem de cobertura
completa; índices desatualizados, ausentes ou parciais exigem o fallback comum de `read`/`search`.
Leia os fontes atuais antes de editar ou de afirmar evidência. As consultas
ficam limitadas ao repositório/índice selecionado e aos limites de resposta; elas não
ampliam permissões, não admitem uma Work, não atualizam o grafo dela nem provam seus critérios.
Conversas normais não ganham vínculo com uma Work por meio da consulta. Não se alega
economia sustentada; a pequena qualificação pareada no TUI continua separada.

## 2. Selecionar outro projeto

Use `/project` (ou F5) para abrir o seletor de projeto/workspace e escolher outra pasta
de projeto. "Reload workspace" troca a pasta e inicia uma conversa separada para ela;
as conversas salvas continuam disponíveis para a pasta delas. Você também pode recarregar com Ctrl+R.

## 3. Permissão de execução

Peça a mudança na conversa. Quando o plano salvo precisa de acesso de edição,
o tare oferece **Allow for this task**, **Always allow in this project** e **Deny**. Ele mostra
o projeto, os arquivos e as verificações. Uma aprovação continua esse plano na mesma conversa e
cobre as correções dele; um plano novo ou outros arquivos perguntam de novo. O coordenador continua somente leitura. Esperar pela sua decisão não faz chamadas de modelo.
Um acesso de edição lembrado, inclusive uma política commands/yolo existente, não precisa de
confirmação extra de edição. Pedidos só de plano continuam sem executar.

Para padrões avançados, abra `/permissions`:

- **Read only** — sem edição de arquivos nem terminal.
- **Edit files** — grava dentro do escopo escolhido; terminal desativado.
- **Edit + system commands** — edição de arquivos mais comandos de terminal.

Os padrões são salvos separadamente para cada projeto. As ferramentas, o modelo,
as instruções e o histórico do próprio coordenador continuam fixos; a autorização dos workers é verificada à parte
ao executar um plano. Conversas explícitas de `/session` mantêm as capacidades
originais. Comandos podem acessar o host e a rede: o escopo de edição de arquivos sozinho
não é um sandbox do sistema.

## 4. Enviar um pedido normal de código

Digite o pedido e pressione **Enter** para enviar. **Shift+Enter** ou **Ctrl+J** insere
uma quebra de linha; **Ctrl+Enter** também envia. Nos menus de completar, o Enter aceita primeiro
o comando ou arquivo selecionado. O coordenador delega o planejamento às rotas de planejador
configuradas e depois a implementação, a verificação no host e a revisão independente.
As chamadas de ferramenta de cada sessão delegada aparecem na conversa como linhas
recolhidas com o nome da etapa, por exemplo `Implement · Edit relay/x.py · Done` ou
`Explore · Read a.py · Done`, cada uma com **Full output**; as linhas do próprio
coordenador não têm prefixo.

Texto colado no campo de mensagem continua sendo um rascunho editável. O campo cresce até
oito linhas. Rascunhos com pelo menos 1 KiB ou mais de oito linhas mostram a contagem de
bytes/linhas; rascunhos acima do limite de 16 KiB para prompt direto mostram um aviso. Encurte o
rascunho ou anexe o material de apoio antes de enviar. Colar não envia
a mensagem nem inicia inferência.

## 5. Completar comandos com barra

Digitar `/` abre a lista de opções. Use as setas para escolher, Tab ou Enter para
completar o comando e Esc para fechar a lista. Os comandos aceitos incluem `/new`,
`/resume`, `/model`, `/diff`, `/sessions`, `/project`, `/permissions`, `/backlog`,
`/terminal`, `/attach`, `/status` e `/help`.

A paleta de comandos também inclui **Artifacts and feedback**, **Tasks and agents**,
**Available tools** e **Project skills**. Os comandos de barra correspondentes são `/artifacts`,
`/tasks`, `/tools` e `/skills`.

## 6. Correções

Se o resultado estiver errado, basta enviar outra mensagem comum descrevendo a correção —
a sessão é persistente, então o contexto continua. Durante a execução, Send vira Queue;
as correções ficam salvas para uma rodada seguinte do modelo. Um recibo de entrega não prova que
o modelo seguiu a correção. Para começar do zero, use `/new`; para uma
preferência de modelo diferente na próxima conversa, use `/model`.

### Comentar uma resposta ou um diff

Abra `/artifacts` para escolher uma resposta retida, ou **F3 → Comment** para o
diff de arquivo registrado que estiver selecionado. Selecione um trecho na prévia, escreva um comentário
e escolha **Add to draft**. Uma resposta curta pode ser citada inteira sem
seleção. O rascunho existente é preservado; revise-o e envie normalmente.

O feedback leva um hash do texto capturado e, nos diffs, a referência da sessão e da
edição. Isso identifica a versão revisada, não o arquivo atual nem um artefato completo
sem truncamento. Trechos são limitados a 2 KiB e comentários a 4 KiB. Adicionar
feedback não faz chamada de modelo. Trocar de projeto/conversa, uma tarefa em execução
ou uma falha ao salvar o rascunho impede que ele seja adicionado ao destino errado. Fechar o
painel antes de **Add to draft** descarta o comentário não enviado.

### Avisos (heads-up)

Depois de algumas respostas, o tare acrescenta um aviso curto: algo que a resposta
deixou pouco claro. **Ask the agent** apenas coloca o aviso no compositor como uma
pergunta; o rascunho que você já tinha é mantido, e nada é enviado até você editar e
apertar **Enter**. Um aviso conta como usado só quando você o envia e a resposta do
agente o confirma. Você pode reescrever o aviso com as suas palavras: mantenha a
primeira linha (`tare's heads-up after your last reply:`) e o tare continua sabendo de
qual aviso a mensagem trata, e guarda a sua versão ao lado da original. **Useful** e
**Not useful** registram se o aviso ajudou (o botão
escolhido mostra um visto, e você pode mudar a escolha). Esses registros ficam nesta
máquina, o log não guarda o texto do aviso, e eles servem para ajustar quando os avisos
são escritos. Um aviso aparece só enquanto a conversa está aberta; `/heads-up` coloca
os últimos avisos no compositor pelo teclado, e a caixa de seleção nas Configurações
desliga os avisos para a máquina.

### Consultar uso, tarefas, ferramentas e skills do projeto

- `/usage` abre na primeira linha, **Provider quota and credit**: o que resta em cada assinatura
  e em cada conta de API, com a hora da leitura. A leitura salva aparece na hora e o painel
  consulta cada fornecedor de novo em segundo plano, sem chamada de modelo (veja
  [Provider quota and credit](../guides/PROVIDER_QUOTA.md)). As linhas seguintes mostram as
  chamadas registradas da conversa, tokens, leituras/gravações de cache, custos conhecidos por
  moeda e os limites configurados; contagens ausentes e chamadas sem preço continuam
  explícitas. Abra
  o painel de novo depois que uma tarefa em execução terminar para ler os totais salvos.
- `/tasks` mostra a execução atual e a atividade de ferramentas retida, agrupadas por
  sessão filha/etapa. Ele se atualiza enquanto está aberto e pode cancelar a **execução
  atual inteira**. Uma operação de ferramenta bem-sucedida não significa que uma tarefa foi aceita.
  É o histórico observado pelo console atual, não um registro global de agentes
  em segundo plano. O perfil e os totais registrados de chamadas/tokens/cache aparecem quando disponíveis;
  a quota de assinatura fica no `/usage`.
- `/tools` mostra as ferramentas efetivas e o escopo da conversa salva, ou o
  escopo pendente de uma conversa nova, além das instruções do projeto. Use
  `/permissions` para mudar os padrões de conversas futuras.
- `/skills` mostra a prévia dos arquivos `skills/<name>/SKILL.md` e
  `.agents/skills/<name>/SKILL.md` de primeiro nível. **Add to draft** pede explicitamente ao agente
  que leia e use essa skill quando você enviar. Ele não executa a skill nem
  amplia permissões. Cada skill é limitada a 32 KiB; só as primeiras 128 entradas de diretório
  de cada local são inspecionadas, e o inventário retornado vem ordenado.
  Caminhos com link, junctions, UTF-8 inválido e prévias alteradas são recusados.
  O rascunho leva uma referência de caminho: o agente lê o conteúdo atual na execução.

O gerenciamento de servidores MCP externos, catálogos globais/de usuário de skills, ramificação de conversas
e o direcionamento independente dos filhos de um flow continuam como trabalho futuro.

## 7. Cancelamento

Pressione **Ctrl+X** para cancelar uma tarefa em execução. **Ctrl+Q** fecha o TUI.

Depois do cancelamento, digite uma correção e pressione **Enter** na mesma conversa.
A nova mensagem tem precedência sobre ações que ainda não tinham começado. Mudanças concluídas
e o contexto anterior continuam salvos; o cancelamento não as desfaz nem renova orçamentos.
Uma ação externa com resultado não resolvido precisa ser reconciliada antes de continuar.

## 8. Sessões e retomada

Abra a lista de conversas salvas com **F1** ou `/sessions` e depois abra uma conversa.
Envie uma mensagem para continuá-la ou corrigi-la. **Resume** continua o turno salvo e as mensagens
na fila sem acrescentar uma nova instrução.
`/resume` também inicia uma retomada a partir do campo de mensagem. As conversas são guardadas por pasta
de projeto, então uma conversa salva continua disponível quando você volta para a pasta dela.

## 9. Revisar mudanças com /diff

Use `/diff` (ou F3) para inspecionar as mudanças confirmadas das ferramentas de arquivo, seus diffs e as verificações
registradas. O painel também permite desfazer explicitamente e rodar de novo as verificações registradas. Edições
feitas pelo shell não viram snapshots automáticos das ferramentas de arquivo; inspecione-as com Git ou com as ferramentas de arquivo.

## 10. Ver o que os agentes leem e editam (F8)

O F8, o botão **Files** ou `/files` abre um painel compacto à direita: os arquivos que
os agentes leram e editaram neste turno (**t** alterna para a sessão inteira),
qualquer que seja o modelo ou o fornecedor de cada papel. Cada linha começa pelo que
aconteceu (`r` lido, `e` editado, `p` editado por uma sessão delegada na sua árvore
candidata, `x` apagado, `!` recusado ou com falha, `⟳` em andamento) e termina com
quem fez, quando foi uma sessão delegada (`impl`, `expl`, `audit`). Uma linha
recusada mostra o motivo quando selecionada. Um arquivo editado também mostra as linhas
acrescentadas e removidas, e uma edição delegada passa de `p` para `e` quando chega ao
projeto. Abaixo da lista, a árvore do projeto marca o que o Git vê (`M` modificado,
`A` acrescentado, `D` apagado, `U` não rastreado, `!` conflito, `•` pasta com mudança)
e abre em cada arquivo que um agente edita; **c** mostra só os arquivos alterados. O Git
é lido a cada poucos segundos e só enquanto o painel aparece, então mudanças feitas num
editor ou num terminal também aparecem. A linha sob o título conta as mudanças e, quando
houver, os commits à frente (↑) e atrás (↓) do upstream da branch, um rebase ou merge em
andamento e os conflitos. Num arquivo, **Enter** abre o arquivo (uma
linha da lista também o abre na árvore), **d** mostra o que mudou desde o último commit,
**h** lista os commits que mudaram o arquivo (seguindo renomeações; **Enter** num deles mostra
o que ele mudou no arquivo), **a** anexa o arquivo à próxima mensagem como referência `@` (o único jeito de o painel
chegar a um modelo) e **y** copia o caminho. Abaixo de 100 colunas o painel ocupa a
tela inteira, como o F1 e o F2; a partir de 140 colunas fica ao lado deles. O F8 ou o
Esc voltam para a mensagem. O painel só lê o que a conversa já mostra: não chama
nenhum modelo, começa fechado numa máquina nova e depois abre como você deixou.

## Outras teclas úteis

- F4 / `/terminal` — inspeciona os processos da sessão. `/attach` acrescenta um arquivo ou imagem como contexto.
- F6 — próximo passo; Ctrl+End — pula para a saída mais recente.
- F2 — configurações do workspace em terminais pequenos.
- F8 / `/files` — os arquivos que os agentes leem e editam (seção 10).

## Flows opcionais (não necessários no uso diário)

`/session TASK`, `/explore QUESTION` (somente leitura), `/plan DEMAND` e `/research QUESTION
URL` apresentam uma sugestão que você inicia explicitamente. `/work WORK-ID` propõe a implementação de uma Work do grafo. Eles são opcionais; mensagens comuns funcionam sem nenhum grafo ou flow.

### Executar uma Work em outro projeto

1. Abra o projeto com F5 e vincule o grafo dele por `/backlog`.
2. Use `/plan DEMAND`, revise a proposta e aplique (Apply), ou selecione uma Work existente.
3. Use `/work WORK-ID` e inicie o flow sugerido. Para uma proposta não preparada,
   abra Setup → **Create source from this Work**, informe os critérios de aceitação e o
   comando de verificação (um argumento por linha), confira a prévia e confirme a preparação.
4. **Start** roda a revisão do plano, a implementação numa worktree Git isolada, a verificação,
   a admissão e o landing. **Review** inspeciona a evidência retida.
5. **Resume** continua a Work selecionada com o grafo, o projeto, o modelo e os
   limites salvos. Para uma resposta de modelo interrompida, ele autoriza outra chamada só quando
   a limpeza local do transporte está confirmada e o orçamento original ainda resta. A conclusão remota
   e o uso da resposta perdida continuam desconhecidos; as ferramentas não são repetidas.

O projeto precisa ter uma baseline no Git. A identidade do grafo separa os locks de Work, o estado
nativo do coordenador, os trens e os recibos, mesmo quando dois projetos usam o mesmo ID de Work.
O landing atualiza o grafo selecionado. Aplicar um plano, sozinho, não admite execução.

No modo Work, **Pause Work** termina a rodada atual do agente antes de parar.
Espere por “Work paused” antes de fechar o TUI. **Inspect controls** lê a pausa salva
depois de reabrir. **Resume** continua explicitamente a mesma tentativa e o mesmo orçamento.
Para orientar um agente inacabado, inspecione os controles durante a pausa e use **Save note**.
Uma decisão de implementação recusada pode ser reaberta com **Reconsider decision**;
ela ainda precisa de uma nova resposta. Veja [Pausa de Work](../guides/WORK_PAUSE.md) para limites e recuperação.
