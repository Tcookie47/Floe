# Installing Floe — step-by-step guide for team members

This guide takes you from a fresh laptop to a working Floe with your team's
profile loaded. It is the short path; [USAGE.md](USAGE.md) remains the full
reference and the [README](README.md) covers development and the frozen macOS
desktop app.

Floe runs entirely on your own machine and is read-only: it never writes to
Iceberg or Nessie.

Current release: **v1.2.0**. Check the
[Releases page](https://github.com/Tcookie47/Floe/releases) for a newer
version and substitute it in the wheel URL below (both the `v1.2.0` folder and
the `1.2.0` in the file name):

```
https://github.com/Tcookie47/Floe/releases/download/v1.2.0/floe-1.2.0-py3-none-any.whl
```

---

## 0. What you need before you start

- [ ] A laptop running **macOS 12+**, **Windows 10/11 (x64)**, or a **Linux
      desktop** distribution.
- [ ] About **600 MB** of free disk space.
- [ ] **Admin rights** to install Python 3.12 if it is not already installed.
- [ ] The **profile file** from the Floe maintainer (for example
      `team.floe-profile.json`). It holds settings only, no secrets.
- [ ] Your own two secrets, which are *not* in the profile file:
  - the **ADLS storage account key**, and
  - the **Nessie client secret**.

  Get them from the Floe maintainer or your data platform admin, through the
  approved secure channel. If you have Azure portal access:
  - *Account key:* Storage account (for example `mystorageacct`) →
    **Security + networking** → **Access keys** → key1 → **Show** → copy.
  - *Nessie client secret:* ask whoever manages the Azure AD app registration
    used for Nessie (**Certificates & secrets**).

  **Never paste secrets into chat, email or tickets.**
- [ ] **VPN access** to the Nessie server.
- [ ] Internet access to these hosts (first install and first query):
  `github.com`, `pypi.org`, `files.pythonhosted.org`,
  `extensions.duckdb.org`, and (optional, for Ask) `openrouter.ai`.

---

## 1. Pre-install checks

Run the block for your OS and compare with the expected output.

### macOS (Terminal)

```
sw_vers -productVersion        # expect 12.0 or higher
uname -m                       # arm64 (Apple silicon) or x86_64 (Intel): both work
python3.12 --version           # expect Python 3.12.x; "command not found" is fine, step 2 installs it
brew --version                 # expect Homebrew 4.x; "command not found" is fine, step 2 installs it
curl -sI https://extensions.duckdb.org | head -1
```

The last command should print a line starting with `HTTP/` (any status such as
`HTTP/2 200` or `HTTP/2 403` means the host is reachable). No output or a
certificate error means a network/proxy is in the way; see
[Troubleshooting](#7-troubleshooting).

### Windows (PowerShell)

```
[System.Environment]::OSVersion.Version
```
Expect `Major` 10 (Windows 10 and 11 both report 10).

```
$env:PROCESSOR_ARCHITECTURE
```
Expect `AMD64`. `ARM64` machines are not a tested target; ask the maintainer
before continuing.

```
py -0p
```
Lists installed Pythons with paths. You want a `-V:3.12` line. If `py` is
unknown or 3.12 is missing, step 2 installs it.

> Use `py`, not `python`: on a machine without Python, typing `python` may open
> the Microsoft Store instead.

Check network access:

```
"github.com","pypi.org","files.pythonhosted.org","extensions.duckdb.org","openrouter.ai" |
  ForEach-Object { Test-NetConnection $_ -Port 443 | Select-Object ComputerName, TcpTestSucceeded }
```
Every line should show `TcpTestSucceeded : True` (`openrouter.ai` only matters
if you will use Ask).

### Linux (terminal)

```
cat /etc/os-release
uname -m                                   # x86_64 or aarch64
python3.12 --version || python3 --version  # need Python 3.12 or newer
curl -sI https://extensions.duckdb.org | head -1
```
The `curl` line should print a line starting with `HTTP/`.

Floe stores secrets in the system keyring, so a keyring service must be
running (GNOME Keyring, KWallet, or another Secret Service provider). After
installing (step 2) check it with:

```
python3 -c "import keyring; print(keyring.get_keyring())"
```
A real backend (for example `SecretService`) is what you want. If it prints a
`fail` or `null` backend, or you are on a headless machine, use the
environment-variable fallback in [step 4](#secrets-without-a-keyring-headless-linux).

---

## 2. Install

Floe needs **Python 3.12+** and [pipx](https://pipx.pypa.io/), which installs
Floe in its own isolated environment.

### macOS

1. Install Homebrew if `brew --version` failed (official one-liner):
   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   Follow the "Next steps" it prints (adding `brew` to your shell profile).
2. Install Python and pipx:
   ```
   brew install python@3.12 pipx
   pipx ensurepath
   ```
3. **Open a new terminal window** (so the PATH change applies).
4. Install Floe:
   ```
   pipx install --python python3.12 "https://github.com/Tcookie47/Floe/releases/download/v1.2.0/floe-1.2.0-py3-none-any.whl"
   ```
5. Verify:
   ```
   floe --version
   ```
   Expect `floe 1.2.0 (<commit>)`.

### Windows (PowerShell)

1. Install Python 3.12:
   ```
   winget install -e --id Python.Python.3.12
   ```
   (Alternative: download the installer from python.org and tick **Add
   python.exe to PATH**.) Close and reopen PowerShell afterwards.
2. Install pipx:
   ```
   py -3.12 -m pip install --user pipx
   py -3.12 -m pipx ensurepath
   ```
3. **Close and reopen PowerShell.**
4. Install Floe:
   ```
   pipx install --python 3.12 "https://github.com/Tcookie47/Floe/releases/download/v1.2.0/floe-1.2.0-py3-none-any.whl"
   ```
5. Verify:
   ```
   floe --version
   ```
   Expect `floe 1.2.0 (<commit>)`.

### Linux

1. Install Python 3.12 and pipx for your distribution:

   | Distribution | Command |
   |---|---|
   | Debian / Ubuntu | `sudo apt install python3.12 python3.12-venv pipx` |
   | Fedora | `sudo dnf install python3.12 pipx` |
   | Arch | `sudo pacman -S python python-pipx` |

   Ubuntu 24.04 ships Python 3.12. On older Debian/Ubuntu releases, get 3.12
   from the deadsnakes PPA or install it with pyenv first. Arch's `python` is
   already newer than 3.12, so use `python3` in the next steps if
   `python3.12` does not exist.
2. Put pipx's bin directory on your PATH, then **open a new terminal**:
   ```
   pipx ensurepath
   ```
3. Install Floe:
   ```
   pipx install --python python3.12 "https://github.com/Tcookie47/Floe/releases/download/v1.2.0/floe-1.2.0-py3-none-any.whl"
   ```
4. Verify:
   ```
   floe --version
   ```
   Expect `floe 1.2.0 (<commit>)`.

---

## 3. First start

Connect to your VPN first, then:

```
floe serve --background
```

Your browser opens Floe and the terminal is free again. Floe keeps running
after you close the terminal and survives laptop sleep.

| Command | What it does |
|---|---|
| `floe show --open` | Get back in (after closing the tab): mints a fresh link and opens it. |
| `floe show` | Prints a fresh link without opening a browser. |
| `floe stop` | Stops Floe. |
| `floe serve --no-browser` | Starts Floe in this terminal and only prints the link (use it if the browser does not open). Keep the terminal open; Ctrl+C stops it. |

Links are one-time: each works **once, within two minutes**. If you miss the
window, run `floe show` (or `floe show --open`) for a new one. Floe only
listens on `127.0.0.1`, so nobody else on the network can reach it.

---

## 4. Import the team profile

1. In Floe, click **Profiles…** (top bar).
2. Click **Import profile…** and pick your `team.floe-profile.json`.
   The form fills in. Nothing is saved yet.
3. The secret fields the profile needs are **highlighted**. Paste:
   - your **ADLS account key** into **Account key** (Storage section), and
   - your **Nessie client secret** into **Client secret** (Nessie section).
4. Click **Save**.
5. Click **Test connection**. Four steps run and stop at the first failure:

   | Step | Passing means | If it fails |
   |---|---|---|
   | TCP reachability | Nessie host is reachable | Not on VPN, or wrong network |
   | Token acquisition | The Nessie client secret was accepted | Wrong or expired Nessie client secret |
   | GET /trees/main | Nessie answered for the main ref | Permissions or URI problem: send diagnostics to the maintainer |
   | ADLS read | Table metadata could be read from storage | Wrong storage account key, or an HTTPS certificate error (see [Troubleshooting](#7-troubleshooting)) |

6. Close the dialog, pick your **Branch** in the top bar, choose a table in the
   tree, and open **Preview** to confirm you see rows.

### Where your secrets are stored

Secrets go only into your operating system's keyring, never into
`profiles.json` or any log:

- **macOS:** Keychain. If macOS asks Floe (Python) for access, choose
  **Always Allow**.
- **Windows:** Credential Manager.
- **Linux:** Secret Service (GNOME Keyring / KWallet).

Profiles, history and prefs live in your per-user data directory:

| OS | Data | Logs |
|---|---|---|
| macOS | `~/Library/Application Support/Floe` | `~/Library/Logs/Floe` |
| Linux | `~/.local/share/Floe` | `~/.local/state/Floe/log` |
| Windows | `%LOCALAPPDATA%\Floe` | `%LOCALAPPDATA%\Floe\Logs` |

### Secrets without a keyring (headless Linux)

If Floe reports no usable OS keyring, it reads secrets from environment
variables instead. Leave the secret fields empty in the UI and set, **before
starting Floe**:

```
FLOE_SECRET__<PROFILE>__<FIELD>
```

`<PROFILE>` is the profile name and `<FIELD>` the internal field name, both
upper-cased with every non-alphanumeric character replaced by `_`. For a
profile named `team`:

```
export FLOE_SECRET__TEAM__ADLS_ACCOUNT_KEY='paste-your-account-key'
export FLOE_SECRET__TEAM__NESSIE_CLIENT_SECRET='paste-your-nessie-client-secret'
```

(A profile named `eg-tenant1` would use `FLOE_SECRET__EG_TENANT1__...`.)
Put the lines in `~/.profile`, run `chmod 600 ~/.profile`, then log in again
(or `source ~/.profile`) and start Floe from that shell. Warning: do not type
the secrets directly at the prompt, since they end up in your shell history;
edit the file in an editor instead. If Floe is already running, `floe stop`
first so it restarts with the variables.

---

## 5. Set up Ask (optional, free)

Ask turns a plain-language question into SQL and puts it in the editor for you
to review. It never runs it automatically.

1. Create an account at <https://openrouter.ai> (sign in with Google, GitHub
   or email).
2. Create an API key: **Settings → Keys → Create key**. Name it `Floe`;
   optionally set a credit limit of `0`. Copy the key now, since it is shown
   only once.
3. In Floe open the **SQL** tab, click **Ask…**, then **Settings…** in the
   Ask panel (or use **Help ▾ → Ask settings…**).
4. Tick **Enabled**, paste the key into **API key**, and click **Save**.

There is no model to choose: Floe tries an ordered list of free models
automatically and falls back if one is busy. It talks to
`https://openrouter.ai/api/v1` (the default Base URL under **Advanced**).

- **Free-tier limits** are roughly 20 requests per minute and 200 per day;
  these are OpenRouter's and may change.
- If you see a **"no endpoints matching your data policy"** error: on
  OpenRouter go to **Settings → Privacy** and allow the options that free
  models require, then try again.
- Without a keyring, set `FLOE_OPENROUTER_API_KEY` before starting Floe
  instead of saving the key in the UI.

**Privacy:** only table/column names and types, other view names, and your
question are sent, never row data or results. Use **What will be sent** to see
the exact prompt. Free providers may log prompts, so **don't type patient
identifiers** in your question.

---

## 6. Updating and uninstalling

### Update

```
floe stop
pipx install --force "https://github.com/Tcookie47/Floe/releases/download/vX.Y.Z/floe-X.Y.Z-py3-none-any.whl"
floe serve --background
```
Replace `X.Y.Z` with the newest version from the
[Releases page](https://github.com/Tcookie47/Floe/releases). Your profiles and
secrets are kept. On macOS after an update, the Keychain may ask again; choose
**Always Allow**.

### Uninstall

```
floe stop
pipx uninstall floe
```

This does **not** remove your profiles, history, logs or saved secrets:

- **Data and logs:** delete the directories in the table in
  [step 4](#where-your-secrets-are-stored) (for example
  `rm -r ~/Library/Application\ Support/Floe ~/Library/Logs/Floe` on macOS).
- **Secrets:** remove the entries named `Floe` (macOS: Keychain Access, search
  "Floe"; Windows: Control Panel → Credential Manager → Windows Credentials,
  entries for `Floe`; Linux: your keyring app, e.g. Passwords and Keys or
  KWallet Manager). Alternatively delete a profile in **Profiles…** before
  uninstalling: that also deletes its saved secrets and SQL history.

---

## 7. Troubleshooting

| Symptom | What to do |
|---|---|
| `floe: command not found` / `'floe' is not recognized` | Run `pipx ensurepath` and open a **new** terminal. |
| Browser did not open | Run `floe show` and open the printed link yourself, or start with `floe serve --no-browser` and paste the link within 2 minutes (it works once). |
| Page says "This tab isn't signed in. Open Floe from the link printed in the terminal…" | Run `floe show --open` for a fresh link. |
| `Floe isn't running.` | Start it: `floe serve --background`. |
| Test connection step 1 (TCP reachability) fails | Connect to the VPN and retry. |
| Step 2 (Token acquisition) fails | Nessie client secret is wrong or expired; re-enter it in **Profiles…** and Save. |
| Step 4 (ADLS read) fails | Check the storage account key. If the error mentions an HTTPS/certificate problem, your network inspects TLS: ask the maintainer for a CA certificate file and set it in the profile's **CA cert file** field, or use a different network. |
| First query hangs or errors mentioning `extensions.duckdb.org` | The host is blocked; ask IT to allow it (DuckDB downloads its extensions once, then caches them). |
| Keychain prompts on macOS | Choose **Always Allow**. |
| "No usable OS keyring" | Use the environment variables in [step 4](#secrets-without-a-keyring-headless-linux). |
| Something else | **Help ▾ → Copy diagnostics** and send the text to the maintainer. It is redacted and contains no secrets. **Never send screenshots showing secrets.** |

More detail on everything above (Preview, Schema, SQL, tenant filter, export,
history) is in [USAGE.md](USAGE.md).
