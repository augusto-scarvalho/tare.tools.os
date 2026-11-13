# Tare setup

Open `/setup`. One connection is enough to start: a subscription signed in through its CLI
or an API key. You do not need to fill in every tab.

| Tab | When to use it |
|---|---|
| Connections | Check CLIs, enter API keys and check connections. |
| Local models | Enter the address of a model server you already use. You can skip it. |
| Web search | Configure DeepSeek, SerpApi or Firecrawl for API searches, including with local models. |
| Helpers | Allow the optional helpers and register the key for online Jev. |
| Advanced | Restrict connections, show old models or test a reply. |

In **Connections**, each connection has its own state:

- **Key saved in the secure vault**: a key is saved on this computer; its validity has not been checked yet.
- **Key configured outside Tare**: a key exists in the external configuration, which takes precedence over the vault.
- **No API key configured**: the key for this provider is still missing.
- **Installed and signed in**: the CLI confirmed the sign-in; this does not yet prove that the model replies.
- **Sign-in needed**: run the sign-in command shown and click **Check sign-in**.
- **Sign-in not verified**: the CLI was found, but it does not offer a supported local sign-in check.

**Add API key** opens a masked field. The key goes to the system vault, not
to the history. **Check connection** queries the model list; it does not ask for
a reply. A listed model can still fail to reply. To check
that, use **Test a reply…**: review the cost and only then **Run reply test**.
That last button consumes subscription quota or API credit.

In **Helpers**, there are two different helpers:

- **Jev online (TypeSafe)** is a small hosted classifier: it helps choose routes and classify commands. It requires your own TypeSafe key and uses credit when it receives context for a decision.
- **Local helper (LitJEV/Qwen3.8)** uses a model already loaded on this PC, when the GPU is free. It does not use the TypeSafe key, and this screen does not download or load models.

The helpers are not the implementers. Command review and the sandbox are
configured in **Project permissions**: **Command pre-check** classifies,
**Second opinion** chooses who reviews and **Execution sandbox** isolates execution.
The native Codex reviewer is an option separate from Jev and the local helper.

**Automatic · allow when available** allows the connection to be used when it is available;
it does not mean it was tested. **Do not use** excludes it. A group restriction
in **Advanced** overrides individual choices. Saved conversations keep
their settings.

In **Local models** (or `tare setup --local-gateway URL`, or `TARE_LOCAL_GATEWAY`), the address may use an IP
address or a host name: `http://aaaaa:8080`, `http://box.local:11434` or a Tailscale name all work. Every model that
address serves counts as local and gets the protections tare applies to local models.

Keys are saved immediately. **Save settings** applies the usage choices and
shows a confirmation; **Back · Esc** returns to the conversation. Saving everything as
Automatic also completes the initial setup.

In **Web search**, register a key and choose which service to try first. The agent
can search directly through the API, without calling another model. Opening the screen and saving the key do not
send searches. Results carry links; the agent reads the relevant pages before using them
as a source. See [API search](WEB_SEARCH_APIS.md) for limits, reuse and failures.
