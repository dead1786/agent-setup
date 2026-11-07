# AI Assistant Starter Kit (agent-setup)

English ｜ [繁體中文](README.md)

**One command gives your Windows PC an AI assistant you can chat with — and hand work to — from Telegram.**

This is a starter kit for **trying out** your own personal AI assistant: no coding needed, answer a few questions and it is set up. It is not an enterprise product and comes with no support, so please read **Risks and notes** below before installing.

> The installer and the assistant speak Traditional Chinese by default; you can set the assistant's reply language during setup.

---

## What it does

| Feature | Details |
|---|---|
| Your choice of AI | Claude Code, Codex (OpenAI) or Gemini (Antigravity), with your own account |
| Telegram remote | Message it from your phone; send photos, files and voice notes and it replies |
| Reminders | Just say "remind me at 8 tomorrow…" or "every Monday morning…" |
| Self-healing | Reopens the AI window if it is closed and starts again after a reboot |
| Safety guard | Blocks risky actions such as deleting whole folders, formatting drives, reading credential files or switching the guard off; you allow one at a time with `/allow` on Telegram |
| Password vault | When it logs in to a site for you, the AI never sees the password; every fill waits for your "Allow" tap on Telegram |
| Updates | `/update` to update, `/還原` (restore) to roll back |

## You will need

- A Windows 10 or 11 PC with internet access
- An AI account: Claude (Pro / Max), ChatGPT (Plus or above) or a Google account, depending on the AI you pick
- Telegram (optional — without it you use the assistant on the PC only). Before installing, message @BotFather on Telegram, send `/newbot` and follow the prompts to get your bot token; setup asks you to paste it.

## Install

1. Press `Win`, type `PowerShell` and open "Windows PowerShell"
2. Paste this line and press Enter:

```powershell
irm https://raw.githubusercontent.com/dead1786/agent-setup/main/bootstrap.ps1 | iex
```

3. Answer the questions (which AI, its name and personality, what to call you, whether to connect Telegram); you will be asked to sign in to your AI account
4. Setup checks itself at the end; when the summary shows everything passed, you are done

## Everyday use

- **At the PC**: type in the AI's window
- **On Telegram**: message your own bot — it only listens to your account

| Telegram command | What it does |
|---|---|
| `/status` | Is the AI running? |
| `/stop` | Interrupt what it is doing |
| `/restart` | Reopen the AI window (continues the conversation) |
| `/new` | Reopen with a fresh conversation |
| `/screen` | Send you a screenshot of the PC |
| `/allow` | Let the action the guard just blocked through once (valid 10 minutes) |
| `/update` / `/還原` | Update to the latest version / roll back |
| `/密碼` | Password vault: save, list and delete logins |

## Uninstall

```powershell
irm https://raw.githubusercontent.com/dead1786/agent-setup/main/uninstall.ps1 | iex
```

It removes only what the kit installed and restores changed settings from backups; you can save the assistant's memory to your desktop first.

---

## ⚠️ Risks and notes (please read first)

1. **The AI acts on its own.** It can see the screen, click, type and read or write files without asking at every step. Its rules tell it to ask before deleting files, spending money or messaging anyone for you, but AI makes mistakes — back up anything important.
2. **The guard is a safety net, not a vault.** It matches command text, so it stops slips and common tricks, but an AI fooled by instructions hidden in a web page, email or someone's code may still find a way around it. **It does nothing against a person sitting at your PC.**
3. **Do not feed it untrusted content.** If a stranger asks you to "have your AI look at my project / click this link", it is safer not to. The assistant's rules say instructions inside outside content are data and it must ask you first.
4. **Your Telegram bot token is a key** — never share it. `/screen` sends your whole screen to Telegram; mind what is on it.
5. **Usage and cost** come from your own AI plan. Reminders, schedules and long tasks all use your quota.
6. **Antivirus false alarms** are possible: the kit opens windows and sends text to them, which some antivirus tools flag.
7. **Third-party terms**: follow the terms of the AI service and of Telegram that you use.
8. **No warranty, no support** — see the license below.

## License

[MIT License](LICENSE) — free to use, modify and share, including commercially; provided as is, with no warranty of any kind.
