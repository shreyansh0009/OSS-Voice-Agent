# Voice Agent — Multi-Agent AI Call Platform

Enterprise-grade, self-hosted multi-agent voice call platform with Salesforce CRM integration. Build intelligent voice agents that handle customer calls, extract data, and automatically create cases in Salesforce.

## Key Features

✨ **Multi-Agent Orchestration** — Seamless handoffs between specialized agents (Hello → Screener → Sales → Scheduler → Closer)

🎙️ **Multilingual Support** — Auto-detect and respond in Hindi, Bengali, Telugu, Tamil, Marathi, Gujarati, Kannada, Punjabi, Malayalam, Odia, and English

📊 **Real-Time Analytics** — Intent detection, sentiment analysis, and call quality metrics

💾 **Salesforce Integration** — Automatic case creation post-call with:
- Full call transcript
- Extracted customer data (name, phone, email, etc.)
- Call recording (Cloudinary)
- Intent & sentiment analysis
- Customer details & interaction history

🔊 **Premium Voice Quality** — ElevenLabs Flash v2.5, Cartesia Sonic-2, or local Kokoro TTS

🧠 **RAG-Powered Knowledge** — ChromaDB vector store for context-aware responses

🔌 **Flexible LLM Support** — Groq, OpenAI, Ollama, or LiteLLM (multi-provider routing)

📞 **Multiple Telephony Options** — Asterisk AudioSocket, Twilio, Exotel, or custom SIP

## Architecture

```
Inbound Call (Asterisk/Twilio/Exotel)
       │
       ▼
  AudioSocket TCP / WebSocket
       │
       ▼
  StreamingPipeline
   ├── STT (Deepgram/Whisper)    — speech → text (real-time)
   ├── Language Detection         — auto-detect language
   ├── Orchestrator               — route to active agent
   │     ├── HelloAgent           — greet + collect name/phone
   │     ├── ScreenerAgent        — eligibility + intent detection
   │     ├── SalesAgent           — product consultation
   │     ├── SchedulerAgent       — book appointment (tool-calling)
   │     ├── ServiceAgent         — support & troubleshooting
   │     └── CloserAgent          — wrap up + sentiment analysis
   ├── TTS (ElevenLabs/Cartesia)  — text → speech (streaming)
   └── Data Extraction            — extract entities, sentiment, intent
       │
       ▼
  Salesforce Case Creation
   ├── Customer record lookup/create
   ├── Case with transcript
   ├── Call recording link
   ├── Extracted data fields
   ├── Intent & sentiment tags
   └── Activity history
```

## Stack

| Component | Technology |
|-----------|-----------|
| **LLM** | Groq (llama-3.1-8b, 750 tok/s) / OpenAI / Ollama / LiteLLM |
| **STT** | Deepgram Nova-3 (streaming, ~200ms) / Whisper |
| **TTS** | ElevenLabs Flash v2.5 / Cartesia Sonic-2 / Kokoro / Piper |
| **Telephony** | Asterisk AudioSocket / Twilio / Exotel |
| **CRM** | Salesforce (OAuth2 password grant) |
| **RAG** | ChromaDB (embedded vector store) |
| **Embeddings** | Sentence-Transformers / Ollama |
| **Recording** | Cloudinary |
| **Server** | FastAPI + AsyncIO |
| **Hosting** | EC2 / Docker / Self-hosted |

## Quick Start

### 1. Clone & Setup

```bash
git clone <repo>
cd Voice-Agent-Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your API keys:

```bash
# LLM
LLM_PROVIDER=groq
GROQ_API_KEY=your_groq_key
GROQ_MODEL=llama-3.1-8b-instant

# STT
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=your_deepgram_key
DEEPGRAM_MODEL=nova-3

# TTS
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your_elevenlabs_key
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM

# Salesforce
SF_INSTANCE_URL=https://orgfarm-xxx.develop.my.salesforce.com
SF_CLIENT_ID=your_client_id
SF_CLIENT_SECRET=your_client_secret
SF_USERNAME=your_username
SF_PASSWORD=your_password_plus_token

# Call Recording
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret

# Telephony (Asterisk AudioSocket)
AUDIOSOCKET_HOST=0.0.0.0
AUDIOSOCKET_PORT=9093
```

### 3. Run the Server

```bash
python main.py
```

You should see:
```
Servers running:
  AudioSocket TCP → 0.0.0.0:9093
  HTTP Admin      → 0.0.0.0:8080
