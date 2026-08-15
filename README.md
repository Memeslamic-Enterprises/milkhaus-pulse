# milkhaus-pulse

The homelab cannot announce its own death: when the internet at home goes,
the game servers and the Discord bot go dark together. This tiny repo is the
voice that survives - a GitHub Action that probes from outside every five
minutes and posts to Discord **as teto** when everything back home stops
answering, then posts again when it recovers.

- `pulse.py` - a real Minecraft Server List Ping against `mc.milkhaus.net`
  (a TCP connect isn't proof: the hostname fronts a relay that accepts
  connections whether or not the homelab behind it is alive) plus an HTTPS
  probe of the web map. Both dark for two consecutive runs = announce.
- `.github/workflows/pulse.yml` - the schedule, the state cache, and a
  monthly heartbeat commit so GitHub never disables the cron for inactivity.

Secrets (repo Settings → Secrets and variables → Actions):

| secret | meaning |
|---|---|
| `DISCORD_WEBHOOK_URL` | preferred: a channel webhook; used alone if set |
| `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` | fallback: post through the bot |

Nothing here holds a secret in code, and the hostnames are the public ones
players already use.
