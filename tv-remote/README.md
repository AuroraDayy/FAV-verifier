# Sony TV Web Remote (free, local, no account)

A phone remote for Sony BRAVIA Android TV / Google TV models (tested flow targets the
KD-49X8050H). It uses the TV's built-in network control API, which is the same API the
paid "TV remote" apps use. Everything stays on your home Wi-Fi.

## Why there's a small server instead of "just a website"

A web page alone **can't** do this:
- Browsers can't scan your local network or send the custom headers the TV requires
  (CORS / Private Network Access rules block it). An HTTPS site also can't call the
  TV's plain-HTTP address.
- **Bluetooth isn't how these apps work.** The TV is controlled over Wi-Fi, and
  iPhone Safari has no Web Bluetooth support anyway.

So `server.py` runs on a computer on your Wi-Fi (laptop, desktop, Raspberry Pi, even an
Android phone via Termux), serves the remote page, and forwards button presses to the TV.

## Run it

Requires Python 3.8+ only (nothing to install).

```bash
cd tv-remote
python3 server.py
```

It prints something like `http://192.168.1.50:8765`. Open that on your phone
(same Wi-Fi). In Safari: **Share → Add to Home Screen** to get an app icon.

Windows: install Python from python.org, then double-click `server.py` or run
`py server.py`. Allow it through the firewall on *private* networks when asked.

## First-time pairing

1. Tap **Search network** (or type the TV's IP address).
2. Tap **Show code on TV**. A 4-digit PIN appears on the TV. Type it in.
3. Done. The pairing is saved in `config.json` and refreshes itself automatically.

If no PIN appears / pairing fails, on the TV enable:
`Settings → Network & Internet → Home network → Remote device settings → Control remotely`.
(No remote to get there? The free **Google TV** app on iPhone/Android has a built-in
phone remote that works with Android TV models. Use it once to flip the setting.)

Alternatively use a **Pre-Shared Key**: `Settings → Network & Internet → Home network →
IP control → Authentication: Normal and Pre-Shared Key`, set a key, enter it in the app.

## Turning the TV ON

Turning **off** works out of the box. Turning **on** from the phone needs
`Settings → Network & Internet → Remote start = ON` (otherwise the TV's network goes
fully to sleep). Costs a bit more standby power.

## Security

Anyone on your Wi-Fi who opens the page can control the TV. Fine for a home network;
don't port-forward it to the internet.

## Testing without a TV

`bash tests/run_mock_test.sh /tmp/tvtest` runs the server against a fake TV
(`tests/mock_tv.py`) and exercises pairing, keys, power, and auth failures.