```

### 4. Or with Docker

```bash
docker-compose up
```

## Agents

Each agent is a specialized conversational AI with its own prompt and handoff rules:

| Agent | Purpose | Handoff To |
|-------|---------|-----------|
| **HelloAgent** | Greet caller, collect name/phone | Screener |
| **ScreenerAgent** | Eligibility questions, intent detection | Sales/Service |
| **SalesAgent** | Product consultation, upsell | Scheduler |
| **SchedulerAgent** | Book appointments (MCP tool-calling) | Closer |
| **ServiceAgent** | Support tickets, troubleshooting | Closer |
| **CloserAgent** | Wrap up, sentiment analysis, goodbye | End call |

Agents automatically hand off based on squad definition (`config/squads/*.json`). Each handoff carries conversation history and metadata.

## Salesforce Integration

### Case Creation Flow

After call completion, the system automatically:

1. **Lookup/Create Contact** — Find or create customer record
2. **Create Case** with:
   - Full transcript
   - Extracted data (name, phone, email, company, etc.)
   - Call recording URL (Cloudinary)
   - Intent classification (e.g., "Product Inquiry", "Support Request")
   - Sentiment score (positive/negative/neutral)
   - Call duration & quality metrics
3. **Link Activities** — Add call log to customer record

### Salesforce Setup

1. Create a Connected App in Salesforce:
   - Settings → Apps → App Manager → New Connected App
   - Enable OAuth 2.0
   - Scopes: `api`, `refresh_token`, `offline_access`
   - Copy Client ID & Secret

2. Create a Salesforce user for API access:
   - Assign "System Administrator" profile
   - Generate security token (Settings → Reset Security Token)

3. Set environment variables:
   ```bash
   SF_INSTANCE_URL=https://your-org.my.salesforce.com
   SF_CLIENT_ID=your_connected_app_client_id
   SF_CLIENT_SECRET=your_connected_app_secret
   SF_USERNAME=api_user@yourorg.com
   SF_PASSWORD=password+security_token
   ```

## Knowledge Base & RAG

Store company knowledge in `knowledge_bases/`:

```
knowledge_bases/
├── knowledge_base_company.md      — Company overview
├── knowledge_base_products.md     — Product catalog
├── knowledge_base_automobile.md   — Industry-specific
└── godrej_appliances.md           — Brand-specific
```

The system automatically:
- Ingests markdown files into ChromaDB
- Retrieves relevant context for each agent
- Provides accurate, sourced responses

## Customization

### Add a New Agent

1. Create `agents/my_agent.py`:
```python
from core.agent import BaseAgent, AgentResponse

class MyAgent(BaseAgent):
    async def handle(self, transcript: str, session: CallSession) -> AgentResponse:
        # Your logic here
        return AgentResponse(text="Response", end_call=False)
```

2. Add prompt to `prompts/my_agent.md`

3. Register in `agents/registry.py`:
```python
orchestrator.register(MyAgent(llm, retriever))
```

4. Add to squad in `config/squads/default_squad.json`

### Swap LLM Provider

```bash
# Use OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Or use Ollama locally
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Or use LiteLLM for multi-provider
LLM_PROVIDER=litellm
LITELLM_MODEL=groq/llama-3.1-70b-versatile
```

### Swap STT Provider

```bash
# Use Whisper (local, no API key)
STT_PROVIDER=whisper
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
```

### Swap TTS Provider

```bash
# Use Cartesia
TTS_PROVIDER=cartesia
CARTESIA_API_KEY=your_key
CARTESIA_VOICE_ID=your_voice_id

# Or use local Kokoro
TTS_PROVIDER=kokoro
KOKORO_MODEL_PATH=/path/to/model.onnx
KOKORO_VOICE=af_heart
```

## Deployment

### EC2 Sizing

| Use Case | Instance | Notes |
|----------|----------|-------|
| Dev/testing | t3.medium (2 vCPU, 4GB) | Groq LLM, Deepgram STT |
| Production (CPU) | c5.2xlarge (8 vCPU, 16GB) | Groq LLM, Deepgram STT, 10+ concurrent calls |
| Production (GPU) | g4dn.xlarge (T4 GPU) | Local Whisper large, Ollama 13B |

### Docker Deployment

```bash
docker-compose up -d
docker-compose logs -f
```

### Asterisk Integration

See `SETUP.md` for SSH tunnel setup and Asterisk dialplan configuration.

## Monitoring & Logging

- **Latency logs** → `latency.log` (real-time call metrics)
- **Transcripts** → `transcripts/` (JSON per call)
- **Extracted data** → `extracted_data/` (entities, sentiment, intent)
- **Admin API** → `http://localhost:8080/admin/health`

## Troubleshooting

**Call disconnects immediately**
- Check Asterisk connection: `asterisk -rx 'core show channels'`
- Verify AudioSocket port is open: `lsof -i :9093`

**No voice output**
- Check TTS API key and rate limits
- Verify `ELEVENLABS_API_KEY` or `CARTESIA_API_KEY`

**Salesforce case not created**
- Check SF credentials in `.env`
- Verify Connected App has correct scopes
- Check logs for OAuth errors

**Language detection issues**
- Ensure Deepgram language detection is enabled
- Check `DEEPGRAM_LANGUAGE` setting (empty = auto-detect)

## License

See LICENSE file
