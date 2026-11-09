# tare.tools.os

O **Tare** é um workspace de terminal onde você conversa com um agente de programação em linguagem natural, e ele
faz o trabalho por meio de papéis separados e auditáveis: explora o projeto, planeja a mudança, implementa numa
cópia privada, roda os seus checks, pede a revisão de um modelo independente e só então aplica o resultado.

[![Licença: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-brightgreen.svg)](https://python.org)

🇺🇸 [Read in English](README.md) · A documentação completa é em inglês; este arquivo, a instalação e o guia rápido
do TUI têm versão em português em [docs/pt-BR](docs/pt-BR/).

---

## O que ele faz

- **Uma conversa, papéis separados.** Um coordenador responde perguntas direto e encaminha o trabalho de verdade:
  um explorador investiga, um planner escreve o plano, um implementer edita uma cópia candidata, o host roda os
  checks do projeto e um auditor de outra família de modelos revisa. Você aprova a escrita no projeto uma vez por
  tarefa. Veja [uma conversa e seus papéis](docs/guides/CONVERSATION_ROLES.md).
- **Seus modelos, suas contas.** Modelos locais por um gateway compatível com OpenAI, as CLIs oficiais do Claude,
  do Codex e do AGY com o seu login, ou APIs HTTP (OpenRouter, Bedrock, OpenAI). Em todos os casos, o Tare controla
  o loop do agente, as ferramentas e as permissões. Veja [inferência por CLI](docs/guides/VENDOR_CLI_INFERENCE.md)
  e [provedores HTTP](docs/guides/HTTP_API_PROVIDERS.md).
- **Evidência, não promessa.** Cada chamada de modelo, efeito de ferramenta, check e revisão fica registrado. Uma
  mudança só é aplicada depois que os checks passam e uma revisão independente aceita, e uma execução interrompida
  nunca é repetida em silêncio.
- **Histórico durável.** Conversas, planos e recibos ficam fora dos seus projetos e sobrevivem a reinícios e
  atualizações.

## Primeiros passos

Você precisa de Windows 10 ou 11 (Linux também funciona; veja a [instalação do produto](docs/pt-BR/PRODUCT_INSTALLATION.md#linux)),
Python 3.12 de 64 bits, Git e acesso a pelo menos um modelo:

- uma CLI logada: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Codex ou AGY;
- uma chave de API da Anthropic, OpenAI, Google, OpenRouter ou Amazon Bedrock; ou
- um servidor local com API compatível com OpenAI (llama.cpp, vLLM, LM Studio e similares).

Instale a partir do código num ambiente virtual:

```powershell
git clone --recurse-submodules https://github.com/augusto-scarvalho/tare.tools.os.git
cd tare.tools.os
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install "./satellites/tare.tools.kernel[attachments]" ./satellites/tare.tools.agent-runtime `
  ./satellites/tare.tools.specgraph ./satellites/tare.tools.backlog-graph -e .
```

Abra uma pasta de projeto (qualquer pasta serve; não precisa de Git):

```powershell
tare C:/caminho/do/seu-projeto
```

Na primeira vez, digite `/setup` e conecte um modelo: o Tare encontra sozinho as CLIs instaladas (sem gastar
chamada), e ali você pode adicionar uma chave de API ou o endereço de um servidor local. Depois pergunte qualquer
coisa sobre o projeto ou descreva a mudança que você quer. O uso diário está no
[guia rápido do TUI](docs/pt-BR/TUI_DAILY_USE.md).

Conversas e recibos ficam fora dos seus projetos. Numa instalação a partir do código, eles vão para `.reconciliation`,
ao lado do clone; defina `TARE_STORAGE_ROOT` com uma pasta absoluta para guardá-los em outro lugar.

Para escolher os modelos de cada papel:

```powershell
tare models --role coordinator     # lista o que esta máquina pode usar, com uma linha para copiar em cada um
tare C:/caminho/do/seu-projeto --coordinator PROVEDOR/DEPLOYMENT@NIVEL --planner PROVEDOR/DEPLOYMENT@NIVEL
```

A instalação gerenciada (bundles prontos, com atualização automática e rollback) está descrita em
[instalação do produto](docs/pt-BR/PRODUCT_INSTALLATION.md).

## Outras formas de usar

| Entrada | Para quê |
|---|---|
| `tare` / `tare console` | O TUI: conversas, aprovações, histórico, escolha de modelos e papéis. |
| `tare-ops agent` | Uma sessão nativa de agente, a partir de um script ou outro terminal. |
| `tare-ops run <WorkId>` | Executar e assentar um Work explícito de um grafo de backlog ([guia](docs/guides/WORK_RUN.md)). |
| `tare serve`, CLI e MCP | Outros frontends e editores ([CLI e MCP](docs/guides/CLI_MCP.md)). |

## Estrutura do repositório

O `tare.tools.os` compõe o runtime. As peças são repositórios separados, fixados como submódulos Git
([ADR-049](docs/adr/ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md)):

```
tare.tools.os/
├── relay/                                    # Sessões, TUI, fluxo de conversa, delegação, releases do produto
├── tests/                                    # Suíte de testes offline (sem provedor, sem rede)
├── docs/                                     # Guias, decisões (ADRs) e especificações
├── node/                                     # Serviço do nó de GPU: gateway de modelos, lease da GPU (Linux)
├── satellites/
│   ├── tare.tools.kernel/        (@ 7622bc5) # Fronteiras de execução, journals, sessões de terminal
│   ├── tare.tools.agent-runtime/ (@ bbbe120) # Loop de agente com limites
│   ├── tare.tools.specgraph/     (@ cce9edd) # Grafo de especificação e código do projeto
│   ├── tare.tools.backlog-graph/ (@ 0fd1e7c) # Grafo de tarefas com atualização compare-and-swap
│   └── tare.tools.library/       (@ 6e9639e) # Biblioteca técnica e registros de conhecimento
└── scripts/                                  # Ferramentas de release, CI e manutenção
```

Clone com os satélites:

```bash
git clone --recurse-submodules https://github.com/augusto-scarvalho/tare.tools.os.git
# ou, num clone existente:
git submodule update --init --recursive
```

## Desenvolvimento

Leia primeiro o [AGENTS.md](AGENTS.md), que reúne as regras seguidas aqui por qualquer contribuidor, humano ou agente.

- Trabalhe numa branch. A `main` é protegida e só recebe pull requests atualizados que passam no CI (Windows e
  Linux) e na política de changelog. `tare ci pr` envia a branch e abre o pull request com auto-merge.
- Ative os hooks do repositório uma vez por clone com `git config core.hooksPath .githooks`. Depois de cada commit
  numa branch de trabalho o hook faz o push; ele nunca envia `main` nem `dev`.
- Rode só os testes do que você mudou (`python scripts/ops/run_focused_tests.py`); o CI roda o que a mudança alcança
  e a suíte inteira toda noite ([CI feedback](docs/guides/CI_FEEDBACK.md)).
- O CI roda em runners Linux e Windows hospedados pelo GitHub, inclusive para pull requests de forks (o GitHub pede
  que um mantenedor aprove a execução de quem contribui pela primeira vez). As variáveis de repositório
  `CI_RUNNER_LINUX` e `CI_RUNNER_WINDOWS` levam o CI para runners próprios; aí os pull requests de forks o pulam.

O mapa da documentação é o [docs/INDEX.md](docs/INDEX.md); as mudanças relevantes estão no [CHANGELOG.md](CHANGELOG.md).

## Licença

Apache License 2.0. Veja [LICENSE](LICENSE).
