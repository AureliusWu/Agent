# Security Model

## Protected Assets

- Files inside the user-selected workspace and their reversible backups.
- Model credentials stored in Windows Credential Manager or backend environment variables.
- Conversation, memory, audit, approval, and recovery data in SQLite.
- The authority to run commands, call MCP services, and transmit data to external providers.

## Trust Boundaries

The Tauri shell is the trusted desktop client. It starts the loopback backend with a fresh process-scoped API token. Model providers, MCP servers, project files, logs, Skills, tool output, uploaded scripts, generated comments, and external HTTP responses are untrusted inputs. A selected conversation is the authority for both workspace and permission mode; request bodies cannot widen either value. Web/PWA clients are not part of the v8.0.3 product repository.

## Security Invariants

1. Every built-in filesystem tool path resolves beneath the selected workspace and cannot traverse symlinks, UNC paths, device paths, or the internal backup directory.
2. Every side effect passes the same permission and approval pipeline, including MCP, memory, uploads, rollback, and recovery.
3. Approval capabilities are bound to the conversation, task, workspace, tool, exact arguments, risk, expiry, and requested scope.
4. Untrusted content is data, not authority. It cannot change system rules, sandbox boundaries, network policy, credential handling, or user constraints.
5. Credentials are redacted before model and logs, and credential-bearing MCP arguments are denied. Approved local commands receive their exact arguments; that flow is classified and audited without storing the secret value.
6. External requests validate scheme, destination, redirects, method, response size, and credential origin; private and metadata addresses are denied by default.
7. High-risk local execution has a persisted checkpoint, database backup, Git state, task state, and bounded workspace snapshot before it runs.

## Primary Abuse Cases

- A malicious website or local process calls the loopback API and claims `full` permission.
- README, Skill, MCP, log, or tool output asks the model to ignore rules or exfiltrate secrets.
- An MCP URL reaches localhost, a private network, or cloud metadata through DNS or redirects.
- A model or MCP response smuggles an unknown command or oversized payload.
- A critical command modifies files outside the expected set or leaves the workspace partially changed.

Repository: AureliusWu/Agent
Version: v0.11.0 / Round 13

## Residual Boundaries

- A confirmed local executable is not an operating-system sandbox. It can have the Windows account's ambient authority, so command execution remains critical, shell-free, bounded in time, and preceded by a rollback snapshot.
- Snapshot archives remain local and inherit the current Windows user's filesystem protection; they are not separately encrypted.
- Clipboard, generic export, and generic download tools are absent. Network attachments are denied rather than downloaded implicitly.
