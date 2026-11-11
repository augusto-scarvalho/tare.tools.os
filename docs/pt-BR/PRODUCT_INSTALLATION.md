# Instalar, atualizar e voltar uma versão

> Versão em português. A referência é a [versão em inglês](../guides/PRODUCT_INSTALLATION.md).

O bundle contém os cinco pacotes tare.tools, todas as dependências em wheels,
`requirements.lock`, os commits e hashes de origem e um instalador Python.
No Windows, use Python 3.12 de 64 bits para os bundles `win32` produzidos nesta máquina.
No Windows, o bundle inclui MinGit com shell BusyBox nativo para comandos
não interativos, com versão e SHA-256 fixos e todos os arquivos no manifesto.
O launcher usa esse Git somente no PATH da release, sem alterar o Git da máquina.
Git Bash/MSYS e extensões como Perl não fazem parte desse contrato de execução;
o shell nativo evita a incompatibilidade de MSYS com o confinamento MXC.
Não é necessário
clonar o ecossistema nem acessar um índice de pacotes durante a instalação.

Extraia o bundle em uma pasta e execute nela:

```powershell
python install.py install .
& "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" console --root C:/meu-projeto
```

Para usar apenas `tare` em qualquer terminal Windows, execute uma vez no checkout:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ops/install_product_tasks.ps1 -CommandOnly
```

Abra um novo terminal. O comando usa a instalação gerenciada, com atualização
automática, e tem precedência sobre um `tare.exe` de desenvolvimento antigo.
O PATH aponta para o launcher fixo em `product`, não para uma release: cada
abertura acompanha a versão ativa, inclusive após auto-update ou rollback.
Esse modo altera somente o PATH do usuário; não registra nem reinicia serviços.
A configuração completa de tarefas também configura esse comando.

```powershell
cd C:/projects/meu-projeto
tare
# Ou, de qualquer pasta:
tare C:/projects/meu-projeto
tare "C:/projects/projeto com espaços"
```

`tare` abre a pasta atual; o caminho explícito pode ser absoluto, `./relativo`,
`../outro` ou `~`. A pasta deve existir. Use `./status` para uma pasta cujo nome
coincida com um subcomando. `tare status` continua mostrando a instalação.
No Linux/macOS, coloque o diretório do launcher `tare` no PATH da sua shell.

`RUN_TUI.bat` abre essa mesma instalação. Uma instalação a partir dos fontes (veja o
README) roda o `tare` do próprio clone. **Settings · F2** informa a versão efetivamente
carregada e identifica release ou desenvolvimento.

Para instalar em outra pasta, use `python install.py --home C:/tare install .`.
Nesse caso, o launcher é `C:/tare/tare.cmd`; configure `TARE_PRODUCT_HOME=C:/tare`
para usar também `RUN_TUI.bat`. Prefira caminhos curtos no Windows.

## Autenticar e concluir uma tarefa

Escolha um modelo no TUI. Perfis próprios podem ser importados com
`tare.cmd profiles import meu-modelo C:/perfis/modelo.json`. Cada entrada tem seus
perfis: a instalação usa `product/state/profiles`; a instalação a partir dos fontes usa
`.reconciliation/profiles` ao lado do clone, ou `profiles` dentro de `TARE_STORAGE_ROOT`.

Ao abrir, o TUI procura as CLIs do Claude, do Codex e do AGY (PATH e pastas padrão de instalação) e põe no
seletor os modelos de cada uma que encontrar, sem gastar chamada de modelo. Ele pergunta localmente a cada CLI se
ela está logada; uma CLI instalada sem login aparece num aviso com o comando que faz o login. Perfis que você
escreveu ou editou nunca são substituídos.
`TARE_CLI_DISCOVERY=0` desliga a busca.
Em **Settings → Connection details → Authenticate**, informe a chave do perfil
selecionado. Ela vai para o cofre do sistema operacional e permanece disponível
entre versões. **Check** consulta o endpoint quando o protocolo oferece essa
verificação; salvar uma chave não confirma acesso. Perfis de CLI continuam
usando o login do próprio provedor. Uma variável de ambiente já definida tem
precedência sobre o cofre; o TUI informa esse caso antes de salvar.

Envie a tarefa, confira os arquivos alterados e o resultado no TUI. O histórico
fica em `product/state`, separado dos ambientes de execução. `TARE_STORAGE_ROOT`
permite selecionar uma pasta de histórico existente, inclusive para continuar
usando o histórico de uma instalação anterior não gerenciada.
Nesse caso, informe também a raiz de controle original ao abrir:
`tare.cmd console --os-root C:/raiz-original --root C:/meu-projeto`.
A combinação de projeto e raiz de controle identifica as conversas legadas;
selecionar apenas sua pasta de armazenamento não muda essa identidade.

Na instalação gerenciada, essa identidade permanece em `product/control`,
independente da versão ativa. Use o launcher `tare.cmd` gerado pelo instalador;
o setup do comando acima evita que um `tare.exe` antigo no PATH abra a instalação anterior.

## Conferir a versão instalada

Consulte o estado da instalação gerenciada pelo launcher:

```powershell
& "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" status
```

A saída informa `current`, `previous` e as versões e commits dos cinco pacotes
efetivamente selecionados para novas aberturas. Consulte essa saída para saber
o estado desta máquina. O arquivo
`docs/operations/PRODUCT_RELEASE_INPUTS_WIN312_2026-09-27_0.1.63.json`
preserva os inputs da release histórica 0.1.63.

## Atualizar ou voltar

Para usar a nova versão no TUI, abra-o novamente após a atualização:

```powershell
& "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" update C:/downloads/novo-bundle
& "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" status
& "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" rollback
```

A atualização instala offline em um ambiente separado, confere hashes,
dependências e imports, e só então troca a versão ativa. Uma falha mantém a
versão anterior. Repetir a instalação do mesmo bundle não apaga o ponto de
retorno. `rollback` troca entre as duas versões retidas, sem reinstalar pacotes,
apagar perfis ou desfazer arquivos do projeto. Abra novamente o TUI após a troca.
Releases que mudarem formatos persistidos precisam manter leitura compatível
para oferecer esse mesmo retorno; o instalador não converte dados de usuário.

## Qualificação limitada I2I-06

O [modelo executável da transição atual](../guides/PRODUCT_TRANSITION_MODEL.md) declara
limites, hipóteses de recuperação, controles negativos e testes offline dos
cortes reais de persistência/Serve. Complementa I2I-04/05; não promete migração
simultânea dos processos, durabilidade de diretório sob perda de energia ou
recuperação automática sob falha permanente.

## Contrato de transição I2I-01

`active.json` é a autoridade **para novas aberturas** pelo launcher gerenciado.
Até o selo da transição, `current` continua apontando para N. O sucessor é
preparado lado a lado: verificação do bundle e de sua identidade, instalação
sem índice, `pip check`, imports e gravação de `ready.json`. Antes da ativação,
o instalador verifica novamente o bundle, o Python instalado, a identidade
`ready.json.bundle` e as dependências. `rollback` também qualifica o destino
antes de trocar o ponteiro.

Sob o lock existente de instalação, uma única substituição atômica de
`active.json` publica `current`, `previous` e `last_transition` juntos. O arquivo
temporário é gravado, descarregado e sincronizado antes de `os.replace`.
Falhas de qualificação, sincronização ou substituição anteriores ao selo não
reescrevem os bytes do ponteiro anterior. Isso não é uma transação de toda a
instalação: um candidato preparado ou arquivos do bootstrap podem permanecer
após uma falha. Não se promete durabilidade contra perda de energia do diretório.

Formato do último recibo (IDs abaixo ilustrativos; são SHA-256 completos do
`bundle.json`, não versões de pacote):

```json
{
  "current": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "previous": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "last_transition": {
    "from": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "to": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "operation": "update",
    "at_utc": "2026-10-16T12:00:00Z",
    "qualification": "releases/bbbbbbbbbbbbbbbb/ready.json"
  }
}
```

`operation` é `install` na primeira ativação (`from` e `previous` nulos),
`update` na troca por instalação e `rollback` no retorno. `at_utc` é o relógio
UTC local, com precisão de segundos; não é uma ordenação global.
`qualification` é uma referência relativa à raiz da instalação para o
`ready.json` do destino, cujo conteúdo é `{"bundle": "<SHA-256 completo>"}`.
O recibo é local, não assinado, e guarda somente a última troca: não é um log
de auditoria nem prova de procedência ou de saúde de processos já em execução.
`status` o expõe junto ao ponteiro. Reinstalar o bundle já ativo preserva os
bytes do ponteiro, inclusive o recibo e o ponto de retorno. Ponteiros legados
sem `last_transition` continuam válidos; só uma troca efetiva acrescenta recibo.

O retorno alterna `current` e `previous` e substitui o último recibo; destino
incompleto, identidade divergente ou ausência de versão anterior impede a troca.
`prune` conserva as duas versões apontadas também quando há recibo, sem alterar
`active.json`; outras versões que o sistema não permita renomear ficam para
outra tentativa. Isso não equivale a rastrear todos os processos vivos.

TUIs e Serve já iniciados podem continuar em N: a troca não revoga nem migra
esses processos. A política existente de reinício do Serve é separada do selo;
ativação não significa que todos os processos estejam na mesma versão.
Não estão cobertos por I2I-01: freeze global, novos protocolos de concorrência,
canários, migração de dados, consenso ou as garantias adicionais de I2I-02.
Compatibilidade de dados persistidos continua sendo responsabilidade da release.

### I2I-04: observar uma rotação exata, sem executá-la

O bootstrap `product.py` (também distribuído como `install.py`) oferece
`transition-status`: leitura somente, sem lock de escrita, update, recovery,
processos novos ou replay de Work/tools. Exemplo PowerShell, usando **o mesmo
home absoluto e o mesmo bootstrap** antes e depois da transição autorizada:

```powershell
$productHome = "$env:LOCALAPPDATA/tare.tools/product"
$productPython = 'C:/caminho/para/python.exe'
$productBootstrap = "$productHome/product.py"
& $productPython -I $productBootstrap --home $productHome transition-status
# Registrar current=N, previous e server.release/installation/pid; guardar a saída.
# O proprietário executa separadamente a atualização N -> N+1 já autorizada.
& $productPython -I $productBootstrap --home $productHome transition-status
```

Guardar as duas saídas fora do produto e comparar os IDs SHA-256 completos do
sujeito exato: N antes, N+1 depois e previous=N. Não inferir identidade de nomes
ou versões. `completed=true` / `qualified_rotation_complete` exige último recibo
`update` do par, trial catalog PASS fixado por digest, na fase `canary`, com
`selected=candidate`, incumbent=N e candidate=N+1, ready.bundle=N+1 e saúde
protocol-1 da mesma instalação em N+1,
sem drain/stop pendente. Cada arquivo e resposta de saúde tem limite de 64 KiB.
O ponteiro é relido após a observação: `pointer_changed` descarta a combinação;
`pointer_recheck_failed` preserva incerteza. Isso não é snapshot transacional nem
prova criptográfica contra um escritor local malicioso.

`reasons` contém códigos estáveis: por exemplo `catalog_trial_missing`,
`catalog_digest_mismatch`, `catalog_trial_mismatch`, `ready_identity_mismatch`,
`receipt_pair_mismatch`, `evidence_path_invalid`, `evidence_missing`,
`evidence_malformed`, `evidence_oversized`, `evidence_io_error`,
`health_unavailable`, `server_identity_mismatch`, `incumbent_still_serving` e
`server_not_settled`. `serve` separa selected/incumbent/foreign_or_legacy/unknown;
um ponteiro ativo ou servidor saudável sem trial fixado não basta. Instalação
inicial, rollback explícito e automático aparecem como `initial_install`,
`rollback` e `auto_rollback`, nunca como rotação qualificada.

**Sole managed Serve** significa o único incumbent ativo do endpoint gerenciado,
identificado por instalação/release/PID; não significa todos os processos da
máquina. TUIs antigos podem continuar abertos em N e não são uma segunda
autoridade de atualização. O comando não enumera listeners nem prova a ausência
de outros servidores; essa exclusividade é qualificada separadamente pelo
maintainer. I2I-01/02 continuam donos de seleção/drain/recovery e I2I-03 do trial.

O status não prova preservação de dados nem migração de todos os processos.
A rotação real autorizada, hashes antes/depois dos arquivos duráveis de custódia
selecionados e reabertura pelo TUI público são evidência separada do maintainer,
não resultado inventado por fixture unitária. O roadmap original issue26 pede
transição de produção exata autorizada pelo owner, recibo exato, incumbent ativo
único e rollback retido; não acrescenta consenso distribuído.

Esse comando precisa existir no bootstrap usado para observar. Na primeira
atualização que o introduz, registre antes `tare status` e a saúde do Serve;
use `transition-status` após a atualização. Os recibos do updater N continuam
sendo a prova da qualificação anterior à ativação.

### I2I-05: observação limitada de duas instalações independentes

`python -B scripts/ops/qualify_product_cohort.py packet.json` (ou `-` para stdin)
produz somente JSON em stdout. A função `qualify(packet)` aceita dict, string JSON
ou bytes UTF-8. Limites: 256 KiB, 16 nós/observações, 32 pacotes e rótulos de até
128 caracteres. O ID local mantém o formato atual de 64 hex minúsculos; esse
formato não o torna global nem autenticado. Chaves JSON duplicadas são rejeitadas.
Não há SSH, scheduler, atualização, rollback, provider ou escrita de arquivos.
Use `-B` para não criar bytecode ao carregar módulos em ferramentas próprias.

Formato exato do envelope v1 (metavariáveis abaixo, não evidência real):

```text
{
  "schema": "tare.tools/product-cohort/1",
  "expected_nodes": ["LAPTOP-01", "WORKSTATION-01"],
  "expected_sources": {"<nome de cada pacote>": "<commit Git completo, 40 hex minúsculos>"},
  "observations": [
    {
      "node": "<um nó declarado>",
      "installation": "<hash local esperado, 64 hex minúsculos>",
      "status": <objeto integral da saída pública status>,
      "transition_status": <objeto integral da saída pública transition-status>
    }
  ]
}
```

Declarar todos os pacotes esperados, não apenas os que responderam. Cada status
contém `current`, `previous` (SHA-256 completos distintos), `packages` com
`name`, `version`, `commit`, e `last_transition` update com from/to e qualification
exatos. A referência catalog_trial precisa de caminho do candidato e digest
SHA-256 completos; a prova local de canário continua a cargo de I2I-04.
O transition-status exige schema `/1`, completed booleano true,
reasons exatamente `["qualified_rotation_complete"]`, serve selected, o mesmo
par e server com release=current, installation igual ao ID declarado,
PID inteiro positivo (o protocolo já foi conferido pelo observador local e não
é exportado em server). Os demais campos públicos
são tolerados; não substituem os vínculos exigidos. O verificador reutiliza a
qualificação pública I2I-04; não revalida arquivos de trial via JSON fornecido.

Resultados: `authority=NONE`, `observation=PASS|PARTIAL|REJECTED`, razões globais
limitadas e `nodes` com node, installation, observation e razões limitadas.
PASS exige todos os nós declarados qualificados e nenhum erro de envelope;
PARTIAL preserva cada irmão PASS mesmo com nó ausente, inacessível, duplicado ou
divergente; sem sucesso é REJECTED. Omitir uma observação representa nó
inacessível; um objeto sem status/transition_status também não qualifica.
Rótulos duplicados ignorando maiúsculas não contam duas vezes. Declarações
inválidas rejeitam o envelope; observações extras impedem PASS global.
Exit codes: 0 PASS, 1 PARTIAL, 2 REJECTED.

LAPTOP-01 e WORKSTATION-01 têm o **mesmo hash local de instalação**, pois o caminho
local usado pelo protocolo é igual. Ele não identifica uma máquina globalmente:
a chave de observação é nó declarado + ID local opaco. IDs locais iguais e
bundles de plataformas diferentes são permitidos se o conjunto completo de
inputs de pacote for idêntico ao declarado. Não alterar o protocolo/ID.
Labels e JSON fornecidos não provam autenticidade remota nem frescor. Coleta
honesta por SSH autenticado existente, custódia e confirmação do sujeito exato
continuam responsabilidade do maintainer após revisão; fixtures offline não
são observações reais dos nós.

`notify(peers)` apenas solicita o updater de cada máquina. `updates_started`
true significa pedido aceito, **não rotação concluída**. Falhas OS, timeout
(60 s por pedido) ou retorno não zero ficam false independentemente, sem retry
ou encerramento explícito de processos, e não pulam os próximos peers. Cada nó
mantém seus locks, recibos e recovery; pipeline protegido não muda. Não há
consenso, lock global, execução remota nova, estado de frota ou autorização
emitida pela observação. Reconciliar evidência com o owner canônico existente,
não criar outra autoridade nem repetir efeitos.

## Transição gerenciada I2I-02

Install/update/rollback manual e a operação automática inteira usam o mesmo
`install.lock`: concorrentes falham sem entrar; o lock do sistema é liberado
quando o processo termina. Antes de cada troca, o par current/previous capturado
é conferido novamente. Qualificação anterior ao selo não altera o ponteiro.

A atualização reconcilia primeiro a versão já selecionada com o Serve. Uma
reconciliação pendente impede baixar outro candidato. Replay após ativação tenta
somente concluir essa transição, sem repetir ferramentas ou Work. Não há freeze
nem migração de TUIs; a compatibilidade dos dados continua a cargo da release.

O reinício usa `/health` e `POST /control/drain`, bearer `tare-local`, protocolo 1,
identidade exata pid+release+installation (SHA-256 de normcase(resolve(home))) e
owner UUID. A lease de 120 segundos é renovada enquanto trabalhos terminam;
novas requisições ficam bloqueadas pelo fence do Serve. Só o mesmo owner, com
busy=0, pode parar o servidor. A porta precisa fechar antes de iniciar a tarefa
`Tare-Serve`. Não se usa taskkill, encerramento de tarefa ou busca de listeners.
Servidor legado, identidade estranha ou porta ocupada sem saúde gerenciada adiam
a operação com diagnóstico. O deadline de ocupação libera a lease; falha de
comunicação ao liberar deixa a lease expirar. O adapter de Task Scheduler é
Windows; a lógica pode ser exercitada com adapter de fixture também no Linux.
Sem tarefa gerenciada e sem servidor ou porta ocupada, a atualização continua
normalmente e informa `no serve task`. Um Serve selecionado em drain adia a
busca de outro candidato.

Se B não iniciar, A (previous) é validada e selecionada atomicamente com recibo
`auto_rollback`. A também precisa responder com instalação e release exatas.
Se falhar, o comando termina nonzero e mantém A selecionada. Replay recupera A,
sem alternar de volta para B. Adiamento de segurança não autoriza matar processos
nem trocar outro candidato. O recibo não comprova saúde por si só.
Se o retorno foi uma escolha manual, falha ao iniciar essa versão mantém a
escolha e termina com erro; não desfaz o rollback do operador.

`active.json` retém apenas a última política `auto_update`, com `paused` e `after`,
junto do último recibo. Rollback manual pausa novas versões. Para limpar pausa e
barreira, sem inventar recibo de ativação, execute:

```powershell
tare auto-update --resume
```

Esse comando só limpa a política; a próxima execução automática reconcilia e
procura candidatos. Auto rollback grava `after=B` (ID SHA-256 completo), excluindo
B e publicações anteriores. C posterior pode seguir. A busca é limitada às 30
publicações e aos 10 candidatos mais recentes com CI: fronteira ausente não
permite inferir ordem nem downgrade. Reinstalar o bundle ativo preserva os bytes
do ponteiro, recibo e política. Não há journal, banco, histórico ilimitado ou
migração de dados. O controle seguro exige o protocolo 1 nas versões participantes;
um servidor legado precisa de transição supervisionada para essa geração.

## Shadow e canário antes da promoção (I2I-03)

Uma atualização gerenciada tem agora uma qualificação delimitada do protocolo
de descoberta de modelos. Após preparar e validar o bundle de N+1, o instalador
com suporte a I2I-03 inicia N e N+1 em dois processos descartáveis, em portas
loopback privadas. Usa o handler real de cada release com um perfil embarcado,
bearer aleatório, estado e configurações vazios e separados. Esses endpoints
aceitam apenas `GET /v1/models`; POSTs, chats e ferramentas são recusados.
Não são copiadas conversas nem executadas inferências.

Primeiro, shadow compara uma resposta de cada versão. Depois, dez requisições
de **tráfego de qualificação gerado** são distribuídas deterministicamente:
oito para N e duas para N+1 (20%). Cada resposta de N+1 é comparada com uma
resposta nova de N; somadas ao shadow, são até 14 tentativas HTTP. O comparador
valida a estrutura do catálogo e preservação dos IDs e proprietários anteriores.
Adições de modelos e mudanças válidas de janela de contexto são permitidas.
Não é uma avaliação da qualidade das respostas dos modelos.

O limite padrão é zero erros de candidato: a primeira falha interrompe o canário,
seleciona N e impede a promoção. Uma falha na própria referência N é
`INCONCLUSIVE`; não se inventa sucesso nem evidência contra N+1. Há deadline
compartilhado de 30 segundos, até três segundos por GET e resposta de até 64 KiB.
Os processos próprios são encerrados e aguardados; a limpeza tem limite de cinco
segundos antes de terminar o mesmo processo. No Windows, o probe usa o Python
base com `-I -S` e os pacotes da release verificada para controlar o PID real,
sem confundi-lo com o redirector do venv.
Ambas as plataformas usam `-B` explícito e atestam bytecode desabilitado no
handshake, preservando também os caches de código da instalação.

Somente `PASS` permite trocar o bootstrap e selar `active.json`. O recibo de
ativação referencia `releases/<id>/catalog-trial.json` e seu SHA-256. O arquivo
`last-catalog-trial.json` na instalação guarda a última tentativa, inclusive
recusas, com IDs, contadores e hashes; não guarda payloads, prompts ou chaves.
Esses dados têm destino no diagnóstico da transição e na próxima decisão de
rotação, sem um coletor ou histórico crescente.

Na atualização automática, uma rejeição reutiliza `auto_update.after` para não
baixar e experimentar de novo a mesma publicação. Current, previous e o último
recibo de ativação são preservados; `prune` remove a preparação descartada e
conserva as releases protegidas. `INCONCLUSIVE` não condena a publicação.
`tare auto-update --resume` mantém o mecanismo existente para limpar a barreira.
Rollback manual e recuperação de falha posterior ao selo continuam por I2I-02.
Reinstalar o bundle já ativo não repete o trial nem troca seus recibos.

Esse contrato cobre o catálogo do produto e tráfego de qualificação. Tráfego
comum, ferramentas, migração de dados e roteamento gradual de chats não são
reproduzidos por ele. Um updater anterior a I2I-03 continua usando seu contrato
anterior para instalar esta primeira versão; a qualificação prévia passa a valer
nas atualizações conduzidas pelo novo updater. Isso prepara uma primeira rotação
concreta em I2I-04, sem alegar uma rotação geral já concluída.

No dogfood por ConPTY, envie seleção/exclusão/colagem em ações de teclado
separadas e confira o valor numérico exibido antes de iniciar o agente. Agrupá-las
numa escrita pode alterar o limite efetivo.

## Produzir um bundle

Use o ambiente de build e as revisões exatas do arquivo de inputs da release:

```powershell
python scripts/ops/build_native_release.py --lock inputs.json --roots roots.json --output build
python scripts/ops/pack_native_release.py --release build --output bundle
```

O primeiro comando exporta bytes de commits completos, exclui alterações locais
e exige wheels idênticos em duas construções. O segundo resolve as dependências
uma vez e preserva seus bytes e hashes. Conserve o bundle completo para repetir
a instalação; resolver novamente as dependências em outra data pode escolher
versões diferentes. Os hashes detectam alterações, não autenticam a procedência
de um bundle recebido de terceiros. Bundles são por plataforma: gere o de Linux no
próprio Linux. Windows e Linux (Ubuntu/WSL2) com Python 3.12 foram verificados;
outros sistemas precisam de bundles e validação próprios.

## Linux

Pré-requisitos: Python 3.12 com `venv` (no Ubuntu, `sudo apt install python3.12-venv`)
e Git. A instalação fica em `~/.local/share/tare.tools/product`, com o launcher
`tare` ao lado de `product.py` (`~/.local/share/tare.tools/product/tare status`).
Sem Secret Service (WSL ou servidor sem desktop) o keyring não guarda chaves:
defina a variável indicada no perfil, por exemplo `OPENROUTER_API_KEY`.
Smoke de 26/09/2026 no WSL2: instalação, tarefa, credencial por variável, TUI,
atualização e rollback. Evidência.

## Atualização automática

Os mantenedores publicam as releases: cada push que muda o `main` de um dos cinco repositórios pode virar
uma release. `python scripts/ops/publish_product.py` gera o bundle a partir de espelhos próprios (em
`%LOCALAPPDATA%/tare.tools/publisher/mirrors`, nunca os checkouts de desenvolvimento), testa a instalação numa
pasta descartável e publica a release `product-<id>` no GitHub. Na máquina dos mantenedores, uma tarefa agendada,
`Tare-Publish`, faz isso.

Em cada máquina, a tarefa `Tare-Update` roda `tare.cmd auto-update` no logon e a cada 5 minutos (usa o
`gh` já autenticado). Ela instala a release nova ao lado da atual e usa a
transição I2I-02 descrita acima para reconciliar o Serve com segurança. O TUI nunca
é fechado: ele avisa uma vez que há atualização instalada, e a próxima abertura
já usa a nova versão; conversas continuam de onde pararam.

A release é assinada (ADR-075). O workflow a cria como rascunho; um job separado, o único que guarda a chave
Ed25519 do projeto, assina o bundle, confere a assinatura e só então envia `bundle.zip.sig` e publica o rascunho.
Uma release publicada à mão com `publish_product.py --here` é sempre criada como rascunho: essa máquina não tem a
chave de assinatura, e as cópias instaladas ignoram uma release sem assinatura. Assine o rascunho com a chave de
recuperação e publique-o com os comandos no topo de `scripts/ops/release_signing_key.py`.
A assinatura cobre `tare.tools product bundle` e uma quebra de linha, seguidos do SHA-256 de `bundle.zip` em
hexadecimal. Para conferir um download à mão, a partir do bundle extraído:
`python install.py verify-signature bundle.zip bundle.zip.sig` (ou `python relay/native_product.py
verify-signature ...` num checkout); nenhum dos dois precisa de instalação, e o código de saída é 0 quando a
assinatura é válida. As cópias instaladas exigem a assinatura: o atualizador só considera releases que têm o asset
`bundle.zip.sig`, baixa-o junto com o bundle e o verifica com as chaves embutidas antes de extrair qualquer coisa.
Um bundle cuja assinatura falta ou não confere não é instalado: `auto-update` informa `refused` com a tag, deixa a
instalação como estava e tenta de novo numa execução posterior, de modo que uma release corrigida à mão com a chave
de recuperação ainda é instalada.
