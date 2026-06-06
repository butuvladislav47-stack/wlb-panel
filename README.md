# WLB2 Panel 0.2.0

Server part for permanent WLB client links with reserve WB Stream slots.

## Upload to GitHub

Upload these files to the root of the GitHub repository:

- `install.sh`
- `panel.py`
- `README.md`

Then install on a clean Ubuntu server:

```bash
wget -O /tmp/wlb-install.sh https://raw.githubusercontent.com/butuvladislav47-stack/wlb-panel/main/install.sh && sudo bash /tmp/wlb-install.sh
```

## What WLB2 changes

The old server created direct temporary `wbstream://...` links.

WLB2 creates permanent client links instead:

```text
http://SERVER:8088/c/<client-token>
```

The WLB2 mobile app saves this permanent server link. On every connect, it calls:

```text
GET /api/v1/client/start?token=<client-token>
```

The server creates a fresh WB Stream room and returns:

```json
{
  "ok": true,
  "session_id": "...",
  "join_link": "wbstream://...",
  "heartbeat_url": "...",
  "stop_url": "..."
}
```

If two devices use the same client link, the server creates two separate
temporary `wbstream://...` rooms.

For whitelist mobile networks, the app also calls:

```text
GET /api/v1/client/refresh?token=<client-token>
```

The server prepares 3 reserve WB Stream slots and returns:

```json
{
  "ok": true,
  "fallback_links": [
    "wbstream://...",
    "wbstream://...",
    "wbstream://..."
  ]
}
```

The Android client refreshes these links automatically when it opens while the
server is reachable. If the mobile operator blocks direct access to the server,
the client falls back to the locally saved reserve links.

## Reboot behavior

After server reboot:

- `wlb2-panel` starts automatically through systemd.
- admin password, clients, server Chrome profile, and WB cookies are preserved.
- old temporary WB Stream rooms are not restored.
- permanent client links remain valid.
- when a mobile client can reach the server again, it refreshes 3 reserve links
  automatically.

## First login

1. Open the panel on port `8088`.
2. Open `WB Login Browser`.
3. Start the server browser.
4. Open noVNC on port `6080`.
5. Log in to `stream.wb.ru`.
6. Click `Import cookies`.

After that, the WB login should survive server reboot while cookies remain valid.

## Client management

In the dashboard:

- create a client for each person or device group;
- copy the permanent client link;
- optionally set `max_active`:
  - `0` means unlimited;
  - `1` means only one active session for this client;
  - `2+` allows several devices at once.

## Mobile client API

Start a session:

```http
GET /api/v1/client/start?token=<client-token>
```

Refresh reserve links:

```http
GET /api/v1/client/refresh?token=<client-token>
```

Heartbeat:

```http
GET /api/v1/client/heartbeat?token=<client-token>&session_id=<session-id>
```

Stop:

```http
GET /api/v1/client/stop?token=<client-token>&session_id=<session-id>
```

The mobile app should:

1. save the permanent client link once;
2. call `refresh` automatically when it opens and save the returned reserve links;
3. call `start` on Connect when the server is reachable;
4. connect internally to returned `join_link`;
5. if `start` is blocked by the operator, connect through a saved reserve link;
6. send heartbeat every 30 seconds for active sessions;
7. call `stop` when the user disconnects.

## Diagnostics

```bash
systemctl status wlb2-panel
journalctl -u wlb2-panel -n 100 --no-pager
ss -lntp | grep -E '8088|6080'
```

Ports to open in hosting firewall:

- `8088/tcp` for the panel and client API;
- `6080/tcp` for noVNC.

## DNS

The installer builds `headless-wbstream-creator` with server-side DNS redirect.
The Android app can keep `DNS: System`; DNS traffic sent to local router DNS is
redirected by the server to `1.1.1.1`.
