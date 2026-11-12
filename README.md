# Phone Pilot

Control an iPhone (or an Android phone) from a Windows PC. No Mac needed.

Phone Pilot is a small set of Python and PowerShell scripts that run on your own Windows PC. They talk to the phone over USB:

- **iPhone:** [go-ios](https://github.com/danielpaulus/go-ios) (tunnel, developer disk image, port forwarding) and [WebDriverAgent](https://github.com/appium/WebDriverAgent) (taps, typing, reading the screen), signed with your own Apple ID through Sideloadly.
- **Android:** [adb](https://developer.android.com/tools/adb) (USB debugging).

They are written to be driven by the **Phone Pilot** Grok Bot template, but every script also works on its own from a terminal.

## What is in here

| File | What it does |
|---|---|
| `install.ps1` | Downloads the files below into `C:\phonebot\live` and checks each one against a SHA-256 hash. Never touches your `config.json`. |
| `doctor.py` | One-command health check. Says in plain words what works, what is broken and how to fix it. Presses nothing on the phone. |
| `chain_up.py` | Starts whatever is missing (tunnel, disk image, WebDriverAgent, port forwards) for every phone in `config.json`. `--loop` keeps it up. |
| `install_autostart.ps1` | Adds a per-user Task Scheduler task that runs `chain_up.py --loop` after logon (no admin). `-Remove` takes it out. |
| `pp.py` | Low-level phone driver with the one-driver lock. |
| `phone_view.py` | Live window of the phone on your PC: click to tap, drag to swipe, type to type. Needs Pillow. |
| `do.py` | Generic one-call steps for any app: open, read, tap_label, type, scroll, back, dismiss, hide_keyboard, chain. |
| `act.py` | Faster verified actions for a few apps (WhatsApp, Instagram and Discord messages with `--dry-run`, battery, read a web page). |
| `autolock.py` | Reads or sets Auto-Lock, only for long test runs and only with the owner's OK. |
| `config.example.json` | Example `C:\phonebot\config.json` (phones, ports, `blocked_apps`). |

## Requirements

- Windows 10 or 11, Python 3.9+ (`pip install pillow psutil`).
- iPhone: Apple Devices (Microsoft Store) or iTunes for the USB driver, go-ios `ios.exe` in `C:\phonebot`, a WebDriverAgent build signed with your Apple ID (a free Apple ID works but the signature lasts 7 days; a paid developer account lasts a year), Developer Mode on.
- Android: platform-tools (adb) and USB debugging on.
- A data cable. The phone must be unlocked while anything runs: WebDriverAgent cannot type a passcode.

## Install

In PowerShell:

```powershell
irm https://raw.githubusercontent.com/xthemarketmaker-cmyk/phone-pilot/main/install.ps1 -OutFile $env:TEMP\pp-install.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\pp-install.ps1 -Ref main
```

Use a commit SHA instead of `main` to pin an exact version (the Grok Bot template does this). Then copy `config.example.json` to `C:\phonebot\config.json`, fill in your phone, and run `python C:\phonebot\live\doctor.py`.

The Grok Bot template walks through the whole setup (go-ios, Trust, Developer Mode, Sideloadly, config) and only asks you for the physical steps.

## Safety model

- **One driver per phone.** A lock file in `C:\phonebot\locks` stops the bot and the live window from driving the same phone at once. Whoever doesn't hold the lock presses nothing.
- **Locked means stop.** Every action checks the lock screen first; a locked phone gets nothing pressed.
- **Risky labels are refused.** `do.py` will not tap things like Send, Post, Buy, Delete, Follow, Allow or Install unless it is run with `--allow-risky`, which the bot only uses after the owner OKs that exact action. Message verbs check the recipient on screen and support `--dry-run`, which types, checks, then clears the box.
- **Apps the owner rules out are never opened.** `config.json` has a `"blocked_apps"` list (names like `"notes"`, `"photos"`, `"messages"` or bundle ids). It is empty by default; the Grok Bot template's first-run chat asks the owner which apps the bot must never open and writes them there. Every open, launch and app-specific action checks it first and refuses with nothing pressed. Popups with a real choice are left to the owner.
- **Nothing leaves the PC by itself.** No telemetry. Logs stay in `C:\phonebot\logs`. The scripts' own logs avoid device IDs, but go-ios's logs there can contain your phone's UDID, so don't post them publicly. `doctor.py` output never shows IDs.

This is a hobby project, not a product with a warranty. It drives your real phone; read what a command does before you run it.

## Credits and licences

- [go-ios](https://github.com/danielpaulus/go-ios) by Daniel Paulus (danielpaulus) and contributors, MIT licence. Not included here; download it from its releases page.
- [WebDriverAgent](https://github.com/appium/WebDriverAgent), originally by Facebook, maintained by the Appium project, BSD licence (Copyright (c) 2015-present, Facebook, Inc.). Not included here; download `WebDriverAgentRunner-Runner.zip` from its releases page and sign it yourself.
- [Sideloadly](https://sideloadly.io) is a separate free tool, not affiliated with this project.
- Android [platform-tools](https://developer.android.com/tools/releases/platform-tools) are provided by Google under their own terms.

Phone Pilot's own scripts are MIT licensed (see `LICENSE`). Apple, iPhone and iOS are trademarks of Apple Inc.; this project is not affiliated with Apple.
