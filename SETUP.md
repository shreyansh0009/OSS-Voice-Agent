# Voice Agent — Setup & Run Guide

## Prerequisites

- Python 3.11+
- Asterisk server (remote) with `res_audiosocket` module
- SSH access to the Asterisk server

---

## Step 1: Create Python Virtual Environment

```bash
cd voice-agent-twilio

python3 -m venv .venv
source .venv/bin/activate
```

## Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 3: Configure Environment

Edit `.env` and set your API keys:

```
GROQ_API_KEY=your_groq_key
DEEPGRAM_API_KEY=your_deepgram_key
CARTESIA_API_KEY=your_cartesia_key
```

AudioSocket config (default values are fine):

```
AUDIOSOCKET_HOST=0.0.0.0
AUDIOSOCKET_PORT=9093
```

## Step 4: Start the Python Server

**Terminal 1:**

```bash
cd voice-agent-twilio
source .venv/bin/activate
python main.py
```

Wait until you see:

```
AudioSocket TCP server listening on 0.0.0.0:9093
Servers running:
  AudioSocket TCP → 0.0.0.0:9093
  HTTP Admin      → 0.0.0.0:8081
```

## Step 5: Open SSH Tunnel to Asterisk Server

**Terminal 2:**

First, kill any existing process on port 9093 on the remote server:

```bash
ssh -p 25917 root@103.171.45.90 "fuser -k 9093/tcp 2>/dev/null; true"
ssh -p 25917 \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -R 9093:localhost:9093 \
  -R 5002:localhost:8081 \
  -L 5038:127.0.0.1:5038 \
  -N root@103.171.45.90
```


Enter the server password when prompted. The tunnel stays open as long as this terminal is running.

**What the tunnel does:**

| Flag | Direction | Meaning |
|------|-----------|---------|
| `-R 9093:localhost:9093` | Remote → Local | Asterisk AudioSocket → Python server |
| `-R 5002:localhost:8081` | Remote → Local | Remote HTTP calls → Python admin API |
| `-L 5038:127.0.0.1:5038` | Local → Remote | Local AMI access → Asterisk Manager |

## Step 6: Make a Call

Dial **+91 7935459109** from your phone. The call flow:

```
Phone → SIP → Asterisk → AudioSocket TCP:9093 (tunnel) → Python server
                                                              ↓
                                              Deepgram STT → Groq LLM → Cartesia TTS
                                                              ↓
Phone ← SIP ← Asterisk ← AudioSocket TCP:9093 (tunnel) ← Python server
```

---

## Troubleshooting

### Call disconnects immediately
- Check Terminal 2 — is the SSH tunnel still running?
- Run `lsof -i :9093` to verify the Python server is listening
- Check Asterisk CLI: `ssh -p 25917 root@103.171.45.90 "asterisk -rx 'core show channels'"`

### No voice / silence
- Check Terminal 1 logs for `Cartesia stream` or `TTS error` messages
- Verify `CARTESIA_API_KEY` is set correctly in `.env`

### SSH tunnel error "Address already in use"
- Kill the old process first: `ssh -p 25917 root@103.171.45.90 "fuser -k 9093/tcp 2>/dev/null; true"`

### "connect_to localhost port 9093: failed"
- Make sure Terminal 1 (Python server) is running before opening the tunnel
